"""HTTP Proxy Server for Octoprox.

This module implements a real HTTP proxy server that can be used directly
by HTTP clients (e.g., via http_proxy environment variable).

It supports:
- HTTP CONNECT method for HTTPS tunneling
- Regular HTTP request forwarding
- Upstream proxy selection via ProxyManager strategies
- Project-based authentication via HTTP Basic Auth (Proxy-Authorization header)
"""

import asyncio
import base64
import time
from typing import TYPE_CHECKING

import structlog
from python_socks.async_.asyncio import Proxy as SocksProxy

from api.core.config import settings
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol

if TYPE_CHECKING:
    from api.core.proxy_manager import ProxyManager

logger = structlog.get_logger()

# Buffer size for tunneling
BUFFER_SIZE = 65536


class ProxyServer:
    """HTTP Proxy Server that forwards requests through managed upstream proxies."""

    def __init__(self, proxy_manager: "ProxyManager") -> None:
        self._proxy_manager = proxy_manager
        self._server: asyncio.Server | None = None
        self._host = settings.host
        self._port = settings.proxy_port
        self._timeout = settings.connection_timeout
        self._client_tasks: set[asyncio.Task[None]] = set()

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
            await self._server.wait_closed()

            # Cancel all active client tasks
            if self._client_tasks:
                logger.info(
                    "Cancelling active client connections",
                    count=len(self._client_tasks),
                )
                for task in self._client_tasks:
                    task.cancel()

                # Wait for all tasks to complete with a timeout
                await asyncio.gather(*self._client_tasks, return_exceptions=True)
                self._client_tasks.clear()

            logger.info("Proxy server stopped")

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

    def _get_upstream_proxy(
        self, project_id: str | None = None, session_id: str | None = None
    ) -> Proxy | None:
        """Select an upstream proxy using the configured strategy.

        Args:
            project_id: If provided, selects from project-scoped proxies using
                        the project's routing strategy.
            session_id: Session identifier for sticky routing.

        Returns:
            Selected proxy or None if no healthy proxies available.
        """
        if project_id:
            return self._proxy_manager.select_proxy_for_project(project_id, session_id)
        return self._proxy_manager.select_proxy(session_id)

    def _authenticate_project(self, headers: dict[str, str]) -> Project | None:
        """Authenticate a client request using Proxy-Authorization header.

        Expects HTTP Basic Auth in the Proxy-Authorization header with
        project username and password.

        Args:
            headers: Request headers (lowercase keys)

        Returns:
            Authenticated Project or None if authentication fails.
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

        # Look up project by username
        project = self._proxy_manager.get_project_by_username(username)
        if not project:
            logger.debug("Project not found for username", username=username)
            return None

        # Verify password (plain text comparison as per requirements)
        if project.password != password:
            logger.debug("Invalid password for project", project_id=project.id)
            return None

        return project

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
        # Create proxy instance from URL (handles all protocol types)
        socks_proxy = SocksProxy.from_url(proxy.url)

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

        # Extract client IP for sticky session routing
        client_ip: str | None = None
        if client_addr and isinstance(client_addr, tuple) and len(client_addr) >= 1:
            client_ip = str(client_addr[0])

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
            project = self._authenticate_project(headers)
            logger.debug("Authenticated project", headers=headers, project=project)
            if not project:
                await self._send_error(
                    client_writer,
                    407,
                    "Proxy Authentication Required",
                    "Valid project credentials required in Proxy-Authorization header",
                )
                return

            project_id = project.id
            logger.debug(
                "Authenticated project",
                project_id=project_id,
                project_name=project.name,
                client_addr=client_addr,
            )

            if method.upper() == "CONNECT":
                await self._handle_connect(
                    client_reader, client_writer, target, headers, project_id, client_ip
                )
            else:
                await self._handle_http(
                    client_reader, client_writer, method, target, version, headers, project_id, client_ip
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

    async def _send_error(
        self, writer: asyncio.StreamWriter, status: int, message: str, body: str = ""
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
        project_id: str,
        client_ip: str | None = None,
    ) -> None:
        """Handle HTTPS CONNECT tunneling."""
        # Select upstream proxy scoped to the authenticated project
        proxy = self._get_upstream_proxy(project_id=project_id, session_id=client_ip)
        if not proxy:
            await self._send_error(client_writer, 503, "No upstream proxy available for project")
            return

        # Parse target host:port
        if ":" in target:
            target_host, target_port_str = target.rsplit(":", 1)
            target_port = int(target_port_str)
        else:
            target_host = target
            target_port = 443  # Default HTTPS port

        start_time = time.monotonic()
        success = False
        latency_ms = 0.0
        bytes_sent = 0
        bytes_received = 0
        upstream_writer: asyncio.StreamWriter | None = None

        try:
            # Connect through upstream proxy (handles all protocols)
            upstream_reader, upstream_writer = await self._connect_via_proxy(
                proxy, target_host, target_port
            )

            # Measure latency up to connection establishment (before tunneling)
            latency_ms = (time.monotonic() - start_time) * 1000

            # Send 200 to client
            client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_writer.drain()

            success = True

            # Start bidirectional tunnel (not included in latency measurement)
            bytes_sent, bytes_received = await self._tunnel(
                client_reader,
                client_writer,
                upstream_reader,
                upstream_writer,
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
            logger.error("CONNECT error", error=str(e), target=target)
            await self._send_error(client_writer, 502, "Bad Gateway", str(e))
        except Exception as e:
            latency_ms = (time.monotonic() - start_time) * 1000
            logger.error("CONNECT error", error=str(e), target=target)
            await self._send_error(client_writer, 502, "Bad Gateway", str(e))
        finally:
            if upstream_writer:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            if proxy:
                await self._proxy_manager.update_proxy_stats(
                    proxy.id, success, latency_ms, bytes_sent, bytes_received
                )

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> tuple[int, int]:
        """Bidirectional tunnel between client and upstream.

        Each direction runs independently until:
        - The reader returns EOF (remote closed their write side)
        - A write fails (remote closed their read side or connection lost)
        - An OS-level error occurs

        We wait for both directions to complete naturally, which correctly
        handles half-closed connections (e.g., client sends request, closes
        write side, but still reads the response).

        Returns:
            Tuple of (bytes_sent, bytes_received) where bytes_sent is data
            sent to upstream (from client) and bytes_received is data
            received from upstream (to client).
        """
        bytes_sent = 0
        bytes_received = 0

        async def forward(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> int:
            """Forward data and return total bytes transferred."""
            total_bytes = 0
            try:
                while True:
                    data = await reader.read(BUFFER_SIZE)
                    if not data:
                        break
                    total_bytes += len(data)
                    writer.write(data)
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                # Connection closed or errored - exit gracefully
                pass
            return total_bytes

        task1 = asyncio.create_task(forward(client_reader, upstream_writer))
        task2 = asyncio.create_task(forward(upstream_reader, client_writer))

        try:
            # Wait for both directions to complete
            results = await asyncio.gather(task1, task2)
            bytes_sent = results[0]  # client -> upstream
            bytes_received = results[1]  # upstream -> client
        except asyncio.CancelledError:
            # If we're cancelled from outside, cancel both tasks
            task1.cancel()
            task2.cancel()
            # Wait for cleanup, suppressing exceptions
            await asyncio.gather(task1, task2, return_exceptions=True)
            raise

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
        client_ip: str | None = None,
    ) -> None:
        """Handle regular HTTP request forwarding."""
        # Select upstream proxy scoped to the authenticated project
        proxy = self._get_upstream_proxy(project_id=project_id, session_id=client_ip)
        if not proxy:
            await self._send_error(client_writer, 503, "No upstream proxy available for project")
            return

        start_time = time.monotonic()
        success = False
        latency_ms = 0.0
        bytes_sent = 0
        bytes_received = 0
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

            request_line_bytes = request_line.encode()
            upstream_writer.write(request_line_bytes)
            bytes_sent += len(request_line_bytes)

            # Forward headers, adding proxy auth for HTTP proxies only
            # (SOCKS auth is handled during tunnel establishment)
            if not is_socks and proxy.username and proxy.password:
                credentials = base64.b64encode(
                    f"{proxy.username}:{proxy.password}".encode()
                ).decode()
                auth_header = f"Proxy-Authorization: Basic {credentials}\r\n".encode()
                upstream_writer.write(auth_header)
                bytes_sent += len(auth_header)

            for key, value in headers.items():
                header_line = f"{key}: {value}\r\n".encode()
                upstream_writer.write(header_line)
                bytes_sent += len(header_line)
            upstream_writer.write(b"\r\n")
            bytes_sent += 2

            # Forward request body if present
            content_length = int(headers.get("content-length", 0))
            if content_length > 0:
                body = await asyncio.wait_for(
                    client_reader.readexactly(content_length),
                    timeout=self._timeout,
                )
                upstream_writer.write(body)
                bytes_sent += len(body)

            await upstream_writer.drain()

            # Read response status line - this marks successful proxy connection
            response_line = await asyncio.wait_for(
                upstream_reader.readline(),
                timeout=self._timeout,
            )
            bytes_received += len(response_line)

            # Measure latency up to first response (connection establishment)
            latency_ms = (time.monotonic() - start_time) * 1000
            success = True

            client_writer.write(response_line)

            # Read and forward response headers
            response_headers: dict[str, str] = {}
            while True:
                line = await upstream_reader.readline()
                bytes_received += len(line)
                client_writer.write(line)
                if line in (b"\r\n", b"\n", b""):
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if ":" in line_str:
                    key, value = line_str.split(":", 1)
                    response_headers[key.strip().lower()] = value.strip()

            await client_writer.drain()

            # Forward response body (not included in latency measurement)
            resp_content_length = response_headers.get("content-length")
            transfer_encoding = response_headers.get("transfer-encoding", "")

            if transfer_encoding.lower() == "chunked":
                chunked_bytes = await self._forward_chunked(upstream_reader, client_writer)
                bytes_received += chunked_bytes
            elif resp_content_length:
                body = await upstream_reader.readexactly(int(resp_content_length))
                bytes_received += len(body)
                client_writer.write(body)
                await client_writer.drain()

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
                await self._proxy_manager.update_proxy_stats(
                    proxy.id, success, latency_ms, bytes_sent, bytes_received
                )

    async def _forward_chunked(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> int:
        """Forward chunked transfer encoding and return total bytes read."""
        total_bytes = 0
        while True:
            # Read chunk size line
            size_line = await reader.readline()
            total_bytes += len(size_line)
            writer.write(size_line)

            size_str = size_line.decode("utf-8", errors="replace").strip()
            chunk_size = int(size_str.split(";")[0], 16)

            if chunk_size == 0:
                # Final chunk - read trailing CRLF
                trailing = await reader.readline()
                total_bytes += len(trailing)
                writer.write(trailing)
                break

            # Read chunk data + CRLF
            chunk_data = await reader.readexactly(chunk_size + 2)
            total_bytes += len(chunk_data)
            writer.write(chunk_data)

        await writer.drain()
        return total_bytes

