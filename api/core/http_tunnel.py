# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The CONNECT handshake with an HTTP upstream proxy, keeping what the vendor says when it refuses.

python-socks reads a refusal whole but keeps only its status line. Vendors
explain a refusal in headers of their own (Bright Data's ``x-brd-error``,
for one) or in the body, and that is what tells a 403 for a blocked target
from a 403 for a closed zone. SOCKS upstreams stay on python-socks.

httpx's own proxying keeps only the status line as well, so the preflight
echo goes through :class:`TunnelTransport`, which opens its tunnel here.
"""

import asyncio
import base64
import contextlib
import html
import re
import ssl
from collections.abc import Callable, Iterable
from typing import Any

import httpcore
import httpx
from python_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

# Longest head a refusal may have; past it the answer is not an HTTP proxy's.
MAX_HEAD_BYTES = 65536
# How much of a refusal's body is read, and how long it is waited for once
# the head is in: a vendor that keeps the connection open without saying the
# body's length must not hold up the client's answer.
MAX_BODY_BYTES = 4096
BODY_WAIT_SECONDS = 1.0
# Longest vendor message kept; the reason phrase cuts it again.
MAX_VENDOR_MESSAGE_CHARS = 300

# The words that make a header one a vendor explains a refusal in, as a part
# of its name: x-brd-error, x-brd-err-code, x-error-message and the like.
_EXPLAINING_WORDS = frozenset({"error", "errors", "err", "reason", "message", "msg", "cause", "detail", "details"})
_NAME_PARTS = re.compile(r"[-_]")
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9]")


class TunnelRefusedError(ProxyError):
    """An HTTP upstream answered the CONNECT with a status other than 2xx, worded as python-socks words it.

    ``vendor_message`` is what the vendor said about it besides the status line, None when nothing.
    """

    def __init__(self, status: int, reason: str, vendor_message: str | None = None) -> None:
        super().__init__(f"{status} {reason}".rstrip(), error_code=status)
        self.vendor_message = vendor_message


async def open_http_tunnel(
    proxy_host: str,
    proxy_port: int,
    username: str | None,
    password: str | None,
    target_host: str,
    target_port: int,
    on_connected: Callable[[], None] | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A tunnel to ``target_host:target_port`` through the HTTP proxy at ``proxy_host:proxy_port``; no deadline of its own.

    ``on_connected`` is called once the connection to the proxy is open, before the CONNECT is sent.

    Raises:
        ProxyConnectionError: the proxy could not be reached, as python-socks raises it.
        TunnelRefusedError: the proxy refused the tunnel.
        ProxyError: the proxy's answer was not HTTP.
    """
    try:
        reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    except OSError as e:
        raise ProxyConnectionError(e.errno, f"Could not connect to proxy {proxy_host}:{proxy_port} [{e.strerror}]") from e
    if on_connected is not None:
        on_connected()
    try:
        writer.write(_connect_request(target_host, target_port, username, password))
        await writer.drain()
        status, reason, headers = _parse_head(await _read_head(reader))
        if 200 <= status < 300:
            # Bytes the proxy sent past its head are the target's, and stay in the reader.
            return reader, writer
        body = await _read_body(reader, headers)
        raise TunnelRefusedError(status, reason, vendor_message(status, reason, headers, body))
    except BaseException:
        writer.transport.abort()
        raise


def vendor_message(status: int, reason: str, headers: list[tuple[str, str]], body: bytes) -> str | None:
    """What a refusal says beyond its status line: its explaining headers, then its body as text; None when nothing."""
    content_type = next((value for name, value in headers if name.lower() == "content-type"), "")
    parts = [
        f"{name}: {value}" for name, value in headers if _explains(name) and _says_more(value, status, reason)
    ]
    text = _body_text(body, content_type)
    if _says_more(text, status, reason):
        parts.append(text)
    said = "; ".join(parts)
    if len(said) > MAX_VENDOR_MESSAGE_CHARS:
        said = said[: MAX_VENDOR_MESSAGE_CHARS - 3].rstrip() + "..."
    return said or None


def _explains(name: str) -> bool:
    """Whether a header is one a vendor explains a refusal in, an RFC 9209 Proxy-Status among them."""
    lowered = name.lower()
    return lowered == "proxy-status" or not _EXPLAINING_WORDS.isdisjoint(_NAME_PARTS.split(lowered))


def _connect_request(target_host: str, target_port: int, username: str | None, password: str | None) -> bytes:
    # An IPv6 literal is bracketed, or the port would read as part of it.
    host = f"[{target_host}]" if ":" in target_host and not target_host.startswith("[") else target_host
    lines = [f"CONNECT {host}:{target_port} HTTP/1.1", f"Host: {host}:{target_port}"]
    if username and password:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        lines.append(f"Proxy-Authorization: Basic {token}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    """The status line and headers, up to the blank line; a proxy that closes early leaves what it sent."""
    head = bytearray()
    while len(head) <= MAX_HEAD_BYTES:
        try:
            line = await reader.readline()
        except ValueError as e:  # a line past the reader's limit
            raise ProxyError("Invalid proxy response") from e
        if line in (b"\r\n", b"\n") or not line:
            if not head:
                raise ProxyError("Invalid proxy response")
            return bytes(head)
        head += line
    raise ProxyError("Invalid proxy response")


def _parse_head(head: bytes) -> tuple[int, str, list[tuple[str, str]]]:
    # Split where _read_head did: splitlines would also break a header at a
    # control character inside it. UTF-8, so a vendor's own words read right.
    status_line, *lines = head.decode("utf-8", "replace").split("\n")
    status_line = status_line.strip()
    try:
        _version, code, *reason = status_line.split(None, 2)
    except ValueError:
        raise ProxyError(f"Invalid status line: {status_line}") from None
    try:
        status = int(code)
    except ValueError:
        raise ProxyError(f"Invalid status code: {code}") from None
    headers = [(name.strip(), value.strip()) for name, sep, value in (line.partition(":") for line in lines) if sep]
    return status, " ".join(reason).strip(), headers


async def _read_body(reader: asyncio.StreamReader, headers: list[tuple[str, str]]) -> bytes:
    """The start of a refusal's body: what arrives within ``BODY_WAIT_SECONDS``, up to its length or the cap."""
    fields = {name.lower(): value for name, value in headers}
    try:
        length = int(fields["content-length"])
    except (KeyError, ValueError):
        length = None
    want = MAX_BODY_BYTES if length is None else min(max(length, 0), MAX_BODY_BYTES)
    chunked = "chunked" in fields.get("transfer-encoding", "").lower()
    body = bytearray()
    with contextlib.suppress(TimeoutError, OSError):
        async with asyncio.timeout(BODY_WAIT_SECONDS):
            while len(body) < want:
                chunk = await reader.read(want - len(body))
                if not chunk:
                    break
                body += chunk
                # A vendor keeping the connection open sends nothing after the last chunk.
                if chunked and _dechunk(bytes(body))[1]:
                    break
    return _dechunk(bytes(body))[0] if chunked else bytes(body)


def _dechunk(data: bytes) -> tuple[bytes, bool]:
    """The data of a chunked body as far as it goes, and whether its last chunk has arrived."""
    out = bytearray()
    while data:
        size_line, sep, rest = data.partition(b"\r\n")
        if not sep:
            break
        try:
            size = int(size_line.split(b";", 1)[0], 16)
        except ValueError:
            break
        if size == 0:
            return bytes(out), True
        out += rest[:size]
        data = rest[size + 2:]
    return bytes(out), False


def _body_text(body: bytes, content_type: str) -> str:
    """A body as one line of text, an HTML page's text without its markup; empty for a body that is not text."""
    media = content_type.split(";", 1)[0].strip().lower()
    if media and not (media.startswith("text/") or media.endswith(("json", "xml"))):
        return ""
    text = body.decode("utf-8", "replace")
    if "html" in media or text.lstrip().startswith("<"):
        text = html.unescape(_TAG.sub(" ", _SCRIPT_OR_STYLE.sub(" ", text)))
    return _SPACE.sub(" ", text).strip()


def _says_more(text: str, status: int, reason: str) -> bool:
    """Whether ``text`` says anything but the status and its reason phrase, as an error page's title and heading do."""
    rest = text.lower().replace(str(status), " ")
    if reason:
        rest = rest.replace(reason.lower(), " ")
    return _WORD.search(rest) is not None


class TunnelTransport(httpx.AsyncBaseTransport):
    """An httpx transport to any host through the HTTP proxy at ``proxy_url``, on Octoprox's own CONNECT.

    The upstream failing the CONNECT raises what :func:`open_http_tunnel` raises, a :class:`TunnelRefusedError` with
    what the vendor said among them, as a client's own request through it would; whatever fails once the tunnel is up
    is httpx's error.
    """

    def __init__(self, proxy_url: str, ssl_context: ssl.SSLContext) -> None:
        proxy = httpx.URL(proxy_url)
        backend = _TunnelBackend(proxy.host, proxy.port or 80, proxy.username or None, proxy.password or None)
        self._pool = httpcore.AsyncConnectionPool(ssl_context=ssl_context, network_backend=backend)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = httpcore.URL(
            scheme=request.url.raw_scheme, host=request.url.raw_host, port=request.url.port, target=request.url.raw_path
        )
        try:
            response = await self._pool.request(
                request.method, url,
                headers=request.headers.raw, content=await request.aread(), extensions=request.extensions,
            )
        except httpcore.TimeoutException as e:
            raise httpx.TimeoutException(str(e), request=request) from e
        except (httpcore.NetworkError, httpcore.ProtocolError, httpcore.UnsupportedProtocol) as e:
            raise httpx.TransportError(str(e), request=request) from e
        return httpx.Response(
            response.status, headers=response.headers, content=response.content, extensions=response.extensions
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


class _TunnelBackend(httpcore.AsyncNetworkBackend):
    """Dials each of httpcore's connections as a tunnel through one HTTP upstream proxy."""

    def __init__(self, host: str, port: int, username: str | None, password: str | None) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # Left as python-socks words them, a timeout too: the upstream failed, not the request.
        try:
            async with asyncio.timeout(timeout):
                reader, writer = await open_http_tunnel(
                    self._host, self._port, self._username, self._password, host, port
                )
        except TimeoutError as e:
            # The deadline's own carries no words; say which one it was, as python-socks does.
            raise ProxyTimeoutError(str(e) or f"Proxy connection timed out: {timeout}") from e
        return _TunnelStream(reader, writer)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _TunnelStream(httpcore.AsyncNetworkStream):
    """An open tunnel as httpcore reads and writes it, failing with httpcore's errors."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        try:
            async with asyncio.timeout(timeout):
                return await self._reader.read(max_bytes)
        except TimeoutError as e:
            raise httpcore.ReadTimeout(str(e)) from e
        except OSError as e:
            raise httpcore.ReadError(str(e)) from e

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        try:
            self._writer.write(buffer)
            async with asyncio.timeout(timeout):
                await self._writer.drain()
        except TimeoutError as e:
            raise httpcore.WriteTimeout(str(e)) from e
        except OSError as e:
            raise httpcore.WriteError(str(e)) from e

    async def aclose(self) -> None:
        self._writer.transport.abort()

    async def start_tls(
        self, ssl_context: ssl.SSLContext, server_hostname: str | None = None, timeout: float | None = None
    ) -> httpcore.AsyncNetworkStream:
        try:
            async with asyncio.timeout(timeout):
                await self._writer.start_tls(ssl_context, server_hostname=server_hostname)
        except TimeoutError as e:
            raise httpcore.ConnectTimeout(str(e)) from e
        except OSError as e:  # ssl.SSLError among them
            raise httpcore.ConnectError(str(e)) from e
        return self

    def get_extra_info(self, info: str) -> Any:
        if info in ("ssl_object", "socket"):
            return self._writer.get_extra_info(info)
        if info == "server_addr":
            return self._writer.get_extra_info("peername")
        if info == "client_addr":
            return self._writer.get_extra_info("sockname")
        return None
