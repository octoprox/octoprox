# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the exit headers: a project that asks is told which exit served each request.

They go on the 200 answering a CONNECT, which only the client reads, and on a plain HTTP response, where the upstream's
own ``X-Octoprox-*`` headers are dropped so the client can trust them.
"""

import asyncio
import base64
import contextlib
from collections.abc import AsyncIterator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from api.core.proxy_server import ProxyServer
from api.geo.verifier import ExitDecision, ExitIp, ExitSource
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol
from api.routing.router import Router
from tests.core.test_proxy_server_failures import _exchange, _upstream, _vendor, _verifier
from tests.core.test_proxy_server_http_forward import CLIENT_HEADERS, _Capture, _ClientWriter
from tests.gateway.doubles import QuietMetering

PROJECT = Project(id="project-1", name="Scraping", username="proj", password="secret", exit_headers=True)
OFF = PROJECT.model_copy(update={"exit_headers": False})
STAMP = datetime(2026, 10, 27, 9, 30, 5)
ECHOED = ExitIp("81.2.69.160", ExitSource.PREFLIGHT, STAMP)


def _slot(**metadata: str) -> Proxy:
    return Proxy(id="slot-1", host="gw", port=8000, connector_id="connector-1", metadata=dict(metadata))


def _router(project: Project, proxy: Proxy) -> MagicMock:
    router = MagicMock(spec=Router)
    router.get_project_by_username.side_effect = lambda u: project if u == project.username else None
    router.select_proxy_for_project.return_value = proxy
    return router


def _auth() -> str:
    return f"Proxy-Authorization: Basic {base64.b64encode(b'proj-sessid-s1-cc-gb:secret').decode()}\r\n"


async def _connect(project: Project, decision: ExitDecision) -> bytes:
    """The head of the answer to a CONNECT ``decision`` lets through."""
    server = ProxyServer(
        _router(project, decision.proxy), QuietMetering(), host="127.0.0.1", port=0, timeout=30.0,
        exit_verifier=_verifier(decision),
    )
    await server.start()
    try:
        response = await _exchange(server, f"CONNECT example.com:443 HTTP/1.1\r\n{_auth()}\r\n", then=b"hello")
    finally:
        await server.stop()
    return response.split(b"\r\n\r\n", 1)[0]


class TestHeaderLines:
    def test_off_names_nothing(self) -> None:
        assert ProxyServer._exit_headers(OFF, ExitDecision(_slot(), exit_ip=ECHOED)) == b""

    def test_an_echoed_exit(self) -> None:
        assert ProxyServer._exit_headers(PROJECT, ExitDecision(_slot(), exit_ip=ECHOED)) == (
            b"X-Octoprox-Proxy-Id: slot-1\r\n"
            b"X-Octoprox-Exit-Ip: 81.2.69.160\r\n"
            b"X-Octoprox-Exit-Source: preflight\r\n"
            b"X-Octoprox-Exit-Checked-At: 2026-10-27T09:30:05Z\r\n"
        )

    def test_without_an_echo_the_rows_last_exit_is_named(self) -> None:
        slot = _slot(discovered_ip="203.0.113.9")
        assert ProxyServer._exit_headers(PROJECT, ExitDecision(slot)) == (
            b"X-Octoprox-Proxy-Id: slot-1\r\n"
            b"X-Octoprox-Exit-Ip: 203.0.113.9\r\n"
            b"X-Octoprox-Exit-Source: last-seen\r\n"
        )

    def test_an_unknown_exit_names_the_proxy_alone(self) -> None:
        gateway = _slot(dynamic_sessions="true")
        assert ProxyServer._exit_headers(PROJECT, ExitDecision(gateway)) == b"X-Octoprox-Proxy-Id: slot-1\r\n"


class TestConnect:
    async def test_the_200_carries_the_exit(self) -> None:
        async with _vendor(b"HTTP/1.1 200 Connection established\r\n\r\n") as port:
            upstream = _upstream(port)
            head = await _connect(PROJECT, ExitDecision(upstream, exit_ip=ECHOED))
        assert head.decode().split("\r\n") == [
            "HTTP/1.1 200 Connection Established",
            f"X-Octoprox-Proxy-Id: {upstream.id}",
            "X-Octoprox-Exit-Ip: 81.2.69.160",
            "X-Octoprox-Exit-Source: preflight",
            "X-Octoprox-Exit-Checked-At: 2026-10-27T09:30:05Z",
        ]

    async def test_off_answers_the_bare_200(self) -> None:
        async with _vendor(b"HTTP/1.1 200 Connection established\r\n\r\n") as port:
            head = await _connect(OFF, ExitDecision(_upstream(port), exit_ip=ECHOED))
        assert head == b"HTTP/1.1 200 Connection Established"


class TestPlainHttp:
    @staticmethod
    async def _forward(project: Project, proxy: Proxy) -> _ClientWriter:
        server = ProxyServer(MagicMock(spec=Router), QuietMetering(), host="127.0.0.1", port=0, timeout=30.0)
        server._get_upstream_proxy = AsyncMock(return_value=proxy)  # type: ignore[method-assign]
        client_reader = asyncio.StreamReader()
        client_reader.feed_eof()
        client = _ClientWriter()
        await server._handle_http(
            client_reader, client,  # type: ignore[arg-type]
            "GET", "http://ip-api.com/", "HTTP/1.1", dict(CLIENT_HEADERS),
            project=project, session_id="s1", location=LocationTarget(country="GB"),
        )
        return client

    @staticmethod
    @contextlib.asynccontextmanager
    async def _upstream_answering(response: bytes) -> AsyncIterator[Proxy]:
        capture = _Capture(response)
        await capture.start()
        try:
            yield Proxy(
                id="slot-1", host="127.0.0.1", port=capture.port, protocol=ProxyProtocol.HTTP, connector_id="connector-1",
                metadata={"discovered_ip": "203.0.113.9", "location_checked_at": "2026-10-27T09:30:05"},
            )
        finally:
            await capture.stop()

    async def test_the_response_carries_the_exit_and_drops_the_upstreams_own(self) -> None:
        spoofed = b"HTTP/1.1 200 OK\r\nX-Octoprox-Exit-Ip: 6.6.6.6\r\nX-Other: kept\r\nContent-Length: 2\r\n\r\nok"
        async with self._upstream_answering(spoofed) as proxy:
            client = await self._forward(PROJECT, proxy)
        assert client.header_lines() == [
            "X-Other: kept",
            "Content-Length: 2",
            "X-Octoprox-Proxy-Id: slot-1",
            "X-Octoprox-Exit-Ip: 203.0.113.9",
            "X-Octoprox-Exit-Source: last-seen",
            "X-Octoprox-Exit-Checked-At: 2026-10-27T09:30:05Z",
            "Connection: close",
        ]
        assert client.body() == b"ok"

    async def test_off_relays_the_response_as_it_came(self) -> None:
        spoofed = b"HTTP/1.1 200 OK\r\nX-Octoprox-Exit-Ip: 6.6.6.6\r\nContent-Length: 2\r\n\r\nok"
        async with self._upstream_answering(spoofed) as proxy:
            client = await self._forward(OFF, proxy)
        assert client.header_lines() == ["X-Octoprox-Exit-Ip: 6.6.6.6", "Content-Length: 2", "Connection: close"]
