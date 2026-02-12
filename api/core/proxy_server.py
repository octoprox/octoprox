"""HTTP Proxy Server for Octoprox.

This module implements a real HTTP proxy server that can be used directly
by HTTP clients (e.g., via http_proxy environment variable).

It supports:
- HTTP CONNECT method for HTTPS tunneling
- Regular HTTP request forwarding
- Upstream proxy selection via ProxyManager strategies
"""

import asyncio
import time
from typing import TYPE_CHECKING

import structlog

from api.core.config import settings
from api.models.proxy import Proxy

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

    def _get_upstream_proxy(self, session_id: str | None = None) -> Proxy | None:
        """Select an upstream proxy using the configured strategy."""
        return self._proxy_manager.select_proxy(session_id)

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

            if method.upper() == "CONNECT":
                await self._handle_connect(
                    client_reader, client_writer, target, headers
                )
            else:
                await self._handle_http(
                    client_reader, client_writer, method, target, version, headers
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
        self, writer: asyncio.StreamWriter, status: int, message: str
    ) -> None:
        """Send an HTTP error response."""
        response = f"HTTP/1.1 {status} {message}\r\nContent-Length: 0\r\n\r\n"
        writer.write(response.encode())
        await writer.drain()

    async def _handle_connect(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        target: str,
        headers: dict[str, str],
    ) -> None:
        """Handle HTTPS CONNECT tunneling."""
        # Select upstream proxy
        proxy = self._get_upstream_proxy()
        if not proxy:
            await self._send_error(client_writer, 503, "No upstream proxy available")
            return

        start_time = time.monotonic()
        success = False
        latency_ms = 0.0

        try:
            # Connect to upstream proxy
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(proxy.host, proxy.port),
                timeout=self._timeout,
            )

            try:
                # Send CONNECT to upstream proxy
                connect_req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"

                # Add proxy authentication if needed
                if proxy.username and proxy.password:
                    import base64
                    credentials = base64.b64encode(
                        f"{proxy.username}:{proxy.password}".encode()
                    ).decode()
                    connect_req += f"Proxy-Authorization: Basic {credentials}\r\n"

                connect_req += "\r\n"
                upstream_writer.write(connect_req.encode())
                await upstream_writer.drain()

                # Read response from upstream proxy
                response_line = await asyncio.wait_for(
                    upstream_reader.readline(),
                    timeout=self._timeout,
                )
                response_str = response_line.decode("utf-8", errors="replace").strip()

                # Read and discard upstream proxy response headers
                while True:
                    line = await upstream_reader.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break

                # Measure latency up to connection establishment (before tunneling)
                latency_ms = (time.monotonic() - start_time) * 1000

                # Check if upstream proxy accepted the CONNECT
                if "200" in response_str:
                    # Send 200 to client
                    client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await client_writer.drain()

                    success = True

                    # Start bidirectional tunnel (not included in latency measurement)
                    await self._tunnel(
                        client_reader, client_writer,
                        upstream_reader, upstream_writer,
                    )
                else:
                    # Forward error to client
                    client_writer.write(response_line)
                    await client_writer.drain()

            finally:
                upstream_writer.close()
                await upstream_writer.wait_closed()

        except TimeoutError:
            latency_ms = (time.monotonic() - start_time) * 1000
            await self._send_error(client_writer, 504, "Gateway Timeout")
        except ConnectionRefusedError:
            latency_ms = (time.monotonic() - start_time) * 1000
            await self._send_error(client_writer, 502, "Bad Gateway")
        except Exception as e:
            latency_ms = (time.monotonic() - start_time) * 1000
            logger.error("CONNECT error", error=str(e), target=target)
            await self._send_error(client_writer, 502, "Bad Gateway")
        finally:
            if proxy:
                await self._proxy_manager.update_proxy_stats(proxy.id, success, latency_ms)

    async def _tunnel(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        """Bidirectional tunnel between client and upstream.

        Each direction runs independently until:
        - The reader returns EOF (remote closed their write side)
        - A write fails (remote closed their read side or connection lost)
        - An OS-level error occurs

        We wait for both directions to complete naturally, which correctly
        handles half-closed connections (e.g., client sends request, closes
        write side, but still reads the response).
        """

        async def forward(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            try:
                while True:
                    data = await reader.read(BUFFER_SIZE)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                # Connection closed or errored - exit gracefully
                pass

        task1 = asyncio.create_task(forward(client_reader, upstream_writer))
        task2 = asyncio.create_task(forward(upstream_reader, client_writer))

        try:
            # Wait for both directions to complete
            await asyncio.gather(task1, task2)
        except asyncio.CancelledError:
            # If we're cancelled from outside, cancel both tasks
            task1.cancel()
            task2.cancel()
            # Wait for cleanup, suppressing exceptions
            await asyncio.gather(task1, task2, return_exceptions=True)
            raise


    async def _handle_http(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        method: str,
        target: str,
        version: str,
        headers: dict[str, str],
    ) -> None:
        """Handle regular HTTP request forwarding."""
        # Select upstream proxy
        proxy = self._get_upstream_proxy()
        if not proxy:
            await self._send_error(client_writer, 503, "No upstream proxy available")
            return

        start_time = time.monotonic()
        success = False
        latency_ms = 0.0

        try:
            # Connect to upstream proxy
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(proxy.host, proxy.port),
                timeout=self._timeout,
            )

            try:
                # Build request to send to upstream proxy
                # For HTTP proxies, we send the full URL
                request_line = f"{method} {target} {version}\r\n"
                upstream_writer.write(request_line.encode())

                # Forward headers, adding proxy auth if needed
                if proxy.username and proxy.password:
                    import base64
                    credentials = base64.b64encode(
                        f"{proxy.username}:{proxy.password}".encode()
                    ).decode()
                    upstream_writer.write(
                        f"Proxy-Authorization: Basic {credentials}\r\n".encode()
                    )

                for key, value in headers.items():
                    upstream_writer.write(f"{key}: {value}\r\n".encode())
                upstream_writer.write(b"\r\n")

                # Forward request body if present
                content_length = int(headers.get("content-length", 0))
                if content_length > 0:
                    body = await asyncio.wait_for(
                        client_reader.readexactly(content_length),
                        timeout=self._timeout,
                    )
                    upstream_writer.write(body)

                await upstream_writer.drain()

                # Read response status line - this marks successful proxy connection
                response_line = await asyncio.wait_for(
                    upstream_reader.readline(),
                    timeout=self._timeout,
                )

                # Measure latency up to first response (connection establishment)
                latency_ms = (time.monotonic() - start_time) * 1000
                success = True

                client_writer.write(response_line)

                # Read and forward response headers
                response_headers: dict[str, str] = {}
                while True:
                    line = await upstream_reader.readline()
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
                    await self._forward_chunked(upstream_reader, client_writer)
                elif resp_content_length:
                    body = await upstream_reader.readexactly(int(resp_content_length))
                    client_writer.write(body)
                    await client_writer.drain()

            finally:
                upstream_writer.close()
                await upstream_writer.wait_closed()

        except TimeoutError:
            latency_ms = (time.monotonic() - start_time) * 1000
            await self._send_error(client_writer, 504, "Gateway Timeout")
        except ConnectionRefusedError:
            latency_ms = (time.monotonic() - start_time) * 1000
            await self._send_error(client_writer, 502, "Bad Gateway")
        except Exception as e:
            latency_ms = (time.monotonic() - start_time) * 1000
            logger.error("HTTP forward error", error=str(e), target=target)
            await self._send_error(client_writer, 502, "Bad Gateway")
        finally:
            if proxy:
                await self._proxy_manager.update_proxy_stats(proxy.id, success, latency_ms)

    async def _forward_chunked(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Forward chunked transfer encoding."""
        while True:
            # Read chunk size line
            size_line = await reader.readline()
            writer.write(size_line)

            size_str = size_line.decode("utf-8", errors="replace").strip()
            chunk_size = int(size_str.split(";")[0], 16)

            if chunk_size == 0:
                # Final chunk - read trailing CRLF
                trailing = await reader.readline()
                writer.write(trailing)
                break

            # Read chunk data + CRLF
            chunk_data = await reader.readexactly(chunk_size + 2)
            writer.write(chunk_data)

        await writer.drain()

