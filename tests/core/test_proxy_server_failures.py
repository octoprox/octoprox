# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for what the proxy server tells the failure log.

Each test drives a real listener with a real client, against an upstream
that refuses, stays silent or answers with an error, and checks the
``request_failed`` event that comes out: what kind of failure, what the
client was answered, and what the client had asked for. The invariant
across them is that a request counted as failed by the metrics
(``request_completed`` with success False) is logged exactly once, and a
request that went through is not logged at all.
"""

import asyncio
import base64
import contextlib
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.core.failure_log import FailureKind, RequestFailure
from api.core.mitm.handler import MitmHandler
from api.core.proxy_server import ProxyServer
from api.core.signals import request_completed, request_failed
from api.core.traffic_limiter import TrafficMeter
from api.models.project import MitmMode, Project
from api.models.proxy import Proxy, ProxyProtocol

PROJECT = Project(name="Scraping", username="proj", password="secret")


class _NoLimit:
    def is_interrupted(self, connector_id: str) -> bool:
        return False

    def limit_status_for(self, connector_id: str) -> int:
        return 509

    def progress(self, *args: object) -> None:
        pass


def _auth(username: str = "proj-sessid-s1-cc-us") -> str:
    token = base64.b64encode(f"{username}:secret".encode()).decode()
    return f"Proxy-Authorization: Basic {token}\r\n"


def _upstream(port: int) -> Proxy:
    return Proxy(
        host="127.0.0.1", port=port, protocol=ProxyProtocol.HTTP,
        username="vendor-user", password="vendor-secret", connector_id="connector-1",
    )


@pytest.fixture
def publish(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr("api.core.proxy_server.event_bus.publish", mock)
    return mock


def _failures(publish: AsyncMock) -> list[RequestFailure]:
    return [c.kwargs["failure"] for c in publish.await_args_list if c.args[0] is request_failed]


def _completions(publish: AsyncMock) -> list[dict[str, Any]]:
    return [c.kwargs for c in publish.await_args_list if c.args[0] is request_completed]


@pytest.fixture
async def listen() -> AsyncIterator[Callable[..., Awaitable[ProxyServer]]]:
    servers: list[ProxyServer] = []

    async def start(proxy: Proxy | None, *, quarantined: bool = False, timeout: float | None = None) -> ProxyServer:
        manager = MagicMock()
        manager.get_project_by_username.side_effect = lambda u: PROJECT if u == PROJECT.username else None
        manager.get_project.return_value = PROJECT
        manager.select_proxy_for_project = AsyncMock(return_value=proxy)
        manager.are_all_proxies_quarantined = AsyncMock(return_value=quarantined)
        manager.traffic_limit_status.return_value = None
        manager.traffic_meter = lambda p, project_id, **kwargs: TrafficMeter(  # type: ignore[arg-type]
            _NoLimit(), p.id, project_id, p.connector_id, target_host=kwargs.get("target_host")
        )
        server = ProxyServer(manager)
        server._host, server._port = "127.0.0.1", 0
        if timeout is not None:
            server._timeout = timeout
        await server.start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        await server.stop()


async def _exchange(server: ProxyServer, request: str, *, then: bytes = b"") -> bytes:
    """Send a request, optionally more bytes and a half-close, and read until the server closes."""
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(request.encode() + then)
    await writer.drain()
    if then:
        writer.write_eof()
    response = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return response


@contextlib.asynccontextmanager
async def _vendor(answer: bytes | None) -> AsyncIterator[int]:
    """An upstream HTTP proxy that answers each CONNECT with ``answer``, or never answers when None."""
    held: list[asyncio.StreamWriter] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass
        if answer is None:
            held.append(writer)
            return
        writer.write(answer)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        for w in held:
            w.close()
        server.close()
        await server.wait_closed()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


class TestRefused:
    async def test_no_proxy_is_logged_with_what_was_asked(self, listen: Any, publish: AsyncMock) -> None:
        server = await listen(None)
        response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        assert response.startswith(b"HTTP/1.1 502")
        (failure,) = _failures(publish)
        assert failure.kind is FailureKind.NO_PROXY_AVAILABLE
        assert failure.status == 502
        assert failure.project_id == PROJECT.id
        assert (failure.method, failure.target) == ("CONNECT", "example.com:443")
        assert (failure.session_id, failure.location) == ("s1", "US")
        assert "US" in failure.message
        assert failure.proxy is None

    async def test_quarantine(self, listen: Any, publish: AsyncMock) -> None:
        server = await listen(None, quarantined=True)
        response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        assert response.startswith(b"HTTP/1.1 429")
        (failure,) = _failures(publish)
        assert (failure.kind, failure.status) == (FailureKind.ALL_PROXIES_QUARANTINED, 429)

    async def test_a_state_without_a_country(self, listen: Any, publish: AsyncMock) -> None:
        server = await listen(None)
        response = await _exchange(server, f"GET http://example.com/a?b=c HTTP/1.1\r\n{_auth('proj-st-ny')}\r\n")

        assert response.startswith(b"HTTP/1.1 400")
        (failure,) = _failures(publish)
        assert (failure.kind, failure.status) == (FailureKind.INVALID_LOCATION, 400)
        assert (failure.method, failure.target) == ("GET", "example.com:80")

    async def test_wrong_credentials_are_not_a_project_failure(self, listen: Any, publish: AsyncMock) -> None:
        server = await listen(None)
        token = base64.b64encode(b"proj:wrong").decode()
        response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\nProxy-Authorization: Basic {token}\r\n\r\n")

        assert response.startswith(b"HTTP/1.1 407")
        assert _failures(publish) == []


class TestUpstreamFailures:
    async def test_vendor_refusal_carries_the_vendors_status(self, listen: Any, publish: AsyncMock) -> None:
        async with _vendor(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n") as port:
            server = await listen(_upstream(port))
            response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        assert response.startswith(b"HTTP/1.1 502")
        (failure,) = _failures(publish)
        assert failure.kind is FailureKind.UPSTREAM_REJECTED
        assert (failure.status, failure.upstream_status) == (502, 407)
        assert "407" in failure.message
        assert failure.error_type == "ProxyError"
        assert failure.proxy == f"127.0.0.1:{port}"
        assert failure.connector_id == "connector-1"
        assert failure.latency_ms is not None
        assert [c["success"] for c in _completions(publish)] == [False]

    async def test_unreachable_upstream(self, listen: Any, publish: AsyncMock) -> None:
        server = await listen(_upstream(_closed_port()))
        response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        assert response.startswith(b"HTTP/1.1 502")
        (failure,) = _failures(publish)
        assert (failure.kind, failure.status) == (FailureKind.UPSTREAM_UNREACHABLE, 502)

    async def test_silent_upstream_times_out(self, listen: Any, publish: AsyncMock) -> None:
        async with _vendor(None) as port:
            server = await listen(_upstream(port), timeout=0.3)
            response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n")

        assert response.startswith(b"HTTP/1.1 504")
        (failure,) = _failures(publish)
        assert (failure.kind, failure.status) == (FailureKind.UPSTREAM_TIMEOUT, 504)
        assert "0.3 seconds" in failure.message

    async def test_a_tunnel_that_went_through_is_not_logged(self, listen: Any, publish: AsyncMock) -> None:
        async with _vendor(b"HTTP/1.1 200 Connection established\r\n\r\n") as port:
            server = await listen(_upstream(port))
            response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n", then=b"hello")

        assert response.startswith(b"HTTP/1.1 200")
        assert _failures(publish) == []
        assert [c["success"] for c in _completions(publish)] == [True]

    async def test_a_client_that_leaves_mid_body_is_its_own_kind(self, listen: Any, publish: AsyncMock) -> None:
        async with _vendor(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n") as port:
            server = await listen(_upstream(port))
            response = await _exchange(
                server, f"POST http://example.com/upload HTTP/1.1\r\n{_auth()}Content-Length: 10\r\n\r\n", then=b"abc",
            )

        assert response.startswith(b"HTTP/1.1 502")
        (failure,) = _failures(publish)
        assert failure.kind is FailureKind.CLIENT_CLOSED
        assert (failure.method, failure.target) == ("POST", "example.com:80")
        assert [c["success"] for c in _completions(publish)] == [False]

    async def test_a_client_that_stalls_mid_body_is_its_own_kind(self, listen: Any, publish: AsyncMock) -> None:
        async with _vendor(None) as port:
            server = await listen(_upstream(port), timeout=0.3)
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(f"POST http://example.com/upload HTTP/1.1\r\n{_auth()}Content-Length: 10\r\n\r\nabc".encode())
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=5)
            writer.close()

        assert response.startswith(b"HTTP/1.1 504")
        (failure,) = _failures(publish)
        assert (failure.kind, failure.status) == (FailureKind.CLIENT_CLOSED, 504)
        assert "request body" in failure.message

    def test_an_unparseable_target_keeps_neither_query_nor_userinfo(self) -> None:
        server = ProxyServer(MagicMock())
        assert server._target_of("GET", "http://[2001:db8::1]/a?token=x") == "[2001:db8::1]"
        assert server._target_of("GET", "http://user:pw@example.com/a?token=x") == "example.com"
        assert server._target_of("GET", "http://user:pw@example.com:8080/a") == "example.com:8080"


class TestInterceptedRequests:
    async def test_a_failed_intercepted_request_is_logged_without_its_query(self, publish: AsyncMock) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"GET /search?token=abc HTTP/1.1\r\nHost: example.com\r\n\r\n")
        reader.feed_eof()
        writer = MagicMock()
        writer.transport.get_protocol.return_value = MagicMock()
        writer.drain = AsyncMock()
        project = MagicMock(id="project-1", tls_mitm_mode=MitmMode.PLAIN, tls_mitm_engine=None, tls_mitm_browser=None)
        relay = AsyncMock()
        relay.send_request.side_effect = ConnectionError("upstream failed")

        with (
            patch("asyncio.get_running_loop") as loop,
            patch("api.core.mitm.create_relay", return_value=relay),
        ):
            loop.return_value.start_tls = AsyncMock(return_value=MagicMock())
            await MitmHandler(MagicMock()).handle(reader, writer, "example.com", 443, _upstream(8080), project)

        (failure,) = _failures(publish)
        assert failure.kind is FailureKind.INTERCEPTED_REQUEST_FAILED
        assert (failure.method, failure.target) == ("GET", "https://example.com/search")
        assert failure.message == "upstream failed"
        assert failure.proxy == "127.0.0.1:8080"

    async def test_an_error_text_quoting_the_url_loses_its_query(self, publish: AsyncMock) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"GET /search?token=abc HTTP/1.1\r\nHost: example.com\r\n\r\n")
        reader.feed_eof()
        writer = MagicMock()
        writer.transport.get_protocol.return_value = MagicMock()
        writer.drain = AsyncMock()
        project = MagicMock(id="project-1", tls_mitm_mode=MitmMode.PLAIN, tls_mitm_engine=None, tls_mitm_browser=None)
        relay = AsyncMock()
        relay.send_request.side_effect = ConnectionError(
            'error sending request for url (https://example.com/search?token=abc)'
        )

        with (
            patch("asyncio.get_running_loop") as loop,
            patch("api.core.mitm.create_relay", return_value=relay),
        ):
            loop.return_value.start_tls = AsyncMock(return_value=MagicMock())
            await MitmHandler(MagicMock()).handle(reader, writer, "example.com", 443, _upstream(8080), project)

        (failure,) = _failures(publish)
        assert "token" not in failure.message
        assert "https://example.com/search" in failure.message
