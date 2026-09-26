# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for header hygiene on the plain-HTTP forwarding path.

A client talking to Octoprox sends Proxy-Authorization (its Octoprox
project credential), Proxy-Connection and other hop-by-hop headers. They
are addressed to Octoprox and must be stripped before the request travels
on: to an HTTP upstream we add our own Proxy-Authorization for the vendor,
and a second one would leak the project credential and shadow the vendor
credential; to a SOCKS upstream the request goes straight to the origin,
which must never see either header. The response leg gets the mirror
treatment so the client never sees the vendor's connection management.
"""

import asyncio
import base64
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core.proxy_server import (
    HOP_BY_HOP_REQUEST_HEADERS,
    HOP_BY_HOP_RESPONSE_HEADERS,
    ProxyServer,
)
from api.core.traffic_limiter import TrafficMeter
from api.models.proxy import Proxy, ProxyProtocol

CLIENT_TOKEN = base64.b64encode(b"ivan-sessid-abcd-cc-us:ivan").decode()
UPSTREAM_TOKEN = base64.b64encode(b"vendor-user:vendor-pass").decode()

CLIENT_HEADERS = {
    "host": "ip-api.com",
    "proxy-authorization": f"Basic {CLIENT_TOKEN}",
    "user-agent": "curl/8.7.1",
    "accept": "*/*",
    "proxy-connection": "Keep-Alive",
}

DEFAULT_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"


class _Capture:
    """Records the raw request an upstream receives and answers with a canned response."""

    def __init__(self, response: bytes = DEFAULT_RESPONSE) -> None:
        self.received = b""
        self.response = response
        self.server: asyncio.Server | None = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            line = await reader.readline()
            self.received += line
            if line in (b"\r\n", b"\n", b""):
                break
        writer.write(self.response)
        await writer.drain()
        writer.close()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    @property
    def port(self) -> int:
        assert self.server is not None
        port: int = self.server.sockets[0].getsockname()[1]
        return port

    def header_lines(self) -> list[str]:
        text = self.received.decode()
        return [ln for ln in text.split("\r\n")[1:] if ln]

    def header_names(self) -> list[str]:
        return [ln.split(":", 1)[0].lower() for ln in self.header_lines()]

    def request_line(self) -> str:
        return self.received.decode().split("\r\n", 1)[0]


class _ClientWriter:
    """Minimal stand-in for the client-side StreamWriter."""

    def __init__(self) -> None:
        self.buffer = b""

    def write(self, data: bytes) -> None:
        self.buffer += data

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False

    def header_lines(self) -> list[str]:
        head = self.buffer.split(b"\r\n\r\n", 1)[0].decode()
        return head.split("\r\n")[1:]

    def header_names(self) -> list[str]:
        return [ln.split(":", 1)[0].lower() for ln in self.header_lines()]

    def body(self) -> bytes:
        return self.buffer.split(b"\r\n\r\n", 1)[1]


class _NoLimit:
    """A limiter that never interrupts and swallows progress, for a metered server."""

    def is_interrupted(self, connector_id: str) -> bool:
        return False

    def limit_status_for(self, connector_id: str) -> int:
        return 509

    def progress(self, *args: object) -> None:
        pass


def _server(proxy: Proxy) -> ProxyServer:
    manager = MagicMock()
    manager.get_project.return_value = None  # skips exit verification
    manager.traffic_meter = lambda p, project_id: TrafficMeter(_NoLimit(), p.id, project_id, p.connector_id)  # type: ignore[arg-type]
    server = ProxyServer(manager)
    server._get_upstream_proxy = AsyncMock(return_value=proxy)  # type: ignore[method-assign]
    return server


def _http_proxy(port: int) -> Proxy:
    return Proxy(
        host="127.0.0.1",
        port=port,
        protocol=ProxyProtocol.HTTP,
        username="vendor-user",
        password="vendor-pass",
        connector_id="connector-1",
    )


async def _forward(
    server: ProxyServer, target: str, headers: dict[str, str] | None = None
) -> _ClientWriter:
    client_reader = asyncio.StreamReader()
    client_reader.feed_eof()
    client_writer = _ClientWriter()
    await server._handle_http(
        client_reader,
        client_writer,  # type: ignore[arg-type]
        "GET",
        target,
        "HTTP/1.1",
        dict(CLIENT_HEADERS if headers is None else headers),
        project_id="project-1",
        client_ip="127.0.0.1",
        sessid="abcd",
        country="US",
    )
    return client_writer


@pytest.fixture(autouse=True)
def quiet_event_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep request_completed from reaching receivers other tests left subscribed."""
    monkeypatch.setattr("api.core.proxy_server.event_bus.publish", AsyncMock())


@pytest.fixture
async def upstream() -> AsyncIterator[_Capture]:
    capture = _Capture()
    await capture.start()
    yield capture
    await capture.stop()


class TestEndToEndHeaders:
    def test_strips_static_request_set(self) -> None:
        kept = ProxyServer._end_to_end_headers(dict(CLIENT_HEADERS), HOP_BY_HOP_REQUEST_HEADERS)
        assert set(kept) == {"host", "user-agent", "accept"}
        assert kept["user-agent"] == "curl/8.7.1"

    def test_static_sets_cover_the_proxy_credentials(self) -> None:
        assert {"proxy-authorization", "proxy-connection", "connection"} <= HOP_BY_HOP_REQUEST_HEADERS
        assert {"proxy-authenticate", "connection", "keep-alive"} <= HOP_BY_HOP_RESPONSE_HEADERS

    def test_connection_listed_names_are_dropped(self) -> None:
        headers = {
            "host": "example.com",
            "connection": "keep-alive, X-Hop-Token",
            "x-hop-token": "abc",
            "x-end-to-end": "keep me",
        }
        kept = ProxyServer._end_to_end_headers(headers, HOP_BY_HOP_REQUEST_HEADERS)
        assert set(kept) == {"host", "x-end-to-end"}

    def test_body_framing_names_survive_a_hostile_connection_header(self) -> None:
        headers = {
            "host": "example.com",
            "connection": "content-length, transfer-encoding, host",
            "content-length": "3",
            "transfer-encoding": "chunked",
        }
        kept = ProxyServer._end_to_end_headers(headers, HOP_BY_HOP_REQUEST_HEADERS)
        assert set(kept) == {"host", "content-length", "transfer-encoding"}


class TestHttpUpstream:
    async def test_vendor_credential_is_the_only_proxy_authorization(
        self, upstream: _Capture
    ) -> None:
        client = await _forward(_server(_http_proxy(upstream.port)), "http://ip-api.com/")

        assert upstream.request_line() == "GET http://ip-api.com/ HTTP/1.1"
        auth_lines = [ln for ln in upstream.header_lines() if ln.lower().startswith("proxy-authorization:")]
        assert auth_lines == [f"Proxy-Authorization: Basic {UPSTREAM_TOKEN}"]
        assert CLIENT_TOKEN not in upstream.received.decode()
        assert "proxy-connection" not in upstream.header_names()
        # Ordinary end-to-end headers still travel.
        assert "host: ip-api.com" in upstream.header_lines()
        assert "user-agent: curl/8.7.1" in upstream.header_lines()
        assert client.buffer.startswith(b"HTTP/1.1 200 OK")

    async def test_upstream_is_told_we_close_after_one_exchange(self, upstream: _Capture) -> None:
        headers = {**CLIENT_HEADERS, "connection": "keep-alive", "keep-alive": "timeout=5"}
        await _forward(_server(_http_proxy(upstream.port)), "http://ip-api.com/", headers)

        assert upstream.header_names().count("connection") == 1
        assert "connection: close" in upstream.header_lines()
        assert "keep-alive" not in upstream.header_names()

    async def test_response_hop_by_hop_headers_are_replaced(self) -> None:
        vendor = _Capture(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: keep-alive\r\n"
            b"Keep-Alive: timeout=5\r\n"
            b'Proxy-Authenticate: Basic realm="vendor"\r\n'
            b"X-Brd-Ip: 85.28.54.148\r\n"
            b"\r\n"
            b"ok"
        )
        await vendor.start()
        try:
            client = await _forward(_server(_http_proxy(vendor.port)), "http://ip-api.com/")
        finally:
            await vendor.stop()

        names = client.header_names()
        assert names.count("connection") == 1
        assert "Connection: close" in client.header_lines()
        assert "keep-alive" not in names
        assert "proxy-authenticate" not in names
        # End-to-end headers keep the vendor's spelling.
        assert "X-Brd-Ip: 85.28.54.148" in client.header_lines()
        assert "Content-Length: 2" in client.header_lines()
        assert client.body() == b"ok"

    async def test_chunked_response_still_relays_after_filtering(self) -> None:
        vendor = _Capture(
            b"HTTP/1.1 200 OK\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"2\r\nok\r\n0\r\n\r\n"
        )
        await vendor.start()
        try:
            client = await _forward(_server(_http_proxy(vendor.port)), "http://ip-api.com/")
        finally:
            await vendor.stop()

        assert "Transfer-Encoding: chunked" in client.header_lines()
        assert client.header_names().count("connection") == 1
        assert client.body() == b"2\r\nok\r\n0\r\n\r\n"


class TestSocksUpstream:
    async def test_origin_never_sees_proxy_headers(self, upstream: _Capture) -> None:
        proxy = Proxy(
            host="socks.example",
            port=1080,
            protocol=ProxyProtocol.SOCKS5,
            username="vendor-user",
            password="vendor-pass",
            connector_id="connector-1",
        )
        server = _server(proxy)
        port = upstream.port

        async def fake_tunnel(
            _proxy: Proxy, _host: str, _port: int
        ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            return await asyncio.open_connection("127.0.0.1", port)

        server._connect_via_proxy = fake_tunnel  # type: ignore[method-assign,assignment]
        client = await _forward(server, "http://ip-api.com/json")

        assert upstream.request_line() == "GET /json HTTP/1.1"
        assert not any(name.startswith("proxy-") for name in upstream.header_names())
        assert CLIENT_TOKEN not in upstream.received.decode()
        assert "host: ip-api.com" in upstream.header_lines()
        assert "connection: close" in upstream.header_lines()
        assert client.buffer.startswith(b"HTTP/1.1 200 OK")
