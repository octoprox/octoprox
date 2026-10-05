# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""HTTP Proxy Server for Octoprox.

This module implements a real HTTP proxy server that can be used directly
by HTTP clients (e.g., via http_proxy environment variable).

It supports:
- HTTP CONNECT method for HTTPS tunneling
- Regular HTTP request forwarding
- Upstream proxy selection via ProxyManager strategies
- Project-based authentication via HTTP Basic Auth (Proxy-Authorization header)

Emits request_completed signals instead of directly calling ProxyManager.
"""

import asyncio
import base64
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog
from python_socks.async_.asyncio import Proxy as SocksProxy

from api.core.config import settings
from api.core.event_bus import event_bus
from api.core.signals import request_completed, request_rejected
from api.core.traffic_limiter import TrafficMeter
from api.core.username_params import AuthResult, parse_username_params
from api.geo.verifier import ExitVerifier
from api.models.location import LocationTarget
from api.models.project import MitmMode, Project
from api.models.proxy import Proxy, ProxyProtocol

if TYPE_CHECKING:
    from api.core.mitm import MitmHandler
    from api.core.proxy_manager import ProxyManager

logger = structlog.get_logger()

# Buffer size for tunneling
BUFFER_SIZE = 65536

# Reason phrases for the statuses a traffic limit can answer with.
TRAFFIC_LIMIT_REASONS = {429: "Too Many Requests", 509: "Bandwidth Limit Exceeded"}
# RFC 9209 Proxy-Status header: the same signal whichever status is configured.
TRAFFIC_LIMIT_PROXY_STATUS = 'Proxy-Status: octoprox; error=bandwidth_limit_exceeded'

# What a tunnel direction calls per chunk; False means stop the transfer.
_ByteCounter = Callable[[int], bool] | None

# Headers scoped to a single hop and therefore never relayed (RFC 9110
# section 7.6.1 and section 11.7, plus the legacy Proxy-Connection). The
# request set matters most: Proxy-Authorization carries the client's
# Octoprox project credential, and relaying it leaks that credential to
# the upstream vendor (or, over SOCKS, straight to the origin). For HTTP
# upstreams it also lands as a second Proxy-Authorization header behind
# the one we add for the vendor. Bright Data tolerates the duplicate;
# Oxylabs' HAProxy edge rejects the request with a stock 400 Bad request.
#
# Transfer-Encoding and Trailer are hop-by-hop by the letter of the spec
# but we relay message bodies byte for byte, so they stay.
HOP_BY_HOP_REQUEST_HEADERS = frozenset({
    "connection", "keep-alive", "te", "upgrade",
    "proxy-authorization", "proxy-connection",
})
HOP_BY_HOP_RESPONSE_HEADERS = frozenset({
    "connection", "keep-alive", "upgrade",
    "proxy-authenticate", "proxy-connection",
})
# Names a peer may list in its Connection header that we refuse to drop
# regardless, because they frame the message rather than the hop.
BODY_FRAMING_HEADERS = frozenset({"content-length", "transfer-encoding", "trailer", "host"})


class ProxyServer:
    """HTTP Proxy Server that forwards requests through managed upstream proxies."""

    def __init__(
        self,
        proxy_manager: "ProxyManager",
        mitm_handler: "MitmHandler | None" = None,
        exit_verifier: ExitVerifier | None = None,
    ) -> None:
        self._proxy_manager = proxy_manager
        self._mitm_handler = mitm_handler
        # Exit verification before forwarding; None disables preflight entirely.
        self._exit_verifier = exit_verifier
        self._server: asyncio.Server | None = None
        self._host = settings.host
        self._port = settings.proxy_port
        self._timeout = settings.connection_timeout
        self._client_tasks: set[asyncio.Task[None]] = set()

    @property
    def is_listening(self) -> bool:
        """True while the listener socket is accepting client connections."""
        return self._server is not None and self._server.is_serving()

    @property
    def active_connections(self) -> int:
        """Number of client connections currently being served."""
        return len(self._client_tasks)

    @property
    def port(self) -> int:
        """The port actually bound, which differs from the configured one when it was 0."""
        if self._server is not None and self._server.sockets:
            bound: int = self._server.sockets[0].getsockname()[1]
            return bound
        return self._port

    async def start(self) -> None:
        """Start the proxy server."""
        self._server = await asyncio.start_server(
            self._handle_client_wrapper,
            self._host,
            self._port,
        )
        logger.info(
            "Proxy server started",
            host=self._host,
            port=self._port,
        )

    async def stop(self) -> None:
        """Stop the proxy server gracefully."""
        if self._server:
            # Stop accepting new connections
            self._server.close()
            await self._cancel_client_tasks()
            await self._server.wait_closed()
            logger.info("Proxy server stopped")

    async def _cancel_client_tasks(self) -> None:
        """Cancel every connection in flight and wait for the handlers to finish.

        Done before ``wait_closed``, which since Python 3.12.1 waits for every
        accepted connection to finish: an open tunnel would otherwise hold
        shutdown until the client went away on its own.
        """
        if not self._client_tasks:
            return
        logger.info("Cancelling active client connections", count=len(self._client_tasks))
        for task in self._client_tasks:
            task.cancel()
        await asyncio.gather(*self._client_tasks, return_exceptions=True)
        self._client_tasks.clear()

    async def _handle_client_wrapper(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """Wrapper to track client handler tasks."""
        task = asyncio.current_task()
        if task:
            self._client_tasks.add(task)
        try:
            await self._handle_client(client_reader, client_writer)
        finally:
            if task:
                self._client_tasks.discard(task)

    async def _get_upstream_proxy(
        self,
        project_id: str,
        session_id: str | None = None,
        target_host: str | None = None,
        location: LocationTarget | None = None,
    ) -> Proxy | None:
        """Select an upstream proxy from the authenticated project's pool.

        Every request is authenticated against a project before it gets here
        (see _handle_client), so selection is always project-scoped and uses
        that project's routing strategy.

        Args:
            project_id: The authenticated project.
            session_id: The client's explicit -sessid- value: the routing key
                for sticky routing and the seed of a dynamic-sessions vendor
                session. Only an explicit -sessid- is a session: without one
                the request has no key, sticky routes it like random, and
                nothing is pinned to the client's address, which behind a NAT
                or load balancer is everyone's address.
            target_host: If provided, only consider proxies from connectors
                whose domain routing config allows this host.
            location: If provided, only consider proxies that serve this
                place (from the -cc-, -st- and -city- username suffixes).

        Returns:
            Selected proxy or None if no healthy proxies available.
        """
        return await self._proxy_manager.select_proxy_for_project(
            project_id, session_id, target_host, location
        )

    def _authenticate_project(self, headers: dict[str, str]) -> AuthResult | None:
        """Authenticate a client request using Proxy-Authorization header.

        Expects HTTP Basic Auth in the Proxy-Authorization header with
        project username and password. The username may carry routing
        parameters as suffixes, in any order:
        <username>[-sessid-<session_id>][-cc-<country>][-st-<state>][-city-<city>]

        Args:
            headers: Request headers (lowercase keys)

        Returns:
            AuthResult with authenticated Project and optional sessid and
            location, or None if authentication fails.
        """
        auth_header = headers.get("proxy-authorization", "")
        if not auth_header:
            return None

        # Parse Basic auth
        if not auth_header.lower().startswith("basic "):
            return None

        try:
            encoded_credentials = auth_header[6:]  # Remove "Basic " prefix
            decoded = base64.b64decode(encoded_credentials).decode("utf-8")
            if ":" not in decoded:
                return None
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return None

        # Parse routing parameters (sessid, location) from username
        params = parse_username_params(username)
        real_username = params.username

        # Look up project by the real username (without parameter suffixes)
        project = self._proxy_manager.get_project_by_username(real_username)
        if not project:
            logger.debug("Project not found for username", username=real_username)
            return None

        # Verify password (plain text comparison as per requirements)
        if project.password != password:
            logger.debug("Invalid password for project", project_id=project.id)
            return None

        return AuthResult(
            project=project, sessid=params.sessid, location=params.location, location_error=params.location_error
        )

    @staticmethod
    def _no_proxy_message(location: LocationTarget | None) -> str:
        """Human-readable 502 body when no upstream proxy matched the request."""
        if location:
            return f"No upstream proxy available for {location.describe()} and this domain"
        return "No upstream proxy available for this domain"

    async def _reject_no_proxy(
        self,
        client_writer: asyncio.StreamWriter,
        project_id: str,
        target_host: str | None,
        session_id: str | None,
        location: LocationTarget | None,
    ) -> None:
        """Answer a request no proxy could serve, saying why.

        Quarantine (every proxy resting) is a 429 to retry later. A traffic
        limit (every connector that could serve the request is over its
        period's bytes) is the connector's configured status, 509 by
        default, with a Proxy-Status header naming the error whichever
        status was chosen. Anything else is a 502: nothing is configured
        for this request.
        """
        if await self._proxy_manager.are_all_proxies_quarantined(
            project_id, target_host, session_id, location
        ):
            await self._send_error(
                client_writer, 429, "Too Many Requests",
                "All proxies are temporarily rate-limited. Retry later.",
            )
            await event_bus.publish(request_rejected,
                self, project_id=project_id, reason="all_proxies_quarantined"
            )
            return
        limit_status = self._proxy_manager.traffic_limit_status(project_id, target_host, location)
        if limit_status is not None:
            await self._send_error(
                client_writer, limit_status, TRAFFIC_LIMIT_REASONS.get(limit_status, "Bandwidth Limit Exceeded"),
                "Every connector that could serve this request has reached its traffic limit.",
                extra_headers=[TRAFFIC_LIMIT_PROXY_STATUS],
            )
            await event_bus.publish(request_rejected,
                self, project_id=project_id, reason="traffic_limit_exceeded"
            )
            return
        await self._send_error(
            client_writer, 502, "Bad Gateway", self._no_proxy_message(location)
        )
        await event_bus.publish(request_rejected,
            self, project_id=project_id, reason="no_proxy_available"
        )

    async def _verify_exit(
        self,
        project: Project,
        proxy: Proxy,
        session_id: str | None,
        location: LocationTarget | None,
        target_host: str | None,
        client_writer: asyncio.StreamWriter,
    ) -> Proxy | None:
        """Hand the selected upstream to preflight; None when the request was refused."""
        if self._exit_verifier is None:
            return proxy
        decision = await self._exit_verifier.verify(
            project, proxy, session_id=session_id, location=location, target_host=target_host
        )
        if not decision.rejected:
            return decision.proxy
        await self._send_error(client_writer, 502, "Bad Gateway", decision.rejection or "Exit location mismatch")
        await event_bus.publish(request_rejected, self, project_id=project.id, reason="preflight_mismatch")
        return None

    async def _connect_via_proxy(
        self,
        proxy: Proxy,
        target_host: str,
        target_port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Connect to target through upstream proxy.

        Uses python-socks library to handle SOCKS4, SOCKS5, and HTTP CONNECT
        proxy protocols.

        Args:
            proxy: The upstream proxy to connect through
            target_host: Destination hostname or IP
            target_port: Destination port

        Returns:
            Tuple of (reader, writer) with tunnel established

        Raises:
            ConnectionError: If connection or handshake fails
            TimeoutError: If connection times out
        """
        # Create proxy instance from URL (handles all protocol types). The
        # target name travels to the proxy unresolved on every protocol:
        # SOCKS5 and HTTP CONNECT carry names natively, and rdns makes SOCKS4
        # use its 4a form instead of resolving here, so no path looks a
        # target up on this host.
        socks_proxy = SocksProxy.from_url(proxy.url, rdns=True)

        # Connect through the proxy - returns a socket with tunnel established
        sock = await asyncio.wait_for(
            socks_proxy.connect(dest_host=target_host, dest_port=target_port),
            timeout=self._timeout,
        )

        # Wrap the socket in asyncio streams
        try:
            reader, writer = await asyncio.open_connection(sock=sock)
        except Exception:
            sock.close()
            raise

        return reader, writer

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an incoming client connection."""
        client_addr = client_writer.get_extra_info("peername")
        logger.debug("New client connection", client_addr=client_addr)

        try:
            # Read the first line to determine request type
            first_line = await asyncio.wait_for(
                client_reader.readline(),
                timeout=self._timeout,
            )

            if not first_line:
                return

            first_line_str = first_line.decode("utf-8", errors="replace").strip()
            parts = first_line_str.split()

            if len(parts) < 3:
                await self._send_error(client_writer, 400, "Bad Request")
                return

            method, target, version = parts[0], parts[1], parts[2]

            # Read headers
            headers = await self._read_headers(client_reader)

            # Authenticate project using Proxy-Authorization header
            auth_result = self._authenticate_project(headers)
            logger.debug("Authenticated project", headers=headers, auth_result=auth_result)
            if not auth_result:
                await self._send_error(
                    client_writer,
                    407,
                    "Proxy Authentication Required",
                    "Valid project credentials required in Proxy-Authorization header",
                )
                return

            project = auth_result.project
            session_id = auth_result.sessid
            location = auth_result.location
            project_id = project.id
            logger.debug(
                "Authenticated project",
                project_id=project_id,
                project_name=project.name,
                client_addr=client_addr,
                session_id=session_id,
                location=location.key if location else None,
            )

            if auth_result.location_error:
                # A state or city without a country: nothing could serve it
                # faithfully, and routing it anywhere would be a silent widening.
                await self._send_error(client_writer, 400, "Bad Request", auth_result.location_error)
                await event_bus.publish(request_rejected, self, project_id=project_id, reason="invalid_location")
                return

            if method.upper() == "CONNECT":
                await self._handle_connect(
                    client_reader, client_writer, target, headers, project, session_id, location,
                )
            else:
                await self._handle_http(
                    client_reader, client_writer, method, target, version, headers, project_id,
                    session_id, location,
                )

        except asyncio.CancelledError:
            logger.debug("Client connection cancelled", client_addr=client_addr)
            raise
        except TimeoutError:
            logger.debug("Client connection timeout", client_addr=client_addr)
        except ConnectionResetError:
            logger.debug("Client connection reset", client_addr=client_addr)
        except Exception as e:
            logger.error("Error handling client", error=str(e), client_addr=client_addr)
        finally:
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except Exception:
                pass

    async def _read_headers(self, reader: asyncio.StreamReader) -> dict[str, str]:
        """Read HTTP headers from the stream."""
        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=self._timeout)
            if line in (b"\r\n", b"\n", b""):
                break
            line_str = line.decode("utf-8", errors="replace").strip()
            if ":" in line_str:
                key, value = line_str.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        return headers

    @staticmethod
    def _end_to_end_headers(
        headers: dict[str, str], hop_by_hop: frozenset[str]
    ) -> dict[str, str]:
        """Drop the headers that were addressed to the adjacent hop.

        Removes ``hop_by_hop`` plus every name the peer's own Connection
        header lists (RFC 9110 section 7.6.1), except body-framing names.
        Keys are expected lower-cased, as ``_read_headers`` produces them.
        """
        drop = set(hop_by_hop)
        for token in headers.get("connection", "").split(","):
            name = token.strip().lower()
            if name and name not in BODY_FRAMING_HEADERS:
                drop.add(name)
        return {k: v for k, v in headers.items() if k not in drop}

    async def _send_error(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        message: str,
        body: str = "",
        extra_headers: list[str] | None = None,
    ) -> None:
        """Send an HTTP error response. Silently ignores closed connections."""
        try:
            if writer.is_closing():
                return
            body_bytes = body.encode("utf-8")
            # Build headers
            headers = [
                f"HTTP/1.1 {status} {message}",
                f"Content-Length: {len(body_bytes)}",
            ]
            if status == 407:
                headers.append('Proxy-Authenticate: Basic realm="Proxy"')
            if extra_headers:
                headers.extend(extra_headers)
            response = "\r\n".join(headers) + "\r\n\r\n"

            writer.write(response.encode())
            if body_bytes:
                writer.write(body_bytes)
            await writer.drain()
        except (ConnectionError, BrokenPipeError, OSError):
            # Client already disconnected, nothing to do
            pass

    async def _handle_connect(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        target: str,
        headers: dict[str, str],
        project: Project,
        session_id: str | None = None,
        location: LocationTarget | None = None,
    ) -> None:
        """Handle HTTPS CONNECT tunneling."""
        # Parse target host:port before proxy selection (needed for domain filtering)
        if ":" in target:
            target_host, target_port_str = target.rsplit(":", 1)
            target_port = int(target_port_str)
        else:
            target_host = target
            target_port = 443  # Default HTTPS port

        # Select upstream proxy scoped to the authenticated project
        proxy = await self._get_upstream_proxy(
            project_id=project.id, session_id=session_id, target_host=target_host, location=location,
        )
        if not proxy:
            await self._reject_no_proxy(client_writer, project.id, target_host, session_id, location)
            return

        verified = await self._verify_exit(project, proxy, session_id, location, target_host, client_writer)
        if verified is None:
            return

        await self._relay(
            client_reader, client_writer, verified, project, target_host, target_port,
            established=b"HTTP/1.1 200 Connection Established\r\n\r\n",
            use_mitm=project.tls_mitm_mode != MitmMode.OFF and self._mitm_handler is not None,
        )

    async def _relay(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        proxy: Proxy,
        project: Project,
        target_host: str,
        target_port: int,
        *,
        established: bytes | None,
        use_mitm: bool,
        client_head: bytes = b"",
    ) -> None:
        """Open the upstream leg to ``target_host:target_port`` and relay until either side is done.

        The tail of a CONNECT and the whole of a transparent tunnel connection:
        the client has been authenticated and the proxy selected and verified.
        ``established`` is written to the client once the upstream leg is up
        (the CONNECT 200 line); None for a transparent client, which believes
        it is talking to the origin and must hear nothing from us. With
        ``use_mitm`` the MITM handler takes both legs over after that;
        ``client_head`` is whatever of the client's TLS stream the caller has
        already taken off the socket, which the handshake must be fed.
        """
        target = f"{target_host}:{target_port}"
        start_time = time.monotonic()
        success = False
        client_disconnected = False
        latency_ms = 0.0
        # Counts the tunnel's bytes as they flow; the completion event below
        # carries only what the meter has not reported yet.
        meter = self._proxy_manager.traffic_meter(proxy, project.id)
        upstream_writer: asyncio.StreamWriter | None = None

        try:
            # Connect through upstream proxy (handles all protocols)
            upstream_reader, upstream_writer = await self._connect_via_proxy(
                proxy, target_host, target_port
            )

            # Measure latency up to connection establishment (before tunneling)
            latency_ms = (time.monotonic() - start_time) * 1000

            if established is not None:
                client_writer.write(established)
                await client_writer.drain()
            success = True

            if use_mitm:
                # MITM handler takes ownership of upstream connection
                await self._mitm_handler.handle(  # type: ignore[union-attr]
                    client_reader, client_writer, target_host, target_port,
                    proxy, project,
                    upstream_reader=upstream_reader,
                    upstream_writer=upstream_writer,
                    meter=meter,
                    client_head=client_head,
                )
                upstream_writer = None  # MitmHandler/relay owns it now
            else:
                await self._tunnel(
                    client_reader,
                    client_writer,
                    upstream_reader,
                    upstream_writer,
                    meter=meter,
                )

        except TimeoutError:
            latency_ms = (time.monotonic() - start_time) * 1000
            await self._send_error(
                client_writer, 504, "Gateway Timeout", "Connection to upstream proxy timed out"
            )
        except ConnectionRefusedError:
            latency_ms = (time.monotonic() - start_time) * 1000
            await self._send_error(
                client_writer, 502, "Bad Gateway", "Upstream proxy refused connection"
            )
        except ConnectionError as e:
            latency_ms = (time.monotonic() - start_time) * 1000
            if client_writer.transport.is_closing():
                client_disconnected = True
                logger.debug("Client disconnected", target=target)
            else:
                logger.error("CONNECT error", error=str(e), target=target)
                await self._send_error(client_writer, 502, "Bad Gateway", str(e))
        except Exception as e:
            latency_ms = (time.monotonic() - start_time) * 1000
            if client_writer.transport.is_closing():
                client_disconnected = True
                logger.debug("Client disconnected", target=target)
            else:
                logger.error("CONNECT error", error=str(e), target=target)
                await self._send_error(client_writer, 502, "Bad Gateway", str(e))
        finally:
            if upstream_writer:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            if proxy and not client_disconnected:
                bytes_sent, bytes_received = meter.finish()
                await event_bus.publish(request_completed,
                    self,
                    proxy_id=proxy.id,
                    project_id=project.id,
                    success=success,
                    latency_ms=latency_ms,
                    bytes_sent=bytes_sent,
                    bytes_received=bytes_received,
                )
            else:
                # Nothing will report the remainder; hand it over now so the
                # bytes are not lost with the meter.
                meter.report()

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        meter: TrafficMeter | None = None,
    ) -> tuple[int, int]:
        """Bidirectional tunnel between client and upstream.

        Each direction runs independently until:
        - The reader returns EOF (remote closed their write side)
        - A write fails (remote closed their read side or connection lost)
        - An OS-level error occurs
        - The meter says stop (the connector's traffic limit, interrupt action)

        We wait for both directions to complete naturally, which correctly
        handles half-closed connections (e.g., client sends request, closes
        write side, but still reads the response). A direction stopped by
        the meter takes the other one down with it: the tunnel is being
        cut, not half-closed.

        Returns:
            Tuple of (bytes_sent, bytes_received) where bytes_sent is data
            sent to upstream (from client) and bytes_received is data
            received from upstream (to client). With a meter these are the
            same numbers the meter holds.
        """
        async def forward(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
            count: _ByteCounter,
        ) -> tuple[int, bool]:
            """Forward data; return (bytes transferred, stopped by the meter)."""
            total_bytes = 0
            try:
                while True:
                    data = await reader.read(BUFFER_SIZE)
                    if not data:
                        # Pass the half-close on: a peer that waits for our
                        # EOF before answering (or before closing itself)
                        # would otherwise wait forever. TLS transports
                        # cannot half-close and are left alone.
                        if writer.can_write_eof():
                            writer.write_eof()
                        break
                    total_bytes += len(data)
                    writer.write(data)
                    await writer.drain()
                    if count is not None and not count(len(data)):
                        return total_bytes, True
            except (ConnectionResetError, BrokenPipeError, OSError):
                # Connection closed or errored - exit gracefully
                pass
            return total_bytes, False

        count_sent: _ByteCounter = meter.add_sent if meter is not None else None
        count_received: _ByteCounter = meter.add_received if meter is not None else None
        task1 = asyncio.create_task(forward(client_reader, upstream_writer, count_sent))
        task2 = asyncio.create_task(forward(upstream_reader, client_writer, count_received))
        tasks: set[asyncio.Task[tuple[int, bool]]] = {task1, task2}

        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if any(t.result()[1] for t in done) and pending:
                    # Cut: the other direction must not wait for a natural end.
                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    pending = set()
        except asyncio.CancelledError:
            # If we're cancelled from outside, cancel both tasks
            task1.cancel()
            task2.cancel()
            # Wait for cleanup, suppressing exceptions
            await asyncio.gather(task1, task2, return_exceptions=True)
            raise

        bytes_sent = task1.result()[0] if not task1.cancelled() else 0
        bytes_received = task2.result()[0] if not task2.cancelled() else 0
        if meter is not None:
            # A cancelled direction never returned its count; the meter did.
            bytes_sent, bytes_received = meter.sent, meter.received
        return bytes_sent, bytes_received


    def _parse_http_url(self, url: str) -> tuple[str, int, str]:
        """Parse an HTTP URL into host, port, and path.

        Args:
            url: Full URL like http://example.com:8080/path or http://example.com/path

        Returns:
            Tuple of (host, port, path)
        """
        # Remove scheme
        if url.startswith("http://"):
            url = url[7:]
        elif url.startswith("https://"):
            url = url[8:]

        # Split path from host
        slash_idx = url.find("/")
        if slash_idx == -1:
            host_port = url
            path = "/"
        else:
            host_port = url[:slash_idx]
            path = url[slash_idx:]

        # Split port from host
        if ":" in host_port:
            host, port_str = host_port.rsplit(":", 1)
            port = int(port_str)
        else:
            host = host_port
            port = 80

        return host, port, path

    async def _handle_http(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        method: str,
        target: str,
        version: str,
        headers: dict[str, str],
        project_id: str,
        session_id: str | None = None,
        location: LocationTarget | None = None,
    ) -> None:
        """Handle regular HTTP request forwarding."""
        # Parse target host before proxy selection (needed for domain filtering)
        parsed_host, _, _ = self._parse_http_url(target)

        # Select upstream proxy scoped to the authenticated project
        proxy = await self._get_upstream_proxy(
            project_id=project_id, session_id=session_id, target_host=parsed_host, location=location,
        )
        if not proxy:
            await self._reject_no_proxy(client_writer, project_id, parsed_host, session_id, location)
            return

        project = self._proxy_manager.get_project(project_id)
        if project is not None:
            verified = await self._verify_exit(project, proxy, session_id, location, parsed_host, client_writer)
            if verified is None:
                return
            proxy = verified

        start_time = time.monotonic()
        success = False
        latency_ms = 0.0
        meter = self._proxy_manager.traffic_meter(proxy, project_id)
        upstream_writer: asyncio.StreamWriter | None = None

        # Check if this is a SOCKS proxy
        is_socks = proxy.protocol in (ProxyProtocol.SOCKS4, ProxyProtocol.SOCKS5)

        try:
            if is_socks:
                # For SOCKS: tunnel to target, then send normal HTTP request
                target_host, target_port, path = self._parse_http_url(target)
                upstream_reader, upstream_writer = await self._connect_via_proxy(
                    proxy, target_host, target_port
                )
                # Send request with relative path (direct to target)
                request_line = f"{method} {path} {version}\r\n"
            else:
                # For HTTP proxy: connect directly to proxy, send full URL
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(proxy.host, proxy.port),
                    timeout=self._timeout,
                )
                # Send request with full URL (proxy style)
                request_line = f"{method} {target} {version}\r\n"

            # At this point upstream_writer is always assigned
            assert upstream_writer is not None

            request_line_bytes = request_line.encode()
            upstream_writer.write(request_line_bytes)
            meter.add_sent(len(request_line_bytes))

            # Forward headers, adding proxy auth for HTTP proxies only
            # (SOCKS auth is handled during tunnel establishment)
            if not is_socks and proxy.username and proxy.password:
                credentials = base64.b64encode(
                    f"{proxy.username}:{proxy.password}".encode()
                ).decode()
                auth_header = f"Proxy-Authorization: Basic {credentials}\r\n".encode()
                upstream_writer.write(auth_header)
                meter.add_sent(len(auth_header))

            relayed = self._end_to_end_headers(headers, HOP_BY_HOP_REQUEST_HEADERS)
            # We serve one exchange per connection, so say so: an upstream
            # that honours it will close after the body instead of waiting
            # for a second request that never comes.
            relayed["connection"] = "close"
            for key, value in relayed.items():
                header_line = f"{key}: {value}\r\n".encode()
                upstream_writer.write(header_line)
                meter.add_sent(len(header_line))
            upstream_writer.write(b"\r\n")
            meter.add_sent(2)

            # Forward request body if present, in chunks so a large upload is
            # metered as it goes rather than buffered whole.
            content_length = int(headers.get("content-length", 0))
            remaining = content_length
            while remaining > 0:
                chunk = await asyncio.wait_for(
                    client_reader.read(min(BUFFER_SIZE, remaining)),
                    timeout=self._timeout,
                )
                if not chunk:
                    raise ConnectionError("Client closed the connection mid-body")
                remaining -= len(chunk)
                upstream_writer.write(chunk)
                await upstream_writer.drain()
                if not meter.add_sent(len(chunk)):
                    # Cut by the connector's traffic limit before any response
                    # was started, so the client can still be told why.
                    status = meter.limit_status
                    await self._send_error(
                        client_writer, status,
                        TRAFFIC_LIMIT_REASONS.get(status, "Bandwidth Limit Exceeded"),
                        "The connector serving this request has reached its traffic limit.",
                        extra_headers=[TRAFFIC_LIMIT_PROXY_STATUS],
                    )
                    return

            await upstream_writer.drain()

            # Read response status line - this marks successful proxy connection
            response_line = await asyncio.wait_for(
                upstream_reader.readline(),
                timeout=self._timeout,
            )
            meter.add_received(len(response_line))

            # Measure latency up to first response (connection establishment)
            latency_ms = (time.monotonic() - start_time) * 1000
            success = True

            client_writer.write(response_line)

            # Read response headers, then relay the end-to-end ones with the
            # vendor's original spelling. Hop-by-hop names (Connection,
            # Keep-Alive, Proxy-Authenticate...) describe the upstream leg
            # and are replaced by our own Connection: close.
            response_headers: dict[str, str] = {}
            raw_lines: list[tuple[str | None, bytes]] = []
            while True:
                line = await upstream_reader.readline()
                meter.add_received(len(line))
                if line in (b"\r\n", b"\n", b""):
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if ":" in line_str:
                    key, value = line_str.split(":", 1)
                    name = key.strip().lower()
                    response_headers[name] = value.strip()
                    raw_lines.append((name, line))
                else:
                    raw_lines.append((None, line))

            relayed_names = set(
                self._end_to_end_headers(response_headers, HOP_BY_HOP_RESPONSE_HEADERS)
            )
            for relayed_name, line in raw_lines:
                if relayed_name is None or relayed_name in relayed_names:
                    client_writer.write(line)
            client_writer.write(b"Connection: close\r\n\r\n")

            await client_writer.drain()

            # Forward response body (not included in latency measurement)
            resp_content_length = response_headers.get("content-length")
            transfer_encoding = response_headers.get("transfer-encoding", "")

            if transfer_encoding.lower() == "chunked":
                await self._forward_chunked(upstream_reader, client_writer, meter)
            elif resp_content_length:
                remaining = int(resp_content_length)
                while remaining > 0:
                    chunk = await upstream_reader.read(min(BUFFER_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    client_writer.write(chunk)
                    await client_writer.drain()
                    if not meter.add_received(len(chunk)):
                        # Interrupted by the connector's traffic limit: the
                        # client sees a truncated body and a closed connection.
                        break

        except TimeoutError:
            latency_ms = (time.monotonic() - start_time) * 1000
            await self._send_error(
                client_writer, 504, "Gateway Timeout", "Connection to upstream proxy timed out"
            )
        except ConnectionRefusedError:
            latency_ms = (time.monotonic() - start_time) * 1000
            await self._send_error(
                client_writer, 502, "Bad Gateway", "Upstream proxy refused connection"
            )
        except ConnectionError as e:
            latency_ms = (time.monotonic() - start_time) * 1000
            logger.error("HTTP forward error", error=str(e), target=target)
            await self._send_error(client_writer, 502, "Bad Gateway", str(e))
        except Exception as e:
            latency_ms = (time.monotonic() - start_time) * 1000
            logger.error("HTTP forward error", error=str(e), target=target)
            await self._send_error(client_writer, 502, "Bad Gateway", str(e))
        finally:
            if upstream_writer:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            if proxy:
                bytes_sent, bytes_received = meter.finish()
                await event_bus.publish(request_completed,
                    self,
                    proxy_id=proxy.id,
                    project_id=project_id,
                    success=success,
                    latency_ms=latency_ms,
                    bytes_sent=bytes_sent,
                    bytes_received=bytes_received,
                )

    async def _forward_chunked(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        meter: TrafficMeter | None = None,
    ) -> int:
        """Forward chunked transfer encoding and return total bytes read.

        Stops early, leaving the body incomplete, when the meter says the
        connector's traffic limit cut the transfer.
        """
        total_bytes = 0

        def count(n: int) -> bool:
            nonlocal total_bytes
            total_bytes += n
            return meter.add_received(n) if meter is not None else True

        while True:
            # Read chunk size line
            size_line = await reader.readline()
            writer.write(size_line)
            if not count(len(size_line)):
                break

            size_str = size_line.decode("utf-8", errors="replace").strip()
            chunk_size = int(size_str.split(";")[0], 16)

            if chunk_size == 0:
                # Final chunk - read trailing CRLF
                trailing = await reader.readline()
                writer.write(trailing)
                count(len(trailing))
                break

            # Read chunk data + CRLF in pieces, so a large chunk is metered
            # as it flows rather than buffered whole.
            remaining = chunk_size + 2
            stopped = False
            while remaining > 0:
                piece = await reader.readexactly(min(BUFFER_SIZE, remaining))
                remaining -= len(piece)
                writer.write(piece)
                await writer.drain()
                if not count(len(piece)):
                    stopped = True
                    break
            if stopped:
                break

        await writer.drain()
        return total_bytes

