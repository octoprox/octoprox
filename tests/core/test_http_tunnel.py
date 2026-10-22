# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for Octoprox's own CONNECT to an HTTP upstream: the request it sends, and what it keeps of a refusal."""

import asyncio
import base64
import contextlib
from collections.abc import AsyncIterator

import pytest
from python_socks import ProxyConnectionError, ProxyError

from api.core.failure_log import classify_upstream_error
from api.core.http_tunnel import (
    BODY_WAIT_SECONDS,
    MAX_VENDOR_MESSAGE_CHARS,
    TunnelRefusedError,
    open_http_tunnel,
    vendor_message,
)
from api.core.stats import FailureKind
from api.models.proxy import Proxy, ProxyProtocol


@contextlib.asynccontextmanager
async def _vendor(answer: bytes, heard: list[bytes], *, keep_open: bool = False) -> AsyncIterator[int]:
    """An upstream that records each CONNECT head into ``heard``, answers ``answer`` and closes, or holds the connection open."""
    held: list[asyncio.StreamWriter] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        heard.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(answer)
        await writer.drain()
        if keep_open:
            held.append(writer)
        else:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        for writer in held:
            writer.close()
        server.close()
        await server.wait_closed()


async def _refusal(answer: bytes) -> TunnelRefusedError:
    async with _vendor(answer, []) as port:
        with pytest.raises(TunnelRefusedError) as refused:
            await open_http_tunnel("127.0.0.1", port, None, None, "example.com", 443)
    return refused.value


class TestHandshake:
    async def test_sends_the_target_and_the_connectors_credentials(self) -> None:
        heard: list[bytes] = []
        async with _vendor(b"HTTP/1.1 200 Connection established\r\n\r\n", heard) as port:
            _reader, writer = await open_http_tunnel("127.0.0.1", port, "user", "pässword", "2001:db8::1", 443)
            writer.close()

        token = base64.b64encode("user:pässword".encode()).decode()
        assert heard == [
            f"CONNECT [2001:db8::1]:443 HTTP/1.1\r\nHost: [2001:db8::1]:443\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode()
        ]

    async def test_a_refusal_reads_as_python_socks_worded_it(self) -> None:
        refused = await _refusal(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        assert (str(refused), refused.error_code, refused.vendor_message) == (
            "407 Proxy Authentication Required", 407, None,
        )
        proxy = Proxy(host="127.0.0.1", port=1, protocol=ProxyProtocol.HTTP, connector_id="c")
        assert classify_upstream_error(refused, proxy) == (FailureKind.UPSTREAM_REJECTED, 407)

    async def test_a_refusal_keeps_what_the_vendor_said(self) -> None:
        refused = await _refusal(
            b"HTTP/1.1 502 Bad Gateway\r\nX-Luminati-Error: Could not resolve host\r\nTransfer-Encoding: chunked\r\n"
            b"Content-Type: text/plain\r\n\r\n9\r\nno exit i\r\n7\r\nn japan\r\n0\r\n\r\n"
        )
        assert refused.vendor_message == "X-Luminati-Error: Could not resolve host; no exit in japan"

    async def test_a_chunked_refusal_on_a_connection_kept_open_is_read_to_its_last_chunk(self) -> None:
        """HTTP/1.1 keeps a connection open by default: the last chunk ends the body, not the wait."""
        answer = b"HTTP/1.1 403 Forbidden\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nzone\r\n0\r\n\r\n"
        async with _vendor(answer, [], keep_open=True) as port:
            started = asyncio.get_running_loop().time()
            with pytest.raises(TunnelRefusedError) as refused:
                await open_http_tunnel("127.0.0.1", port, None, None, "example.com", 443)
            waited = asyncio.get_running_loop().time() - started

        assert refused.value.vendor_message == "zone"
        assert waited < BODY_WAIT_SECONDS / 2

    async def test_a_vendors_header_reads_as_utf8(self) -> None:
        refused = await _refusal("HTTP/1.1 403 Forbidden\r\nX-Error: Zone «rési» closed\r\nContent-Length: 0\r\n\r\n".encode())
        assert refused.vendor_message == "X-Error: Zone «rési» closed"

    async def test_an_answer_that_is_not_http_is_an_error(self) -> None:
        async with _vendor(b"SSH-2.0-OpenSSH\r\n\r\n", []) as port:
            with pytest.raises(ProxyError, match="Invalid status line"):
                await open_http_tunnel("127.0.0.1", port, None, None, "example.com", 443)
        async with _vendor(b"HTTP/1.1 OK\r\n\r\n", []) as port:
            with pytest.raises(ProxyError, match="Invalid status code"):
                await open_http_tunnel("127.0.0.1", port, None, None, "example.com", 443)
        async with _vendor(b"", []) as port:
            with pytest.raises(ProxyError, match="Invalid proxy response"):
                await open_http_tunnel("127.0.0.1", port, None, None, "example.com", 443)

    async def test_an_unreachable_proxy_is_worded_and_classed_as_before(self) -> None:
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()

        with pytest.raises(ProxyConnectionError, match=f"Could not connect to proxy 127.0.0.1:{port}") as unreachable:
            await open_http_tunnel("127.0.0.1", port, None, None, "example.com", 443)
        proxy = Proxy(host="127.0.0.1", port=port, protocol=ProxyProtocol.HTTP, connector_id="c")
        assert classify_upstream_error(unreachable.value, proxy)[0] is FailureKind.UPSTREAM_UNREACHABLE


class TestVendorMessage:
    def test_only_explaining_headers_are_kept(self) -> None:
        headers = [
            ("x-brd-error", "Auth failed"),
            ("x-brd-err-code", "client_10000"),
            ("Referrer-Policy", "no-referrer"),
            ("Server", "vendor"),
            ("Proxy-Status", 'vendor; error=destination_unavailable'),
            ("Retry-After", "7"),
        ]
        assert vendor_message(407, "Proxy Authentication Required", headers, b"") == (
            "x-brd-error: Auth failed; x-brd-err-code: client_10000; Proxy-Status: vendor; error=destination_unavailable"
        )

    def test_an_error_page_saying_only_the_status_says_nothing(self) -> None:
        page = b"<html><head><title>407 Proxy Authentication Required</title></head><body><h1>407</h1></body></html>"
        assert vendor_message(407, "Proxy Authentication Required", [("Content-Type", "text/html")], page) is None
        assert vendor_message(429, "Too Many Requests", [("X-Error", "Too Many Requests")], b"") is None

    def test_an_html_page_is_read_as_its_text(self) -> None:
        page = b"<html><style>p{color:red}</style><script>var x = 1;</script><p>Account&nbsp;suspended</p></html>"
        assert vendor_message(403, "Forbidden", [("Content-Type", "text/html; charset=utf-8")], page) == "Account suspended"

    def test_json_is_kept_on_one_line_and_binary_is_not_read(self) -> None:
        body = b'{\n  "error": "zone_not_found"\n}'
        assert vendor_message(400, "Bad Request", [("Content-Type", "application/json")], body) == '{ "error": "zone_not_found" }'
        assert vendor_message(400, "Bad Request", [("Content-Type", "image/png")], b"\x89PNG zone") is None

    def test_a_long_message_is_cut(self) -> None:
        said = vendor_message(403, "Forbidden", [], b"blocked " * 100)
        assert said is not None and len(said) == MAX_VENDOR_MESSAGE_CHARS and said.endswith("...")
