# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""How the proxy listener takes, keeps and ends the connections it relays: a deep accept queue, TCP keepalive on every leg, and closes that never wait.

Real sockets throughout: a listener with a stand-in router and metering without Redis, and clients that misbehave on purpose.
"""

import asyncio
import base64
import socket
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from api.core import proxy_server
from api.core.proxy_server import (
    KEEPALIVE_IDLE_SECONDS,
    KEEPALIVE_INTERVAL_SECONDS,
    KEEPALIVE_PROBES,
    LISTEN_BACKLOG,
    USER_TIMEOUT_MS,
    ProxyServer,
    keep_alive,
)
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol
from api.routing.inflight import InFlight
from api.routing.router import Router
from tests.gateway.doubles import QuietMetering

PROJECT = Project(name="Scraping", username="proj", password="secret")
AUTH = "Proxy-Authorization: Basic " + base64.b64encode(b"proj:secret").decode() + "\r\n"
# The idle-time option: TCP_KEEPIDLE on Linux, TCP_KEEPALIVE on macOS.
TCP_KEEPIDLE: int = getattr(socket, "TCP_KEEPIDLE", getattr(socket, "TCP_KEEPALIVE", 0))
# Linux only; None where the platform has no user timeout.
TCP_USER_TIMEOUT: int | None = getattr(socket, "TCP_USER_TIMEOUT", None)


def _server(vendor_port: int, inflight: InFlight) -> ProxyServer:
    """A listener that routes every request to the vendor at ``vendor_port``."""
    router = MagicMock(spec=Router)
    router.get_project_by_username.side_effect = lambda username: PROJECT if username == PROJECT.username else None
    router.select_proxy_for_project.return_value = Proxy(
        host="127.0.0.1", port=vendor_port, protocol=ProxyProtocol.HTTP, connector_id="connector-1"
    )
    metering = QuietMetering(inflight)
    return ProxyServer(router, metering, host="127.0.0.1", port=0, timeout=5.0)


def _keepalive_of(writer: asyncio.StreamWriter) -> tuple[bool, int, int, int, int | None]:
    """(keepalive on, idle seconds, probe interval, probes, user timeout in ms) of the socket under ``writer``."""
    sock = writer.get_extra_info("socket")
    return (
        sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0,
        sock.getsockopt(socket.IPPROTO_TCP, TCP_KEEPIDLE),
        sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL),
        sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT),
        sock.getsockopt(socket.IPPROTO_TCP, TCP_USER_TIMEOUT) if TCP_USER_TIMEOUT is not None else None,
    )


EXPECTED = (
    True,
    KEEPALIVE_IDLE_SECONDS,
    KEEPALIVE_INTERVAL_SECONDS,
    KEEPALIVE_PROBES,
    USER_TIMEOUT_MS if TCP_USER_TIMEOUT is not None else None,
)


@pytest.fixture
async def vendor() -> AsyncIterator[int]:
    """An upstream HTTP proxy: answers a CONNECT with 200 and then floods the tunnel; a plain request with ``ok``."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = await reader.readline()
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        try:
            if not request_line.startswith(b"CONNECT"):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            while True:
                writer.write(b"x" * 65536)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()


class TestListening:
    async def test_a_burst_of_new_connections_can_wait_to_be_accepted(self, vendor: int) -> None:
        server = _server(vendor, InFlight())

        with patch.object(asyncio, "start_server", wraps=asyncio.start_server) as start_server:
            await server.start()
        await server.stop()

        assert start_server.call_args.kwargs["backlog"] == LISTEN_BACKLOG


class TestKeepAlive:
    async def test_both_legs_of_a_tunnel_are_probed_when_idle(self, vendor: int) -> None:
        server = _server(vendor, InFlight())
        legs: list[tuple[bool, int, int, int, int | None]] = []

        async def tunnel(_client_reader: Any, client_writer: Any, _upstream_reader: Any, upstream_writer: Any,
                         meter: Any = None) -> tuple[int, int]:
            legs.extend(_keepalive_of(leg) for leg in (client_writer, upstream_writer))
            return 0, 0

        await server.start()
        try:
            with patch.object(server, "_tunnel", tunnel):
                reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                writer.write(f"CONNECT example.com:443 HTTP/1.1\r\n{AUTH}\r\n".encode())
                await writer.drain()
                assert (await asyncio.wait_for(reader.read(), 5)).startswith(b"HTTP/1.1 200")
                writer.close()
        finally:
            await server.stop()

        assert legs == [EXPECTED, EXPECTED]

    async def test_a_plain_request_probes_its_upstream_and_its_client(self, vendor: int) -> None:
        server = _server(vendor, InFlight())
        legs: list[tuple[bool, int, int, int, int | None]] = []

        def recording(writer: asyncio.StreamWriter) -> None:
            keep_alive(writer)
            legs.append(_keepalive_of(writer))

        await server.start()
        try:
            with patch.object(proxy_server, "keep_alive", recording):
                reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
                writer.write(f"GET http://example.com/ HTTP/1.1\r\n{AUTH}\r\n".encode())
                await writer.drain()
                assert (await asyncio.wait_for(reader.read(), 5)).endswith(b"ok")
                writer.close()
        finally:
            await server.stop()

        assert legs == [EXPECTED, EXPECTED]

    def test_a_socket_that_takes_no_tcp_options_is_left_as_it_is(self) -> None:
        left, right = socket.socketpair(socket.AF_UNIX)
        with left, right:
            writer = MagicMock(spec=asyncio.StreamWriter)
            writer.get_extra_info.return_value = left
            keep_alive(writer)


class TestClosing:
    async def test_a_client_that_stopped_reading_does_not_hold_shutdown(
        self, vendor: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(proxy_server, "CLOSE_TIMEOUT_SECONDS", 0.2)
        inflight = InFlight()
        server = _server(vendor, inflight)
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(f"CONNECT example.com:443 HTTP/1.1\r\n{AUTH}\r\n".encode())
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        # The client reads no more while the vendor keeps sending: every buffer
        # on the way fills, and the gateway holds what the client will not take.
        await asyncio.sleep(0.3)
        assert inflight.total == 1

        await asyncio.wait_for(server.stop(), 2.0)

        assert server.active_connections == 0 and inflight.total == 0
        writer.close()
