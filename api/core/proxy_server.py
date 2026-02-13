"""HTTP Proxy Server for Octoprox.

This module implements a real HTTP proxy server that can be used directly
by HTTP clients (e.g., via http_proxy environment variable).

It supports:
- HTTP CONNECT method for HTTPS tunneling
- Regular HTTP request forwarding
- Upstream proxy selection via ProxyManager strategies
"""

import asyncio
import socket
import struct
import time
from typing import TYPE_CHECKING

import structlog

from api.core.config import settings
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

    def _get_upstream_proxy(self, session_id: str | None = None) -> Proxy | None:
        """Select an upstream proxy using the configured strategy."""
        return self._proxy_manager.select_proxy(session_id)

    async def _connect_via_socks5(
        self,
        proxy: Proxy,
        target_host: str,
        target_port: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Perform SOCKS5 handshake to establish tunnel (RFC 1928).

        Args:
            proxy: The SOCKS5 proxy with optional credentials
            target_host: Destination hostname or IP
            target_port: Destination port
            reader: Stream reader connected to proxy
            writer: Stream writer connected to proxy

        Raises:
            ConnectionError: If handshake fails
        """
        # Greeting: version + auth methods
        if proxy.username and proxy.password:
            # Offer no-auth (0x00) and username/password (0x02)
            writer.write(b"\x05\x02\x00\x02")
        else:
            # Offer only no-auth
            writer.write(b"\x05\x01\x00")
        await writer.drain()

        # Receive auth method selection
        response = await asyncio.wait_for(reader.readexactly(2), timeout=self._timeout)
        version, auth_method = response[0], response[1]

        if version != 0x05:
            raise ConnectionError(f"SOCKS5: Invalid version {version}")

        if auth_method == 0xFF:
            raise ConnectionError("SOCKS5: No acceptable auth method")

        # Handle username/password auth (RFC 1929)
        if auth_method == 0x02:
            if not proxy.username or not proxy.password:
                raise ConnectionError("SOCKS5: Server requires auth but no credentials")

            username_bytes = proxy.username.encode("utf-8")
            password_bytes = proxy.password.encode("utf-8")
            auth_request = (
                b"\x01"
                + bytes([len(username_bytes)])
                + username_bytes
                + bytes([len(password_bytes)])
                + password_bytes
            )
            writer.write(auth_request)
            await writer.drain()

            auth_response = await asyncio.wait_for(
                reader.readexactly(2), timeout=self._timeout
            )
            if auth_response[1] != 0x00:
                raise ConnectionError("SOCKS5: Authentication failed")

        # Send connect request
        # Format: VER(1) + CMD(1) + RSV(1) + ATYP(1) + DST.ADDR(var) + DST.PORT(2)
        connect_request = b"\x05\x01\x00"  # version, connect command, reserved

        # Try to parse as IP address first
        try:
            ip_bytes = socket.inet_aton(target_host)
            connect_request += b"\x01" + ip_bytes  # IPv4
        except OSError:
            # It's a domain name
            host_bytes = target_host.encode("utf-8")
            connect_request += b"\x03" + bytes([len(host_bytes)]) + host_bytes

        connect_request += struct.pack("!H", target_port)
        writer.write(connect_request)
        await writer.drain()

        # Receive connect response
        # Format: VER(1) + REP(1) + RSV(1) + ATYP(1) + BND.ADDR(var) + BND.PORT(2)
        response = await asyncio.wait_for(reader.readexactly(4), timeout=self._timeout)
        version, reply, _, addr_type = response

        if version != 0x05:
            raise ConnectionError(f"SOCKS5: Invalid response version {version}")

        if reply != 0x00:
            error_messages = {
                0x01: "General failure",
                0x02: "Connection not allowed",
                0x03: "Network unreachable",
                0x04: "Host unreachable",
                0x05: "Connection refused",
                0x06: "TTL expired",
                0x07: "Command not supported",
                0x08: "Address type not supported",
            }
            msg = error_messages.get(reply, f"Unknown error {reply}")
            raise ConnectionError(f"SOCKS5: {msg}")

        # Read bound address (we don't use it, but must consume it)
        if addr_type == 0x01:  # IPv4
            await reader.readexactly(4)
        elif addr_type == 0x03:  # Domain
            domain_len = (await reader.readexactly(1))[0]
            await reader.readexactly(domain_len)
        elif addr_type == 0x04:  # IPv6
            await reader.readexactly(16)

        # Read bound port
        await reader.readexactly(2)

        # Tunnel is now established

    async def _connect_via_socks4(
        self,
        proxy: Proxy,
        target_host: str,
        target_port: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Perform SOCKS4/4a handshake to establish tunnel.

        Uses SOCKS4a extension for domain names (sends 0.0.0.x as IP
        and appends hostname after userid).

        Args:
            proxy: The SOCKS4 proxy (username used as userid if provided)
            target_host: Destination hostname or IP
            target_port: Destination port
            reader: Stream reader connected to proxy
            writer: Stream writer connected to proxy

        Raises:
            ConnectionError: If handshake fails
        """
        # SOCKS4 request format:
        # VN(1) + CD(1) + DSTPORT(2) + DSTIP(4) + USERID(var) + NULL(1)
        # For SOCKS4a with domain: DSTIP=0.0.0.x + USERID + NULL + HOSTNAME + NULL

        userid = (proxy.username or "").encode("utf-8")

        # Try to resolve as IP first
        try:
            ip_bytes = socket.inet_aton(target_host)
            use_socks4a = False
        except OSError:
            # Use SOCKS4a for domain names
            ip_bytes = b"\x00\x00\x00\x01"  # 0.0.0.1 signals SOCKS4a
            use_socks4a = True

        request = (
            b"\x04\x01"  # version 4, connect command
            + struct.pack("!H", target_port)
            + ip_bytes
            + userid
            + b"\x00"
        )

        if use_socks4a:
            request += target_host.encode("utf-8") + b"\x00"

        writer.write(request)
        await writer.drain()

        # Response format: VN(1) + CD(1) + DSTPORT(2) + DSTIP(4)
        response = await asyncio.wait_for(reader.readexactly(8), timeout=self._timeout)
        reply_code = response[1]

        if reply_code != 0x5A:  # 90 = request granted
            error_messages = {
                0x5B: "Request rejected or failed",
                0x5C: "Request failed - client not running identd",
                0x5D: "Request failed - identd could not confirm user",
            }
            msg = error_messages.get(reply_code, f"Unknown error {reply_code}")
            raise ConnectionError(f"SOCKS4: {msg}")

        # Tunnel is now established

    async def _connect_via_http(
        self,
        proxy: Proxy,
        target_host: str,
        target_port: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Perform HTTP CONNECT to establish tunnel through HTTP proxy.

        Args:
            proxy: The HTTP/HTTPS proxy with optional credentials
            target_host: Destination hostname or IP
            target_port: Destination port
            reader: Stream reader connected to proxy
            writer: Stream writer connected to proxy

        Raises:
            ConnectionError: If CONNECT request fails
        """
        target = f"{target_host}:{target_port}"
        connect_req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"

        # Add proxy authentication if needed
        if proxy.username and proxy.password:
            import base64

            credentials = base64.b64encode(
                f"{proxy.username}:{proxy.password}".encode()
            ).decode()
            connect_req += f"Proxy-Authorization: Basic {credentials}\r\n"

        connect_req += "\r\n"
        writer.write(connect_req.encode())
        await writer.drain()

        # Read response from upstream proxy
        response_line = await asyncio.wait_for(
            reader.readline(),
            timeout=self._timeout,
        )
        response_str = response_line.decode("utf-8", errors="replace").strip()

        # Read and discard response headers
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break

        # Check if proxy accepted the CONNECT
        if "200" not in response_str:
            raise ConnectionError(f"HTTP CONNECT failed: {response_str}")

        # Tunnel is now established

    async def _connect_via_proxy(
        self,
        proxy: Proxy,
        target_host: str,
        target_port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Connect to target through upstream proxy.

        Establishes a TCP connection to the proxy and performs the
        appropriate handshake based on proxy protocol.

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
        # Connect to the proxy server
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy.host, proxy.port),
            timeout=self._timeout,
        )

        try:
            # Perform protocol-specific handshake
            if proxy.protocol == ProxyProtocol.SOCKS5:
                await self._connect_via_socks5(
                    proxy, target_host, target_port, reader, writer
                )
            elif proxy.protocol == ProxyProtocol.SOCKS4:
                await self._connect_via_socks4(
                    proxy, target_host, target_port, reader, writer
                )
            else:  # HTTP or HTTPS
                await self._connect_via_http(
                    proxy, target_host, target_port, reader, writer
                )

            return reader, writer

        except Exception:
            # Clean up on failure
            writer.close()
            await writer.wait_closed()
            raise

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
            await self._tunnel(
                client_reader,
                client_writer,
                upstream_reader,
                upstream_writer,
            )

        except TimeoutError:
            latency_ms = (time.monotonic() - start_time) * 1000
            await self._send_error(client_writer, 504, "Gateway Timeout")
        except ConnectionRefusedError:
            latency_ms = (time.monotonic() - start_time) * 1000
            await self._send_error(client_writer, 502, "Bad Gateway")
        except ConnectionError as e:
            latency_ms = (time.monotonic() - start_time) * 1000
            logger.error("CONNECT error", error=str(e), target=target)
            await self._send_error(client_writer, 502, "Bad Gateway")
        except Exception as e:
            latency_ms = (time.monotonic() - start_time) * 1000
            logger.error("CONNECT error", error=str(e), target=target)
            await self._send_error(client_writer, 502, "Bad Gateway")
        finally:
            if upstream_writer:
                upstream_writer.close()
                await upstream_writer.wait_closed()
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

            upstream_writer.write(request_line.encode())

            # Forward headers, adding proxy auth for HTTP proxies only
            # (SOCKS auth is handled during tunnel establishment)
            if not is_socks and proxy.username and proxy.password:
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

        except TimeoutError:
            latency_ms = (time.monotonic() - start_time) * 1000
            await self._send_error(client_writer, 504, "Gateway Timeout")
        except ConnectionRefusedError:
            latency_ms = (time.monotonic() - start_time) * 1000
            await self._send_error(client_writer, 502, "Bad Gateway")
        except ConnectionError as e:
            latency_ms = (time.monotonic() - start_time) * 1000
            logger.error("HTTP forward error", error=str(e), target=target)
            await self._send_error(client_writer, 502, "Bad Gateway")
        except Exception as e:
            latency_ms = (time.monotonic() - start_time) * 1000
            logger.error("HTTP forward error", error=str(e), target=target)
            await self._send_error(client_writer, 502, "Bad Gateway")
        finally:
            if upstream_writer:
                upstream_writer.close()
                await upstream_writer.wait_closed()
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

