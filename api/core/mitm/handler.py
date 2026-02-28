# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""MITM handler for TLS interception.

Intercepts HTTPS traffic by terminating TLS on the client side,
reading plaintext HTTP requests, logging headers, and relaying
requests upstream via a pluggable relay strategy.

Uses h11 as a sans-I/O HTTP/1.1 state machine to handle protocol
parsing and serialization, replacing manual request/response parsing.
"""

import asyncio
import contextlib
from typing import TYPE_CHECKING

import h11
import structlog

from api.core.tls_cert_manager import TLSCertManager

if TYPE_CHECKING:
    from api.core.mitm.base import MitmRelay
    from api.models.project import Project
    from api.models.proxy import Proxy

logger = structlog.get_logger()

# Read buffer size for client-facing connection
_READ_SIZE = 65536


class MitmHandler:
    """Handles MITM interception for a single CONNECT tunnel.

    Upgrades the client connection to TLS (presenting a generated cert),
    reads HTTP/1.1 requests, logs headers, and relays them to the target
    via a pluggable relay strategy (plain, curl_cffi, or rnet).
    """

    def __init__(self, cert_manager: TLSCertManager) -> None:
        self._cert_manager = cert_manager

    async def handle(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        target_host: str,
        target_port: int,
        proxy: "Proxy",
        project: "Project",
        upstream_reader: asyncio.StreamReader | None = None,
        upstream_writer: asyncio.StreamWriter | None = None,
    ) -> tuple[int, int]:
        """Run MITM interception on the client connection.

        Upgrades the client connection to TLS, then uses h11 to parse
        HTTP/1.1 requests and serialize responses, relaying them via
        the appropriate strategy based on project settings.

        Returns:
            Tuple of (bytes_sent, bytes_received).
        """
        from api.core.mitm import create_relay

        bytes_sent = 0
        bytes_received = 0

        # Upgrade client connection to TLS (Octoprox as server)
        server_ssl_ctx = self._cert_manager.get_server_ssl_context(target_host)
        loop = asyncio.get_running_loop()
        transport = client_writer.transport
        protocol = transport.get_protocol()

        try:
            new_transport = await loop.start_tls(
                transport,
                protocol,
                server_ssl_ctx,
                server_side=True,
            )
        except Exception as e:
            logger.debug("MITM TLS handshake failed", target_host=target_host, error=str(e))
            return bytes_sent, bytes_received

        # Update the writer's transport to the TLS-wrapped one
        client_writer._transport = new_transport  # type: ignore[attr-defined]

        # Build base URL for the target
        if target_port == 443:
            base_url = f"https://{target_host}"
        else:
            base_url = f"https://{target_host}:{target_port}"

        proxy_url = proxy.url

        # Create relay strategy based on project settings
        relay: MitmRelay = create_relay(
            project,
            upstream_reader=upstream_reader,
            upstream_writer=upstream_writer,
            target_host=target_host,
        )

        conn = h11.Connection(our_role=h11.SERVER)

        try:
            while True:
                # Read a complete request using h11 (Request + optional Data + EndOfMessage)
                request_event, body, read_bytes = await _read_request(conn, client_reader)
                bytes_sent += read_bytes

                if request_event is None:
                    break

                # Extract request details from h11 event
                method = request_event.method.decode("ascii")
                path = request_event.target.decode("ascii")
                headers: dict[str, str] = {
                    name.decode("latin-1"): value.decode("latin-1")
                    for name, value in request_event.headers
                }

                logger.debug(
                    "MITM intercepted request",
                    target_host=target_host,
                    method=method,
                    path=path,
                    headers=headers,
                    mode=project.tls_mitm_mode,
                )

                url = f"{base_url}{path}"

                # Remove hop-by-hop headers that shouldn't be forwarded
                forward_headers = {
                    k: v for k, v in headers.items()
                    if k.lower() not in (
                        "connection", "proxy-connection", "keep-alive",
                        "transfer-encoding",
                    )
                }

                # Relay request via the chosen strategy
                try:
                    status_code, _reason, response_headers, response_body = (
                        await relay.send_request(method, url, forward_headers, body, proxy_url)
                    )
                except Exception as e:
                    logger.debug("MITM upstream request failed", url=url, error=str(e))
                    written = _send_via_h11(conn, client_writer, 502, {}, b"")
                    with contextlib.suppress(ConnectionResetError, BrokenPipeError, OSError):
                        await client_writer.drain()
                    bytes_received += written
                    break

                # Filter response headers: drop transfer-encoding/content-length
                # (h11 manages content-length automatically)
                filtered: dict[str, str] = {
                    k: v for k, v in response_headers.items()
                    if k.lower() not in ("transfer-encoding", "content-length")
                }

                try:
                    written = _send_via_h11(
                        conn, client_writer, status_code, filtered, response_body,
                    )
                    await client_writer.drain()
                    bytes_received += written
                except (ConnectionResetError, BrokenPipeError, OSError):
                    break

                # Prepare for next request (keep-alive)
                try:
                    conn.start_next_cycle()
                except h11.LocalProtocolError:
                    break

        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            await relay.close()

        return bytes_sent, bytes_received


async def _read_request(
    conn: h11.Connection,
    reader: asyncio.StreamReader,
) -> tuple[h11.Request | None, bytes | None, int]:
    """Read a full HTTP/1.1 request via h11.

    Returns (request_event, body_bytes, bytes_read_from_socket).
    Returns (None, None, bytes_read) if the connection is closed or an
    unrecoverable event is encountered.
    """
    request_event: h11.Request | None = None
    body_parts: list[bytes] = []
    total_read = 0

    while True:
        event = conn.next_event()

        if event is h11.NEED_DATA:
            try:
                data = await reader.read(_READ_SIZE)
            except (ConnectionResetError, BrokenPipeError, OSError):
                return None, None, total_read
            if not data:
                return None, None, total_read
            total_read += len(data)
            conn.receive_data(data)
            continue

        if isinstance(event, h11.Request):
            request_event = event
            continue

        if isinstance(event, h11.Data):
            body_parts.append(bytes(event.data))
            continue

        if isinstance(event, h11.EndOfMessage):
            body = b"".join(body_parts) if body_parts else None
            return request_event, body, total_read

        # ConnectionClosed, PAUSED, or other sentinel — stop
        return None, None, total_read


def _send_via_h11(
    conn: h11.Connection,
    writer: asyncio.StreamWriter,
    status_code: int,
    headers: dict[str, str],
    body: bytes,
) -> int:
    """Serialize and write a full HTTP/1.1 response via h11.

    Returns the number of bytes written.
    """
    h11_headers: list[tuple[str, str]] = list(headers.items())
    h11_headers.append(("content-length", str(len(body))))

    out = conn.send(h11.Response(status_code=status_code, headers=h11_headers)) or b""
    if body:
        out += conn.send(h11.Data(data=body)) or b""
    out += conn.send(h11.EndOfMessage()) or b""

    writer.write(out)
    return len(out)
