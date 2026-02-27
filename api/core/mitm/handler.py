# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""MITM handler for TLS interception.

Intercepts HTTPS traffic by terminating TLS on the client side,
reading plaintext HTTP requests, logging headers, and relaying
requests upstream via a pluggable relay strategy.
"""

import asyncio
from typing import TYPE_CHECKING

import structlog

from api.core.tls_cert_manager import TLSCertManager

if TYPE_CHECKING:
    from api.core.mitm.base import MitmRelay
    from api.models.project import Project
    from api.models.proxy import Proxy

logger = structlog.get_logger()


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

        Upgrades the client connection to TLS, then enters a request loop
        that reads HTTP/1.1 requests, logs headers, and relays them via
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

        # Determine proxy URL
        proxy_url = proxy.url

        # Create relay strategy based on project settings
        relay: MitmRelay = create_relay(
            project,
            upstream_reader=upstream_reader,
            upstream_writer=upstream_writer,
            target_host=target_host,
        )

        try:
            # Request loop (handles HTTP keep-alive)
            while True:
                # Read request line
                try:
                    request_line_raw = await client_reader.readline()
                except (ConnectionResetError, BrokenPipeError, OSError):
                    break
                if not request_line_raw:
                    break

                request_line = request_line_raw.decode("utf-8", errors="replace").strip()
                if not request_line:
                    break

                bytes_sent += len(request_line_raw)

                # Parse request line
                parts = request_line.split()
                if len(parts) < 3:
                    break
                method, path, _version = parts[0], parts[1], parts[2]

                # Read headers until blank line
                headers: dict[str, str] = {}
                content_length = 0
                is_chunked = False

                while True:
                    try:
                        line_raw = await client_reader.readline()
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        return bytes_sent, bytes_received
                    if not line_raw:
                        return bytes_sent, bytes_received

                    bytes_sent += len(line_raw)
                    line = line_raw.decode("utf-8", errors="replace").strip()

                    if not line:
                        break  # End of headers

                    if ":" in line:
                        key, value = line.split(":", 1)
                        header_name = key.strip()
                        header_value = value.strip()
                        headers[header_name] = header_value

                        lower_name = header_name.lower()
                        if lower_name == "content-length":
                            content_length = int(header_value)
                        elif lower_name == "transfer-encoding" and "chunked" in header_value.lower():
                            is_chunked = True

                # Log the intercepted request
                logger.debug(
                    "MITM intercepted request",
                    target_host=target_host,
                    request_line=request_line,
                    headers=headers,
                    mode=project.tls_mitm_mode,
                )

                # Read request body if present
                body: bytes | None = None
                if is_chunked:
                    body = await _read_chunked_body(client_reader)
                    bytes_sent += len(body) if body else 0
                elif content_length > 0:
                    try:
                        body = await client_reader.readexactly(content_length)
                        bytes_sent += len(body)
                    except asyncio.IncompleteReadError:
                        break

                # Build full URL
                url = f"{base_url}{path}"

                # Remove hop-by-hop headers that shouldn't be forwarded
                forward_headers = {
                    k: v for k, v in headers.items()
                    if k.lower() not in ("connection", "proxy-connection", "keep-alive",
                                         "transfer-encoding")
                }

                # Relay request via the chosen strategy
                try:
                    status_code, reason, response_headers, response_body = (
                        await relay.send_request(
                            method, url, forward_headers, body, proxy_url,
                        )
                    )
                except Exception as e:
                    logger.debug("MITM upstream request failed", url=url, error=str(e))
                    # Send 502 to client
                    error_response = (
                        b"HTTP/1.1 502 Bad Gateway\r\n"
                        b"Content-Length: 0\r\n"
                        b"Connection: close\r\n\r\n"
                    )
                    try:
                        client_writer.write(error_response)
                        await client_writer.drain()
                        bytes_received += len(error_response)
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        pass
                    break

                # Write response back to client
                try:
                    # Status line
                    status_line = f"HTTP/1.1 {status_code} {reason}\r\n"
                    status_bytes = status_line.encode()
                    client_writer.write(status_bytes)
                    bytes_received += len(status_bytes)

                    # Response headers (skip transfer-encoding, we use content-length)
                    for name, value in response_headers.items():
                        if name.lower() in ("transfer-encoding", "content-length"):
                            continue
                        header_line = f"{name}: {value}\r\n".encode()
                        client_writer.write(header_line)
                        bytes_received += len(header_line)

                    # Set Content-Length
                    cl_header = f"Content-Length: {len(response_body)}\r\n".encode()
                    client_writer.write(cl_header)
                    bytes_received += len(cl_header)

                    # End of headers
                    client_writer.write(b"\r\n")
                    bytes_received += 2

                    # Response body
                    if response_body:
                        client_writer.write(response_body)
                        bytes_received += len(response_body)

                    await client_writer.drain()

                except (ConnectionResetError, BrokenPipeError, OSError):
                    break

        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            await relay.close()

        return bytes_sent, bytes_received


async def _read_chunked_body(reader: asyncio.StreamReader) -> bytes:
    """Read a chunked transfer-encoded body from the stream."""
    body = bytearray()
    while True:
        try:
            size_line = await reader.readline()
        except (ConnectionResetError, BrokenPipeError, OSError):
            break
        if not size_line:
            break

        size_str = size_line.decode("utf-8", errors="replace").strip()
        if not size_str:
            break

        try:
            chunk_size = int(size_str.split(";")[0], 16)
        except ValueError:
            break

        if chunk_size == 0:
            # Read trailing CRLF after the last chunk
            await reader.readline()
            break

        try:
            chunk_data = await reader.readexactly(chunk_size)
        except asyncio.IncompleteReadError:
            break
        body.extend(chunk_data)

        # Read CRLF after chunk data
        await reader.readline()

    return bytes(body)
