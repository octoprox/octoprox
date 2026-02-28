# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Plain MITM relay using Python's standard TLS library.

Forwards HTTP requests through the pre-established upstream connection
using Python's ssl module. The target server sees a Python/OpenSSL TLS
fingerprint — easily detectable, but useful for debugging.
"""

import asyncio
import contextlib
import ssl

import structlog

from api.core.mitm.base import MitmRelay

logger = structlog.get_logger()


class PlainRelay(MitmRelay):
    """Relay requests via a raw asyncio TLS connection.

    Uses the upstream connection that was already established through the
    proxy by proxy_server.py. Upgrades it to TLS on first use.
    """

    def __init__(
        self,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        target_host: str,
    ) -> None:
        self._upstream_reader = upstream_reader
        self._upstream_writer = upstream_writer
        self._target_host = target_host
        self._tls_upgraded = False

    async def _ensure_tls(self) -> None:
        """Upgrade the upstream connection to TLS if not already done."""
        if self._tls_upgraded:
            return

        loop = asyncio.get_running_loop()
        transport = self._upstream_writer.transport
        protocol = transport.get_protocol()

        ssl_ctx = ssl.create_default_context()
        new_transport = await loop.start_tls(
            transport,
            protocol,
            ssl_ctx,
            server_hostname=self._target_host,
        )

        # Update writer's transport to the TLS-wrapped one
        self._upstream_writer._transport = new_transport  # type: ignore[attr-defined]
        self._tls_upgraded = True

    async def send_request(
        self,
        method: str,
        url: str,
        headers: list[tuple[str, str]],
        body: bytes | None,
        proxy_url: str,
    ) -> tuple[int, str, list[tuple[str, str]], bytes]:
        """Send request over the raw TLS connection and read the response."""
        await self._ensure_tls()

        # Extract path from full URL (e.g. https://example.com/path -> /path)
        # url is already full URL like https://host:port/path
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        # Build raw HTTP/1.1 request
        upstream_headers = list(headers)
        if not any(k.lower() == "host" for k, _v in upstream_headers):
            upstream_headers.append(("Host", self._target_host))
        self.last_upstream_headers = upstream_headers

        request_line = f"{method} {path} HTTP/1.1\r\n"
        self._upstream_writer.write(request_line.encode())

        for name, value in upstream_headers:
            self._upstream_writer.write(f"{name}: {value}\r\n".encode())

        # Content-Length for body
        if body:
            self._upstream_writer.write(f"Content-Length: {len(body)}\r\n".encode())

        self._upstream_writer.write(b"\r\n")

        if body:
            self._upstream_writer.write(body)

        await self._upstream_writer.drain()

        # Read response status line
        status_line_raw = await self._upstream_reader.readline()
        if not status_line_raw:
            return 502, "Bad Gateway", [], b""

        status_line = status_line_raw.decode("utf-8", errors="replace").strip()
        parts = status_line.split(" ", 2)
        if len(parts) < 2:
            return 502, "Bad Gateway", [], b""

        status_code = int(parts[1])
        reason = parts[2] if len(parts) > 2 else "OK"

        # Read response headers — list of tuples to preserve duplicates
        response_headers: list[tuple[str, str]] = []
        content_length = 0
        is_chunked = False

        while True:
            line_raw = await self._upstream_reader.readline()
            if not line_raw:
                break
            line = line_raw.decode("utf-8", errors="replace").strip()
            if not line:
                break
            if ":" in line:
                key, value = line.split(":", 1)
                header_name = key.strip()
                header_value = value.strip()
                response_headers.append((header_name, header_value))
                lower_name = header_name.lower()
                if lower_name == "content-length":
                    content_length = int(header_value)
                elif lower_name == "transfer-encoding" and "chunked" in header_value.lower():
                    is_chunked = True

        # Read response body
        response_body = b""
        if is_chunked:
            response_body = await self._read_chunked()
        elif content_length > 0:
            try:
                response_body = await self._upstream_reader.readexactly(content_length)
            except asyncio.IncompleteReadError as e:
                response_body = e.partial

        return status_code, reason, response_headers, response_body

    async def _read_chunked(self) -> bytes:
        """Read a chunked transfer-encoded response body."""
        body = bytearray()
        while True:
            size_line = await self._upstream_reader.readline()
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
                await self._upstream_reader.readline()  # trailing CRLF
                break
            try:
                chunk_data = await self._upstream_reader.readexactly(chunk_size)
            except asyncio.IncompleteReadError:
                break
            body.extend(chunk_data)
            await self._upstream_reader.readline()  # CRLF after chunk
        return bytes(body)

    async def close(self) -> None:
        """Close the upstream connection."""
        with contextlib.suppress(Exception):
            self._upstream_writer.close()
