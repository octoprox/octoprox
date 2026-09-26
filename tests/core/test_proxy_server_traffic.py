# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for traffic metering on the proxy server's transfer paths.

A tunnel counts its bytes as they flow, so a keep-alive tunnel is charged
against the connector's limit while it is open, and a connector under the
interrupt action cuts it mid-stream. The limiter is stubbed so the tests
control when the connector counts as interrupted.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from api.core.proxy_server import ProxyServer
from api.core.traffic_limiter import PROGRESS_REPORT_BYTES, TrafficMeter


class StubLimiter:
    """Just enough of TrafficLimiter for a meter to run against."""

    def __init__(self) -> None:
        self.interrupted = False
        self.reports: list[tuple[int, int]] = []

    def is_interrupted(self, connector_id: str) -> bool:
        return self.interrupted

    def limit_status_for(self, connector_id: str) -> int:
        return 509

    def progress(self, proxy_id: str, project_id: str, connector_id: str, sent: int, received: int) -> None:
        self.reports.append((sent, received))


class Pipe:
    """Two connected stream pairs over a real socket, so half-closes behave."""

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self._accepted: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
            asyncio.get_running_loop().create_future()
        )

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._accepted.set_result((reader, writer))

    async def open(self) -> tuple[
        tuple[asyncio.StreamReader, asyncio.StreamWriter],
        tuple[asyncio.StreamReader, asyncio.StreamWriter],
    ]:
        self.server = await asyncio.start_server(self._on_connect, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        client = await asyncio.open_connection("127.0.0.1", port)
        server_side = await self._accepted
        return client, server_side

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


@pytest.fixture
async def pipes() -> AsyncIterator[list[Pipe]]:
    created: list[Pipe] = []
    yield created
    for p in created:
        await p.close()


async def _tunnel_setup(pipes: list[Pipe]) -> tuple[Any, Any, Any, Any]:
    """(client_end, server_end_for_client, upstream_end_for_server, upstream_peer)."""
    client_pipe, upstream_pipe = Pipe(), Pipe()
    pipes += [client_pipe, upstream_pipe]
    client_end, client_server_end = await client_pipe.open()
    upstream_server_end, upstream_peer = await upstream_pipe.open()
    return client_end, client_server_end, upstream_server_end, upstream_peer


async def _close(*writers: asyncio.StreamWriter) -> None:
    for w in writers:
        w.close()
    for w in writers:
        with contextlib.suppress(Exception):
            await w.wait_closed()


class TestTunnelMetering:
    async def test_bytes_are_reported_while_the_tunnel_is_open(self, pipes: list[Pipe]) -> None:
        limiter = StubLimiter()
        meter = TrafficMeter(limiter, "proxy", "project", "connector")  # type: ignore[arg-type]
        server = ProxyServer(proxy_manager=MagicMock())
        client, client_server, upstream_server, upstream_peer = await _tunnel_setup(pipes)

        tunnel = asyncio.create_task(server._tunnel(
            client_server[0], client_server[1], upstream_server[0], upstream_server[1], meter=meter,
        ))
        # The upstream streams two megabytes down; the tunnel is still open.
        # Drain concurrently with the read: the socket buffers are smaller
        # than the payload, so awaiting the drain first would deadlock.
        payload = b"x" * (2 * PROGRESS_REPORT_BYTES)
        upstream_peer[1].write(payload)
        drain = asyncio.create_task(upstream_peer[1].drain())
        got = await client[0].readexactly(len(payload))
        await drain
        assert got == payload
        assert sum(r for _s, r in limiter.reports) >= PROGRESS_REPORT_BYTES
        assert not tunnel.done()

        # Client sends a little upstream, then both sides close.
        client[1].write(b"hello")
        await client[1].drain()
        assert await upstream_peer[0].readexactly(5) == b"hello"
        await _close(client[1], upstream_peer[1])
        sent, received = await asyncio.wait_for(tunnel, 5)
        assert (sent, received) == (5, len(payload))
        # Whatever was not reported during the tunnel comes out of finish, no more.
        rest_sent, rest_received = meter.finish()
        reported_received = sum(r for _s, r in limiter.reports)
        assert reported_received + rest_received == len(payload)
        assert sum(s for s, _r in limiter.reports) + rest_sent == 5
        await _close(client_server[1], upstream_server[1])

    async def test_interrupt_cuts_both_directions(self, pipes: list[Pipe]) -> None:
        limiter = StubLimiter()
        meter = TrafficMeter(limiter, "proxy", "project", "connector")  # type: ignore[arg-type]
        server = ProxyServer(proxy_manager=MagicMock())
        client, client_server, upstream_server, upstream_peer = await _tunnel_setup(pipes)

        tunnel = asyncio.create_task(server._tunnel(
            client_server[0], client_server[1], upstream_server[0], upstream_server[1], meter=meter,
        ))
        upstream_peer[1].write(b"before")
        await upstream_peer[1].drain()
        assert await client[0].readexactly(6) == b"before"

        limiter.interrupted = True
        # The next chunk in either direction ends the tunnel, without anyone closing.
        upstream_peer[1].write(b"after")
        await upstream_peer[1].drain()
        await asyncio.wait_for(tunnel, 5)
        await _close(client_server[1], upstream_server[1])
        # The chunk that tripped the meter was still relayed, then the proxy side closed.
        assert await client[0].read() == b"after"
        await _close(client[1], upstream_peer[1])

    async def test_no_meter_keeps_the_old_contract(self, pipes: list[Pipe]) -> None:
        server = ProxyServer(proxy_manager=MagicMock())
        client, client_server, upstream_server, upstream_peer = await _tunnel_setup(pipes)
        tunnel = asyncio.create_task(server._tunnel(
            client_server[0], client_server[1], upstream_server[0], upstream_server[1],
        ))
        client[1].write(b"ping")
        await client[1].drain()
        assert await upstream_peer[0].readexactly(4) == b"ping"
        upstream_peer[1].write(b"pong!")
        await upstream_peer[1].drain()
        assert await client[0].readexactly(5) == b"pong!"
        await _close(client[1], upstream_peer[1])
        assert await asyncio.wait_for(tunnel, 5) == (4, 5)
        await _close(client_server[1], upstream_server[1])


class TestChunkedForwarding:
    async def test_chunked_body_is_metered_and_forwarded(self, pipes: list[Pipe]) -> None:
        limiter = StubLimiter()
        meter = TrafficMeter(limiter, "proxy", "project", "connector")  # type: ignore[arg-type]
        server = ProxyServer(proxy_manager=MagicMock())
        pipe = Pipe()
        pipes.append(pipe)
        (reader_side, writer_side), (peer_reader, peer_writer) = await pipe.open()
        body = b"5\r\nhello\r\n0\r\n\r\n"
        peer_writer.write(body)
        await peer_writer.drain()
        out_pipe = Pipe()
        pipes.append(out_pipe)
        (client_reader, _client_writer), (_in_reader, out_writer) = await out_pipe.open()
        total = await server._forward_chunked(reader_side, out_writer, meter)
        assert total == len(body)
        assert meter.received == len(body)
        assert await client_reader.readexactly(len(body)) == body
        await _close(writer_side, peer_writer, out_writer, _client_writer)
