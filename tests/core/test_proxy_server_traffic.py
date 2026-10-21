# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for traffic metering on the proxy server's transfer paths.

A tunnel counts its bytes as they flow, so a keep-alive tunnel is charged against the connector's limit while it is open
and a connector under the interrupt action cuts it mid-stream; the meters report to a recording request metering, so the
tests control when the connector counts as interrupted.
"""

import asyncio
import contextlib
import errno
import socket
import struct
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core.proxy_server import ProxyServer
from api.core.traffic_limiter import PROGRESS_REPORT_BYTES, TrafficMeter
from api.gateway.metering import RequestMetering
from api.routing.router import Router
from tests.gateway.doubles import RecordingMetering


def _server() -> ProxyServer:
    """A listener whose transfer loops are driven directly; its router and metering are never asked."""
    return ProxyServer(MagicMock(spec=Router), MagicMock(spec=RequestMetering), host="127.0.0.1", port=0, timeout=30.0)


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


async def _abort(tunnel: asyncio.Task[Any], *writers: asyncio.StreamWriter) -> None:
    """End a test's tunnel and sockets whatever its asserts did: an open socket holds the pipes' teardown forever."""
    tunnel.cancel()
    for w in writers:
        w.transport.abort()
    await asyncio.gather(tunnel, return_exceptions=True)


class TestTunnelMetering:
    async def test_bytes_are_reported_while_the_tunnel_is_open(self, pipes: list[Pipe]) -> None:
        metering = RecordingMetering()
        meter = TrafficMeter(metering, "proxy", "project", "connector")
        server = _server()
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
        assert metering.reported()[1] >= PROGRESS_REPORT_BYTES
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
        reported_sent, reported_received = metering.reported()
        assert reported_received + rest_received == len(payload)
        assert reported_sent + rest_sent == 5
        await _close(client_server[1], upstream_server[1])

    async def test_interrupt_cuts_both_directions(self, pipes: list[Pipe]) -> None:
        metering = RecordingMetering()
        meter = TrafficMeter(metering, "proxy", "project", "connector")
        server = _server()
        client, client_server, upstream_server, upstream_peer = await _tunnel_setup(pipes)

        tunnel = asyncio.create_task(server._tunnel(
            client_server[0], client_server[1], upstream_server[0], upstream_server[1], meter=meter,
        ))
        upstream_peer[1].write(b"before")
        await upstream_peer[1].drain()
        assert await client[0].readexactly(6) == b"before"

        metering.interrupted = True
        # The next chunk in either direction ends the tunnel, without anyone closing.
        upstream_peer[1].write(b"after")
        await upstream_peer[1].drain()
        await asyncio.wait_for(tunnel, 5)
        await _close(client_server[1], upstream_server[1])
        # The chunk that tripped the meter was still relayed, then the proxy side closed.
        assert await client[0].read() == b"after"
        await _close(client[1], upstream_peer[1])

    async def test_no_meter_keeps_the_old_contract(self, pipes: list[Pipe]) -> None:
        server = _server()
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



class TestClosedLeg:
    async def test_bytes_for_a_leg_that_already_closed_end_the_tunnel_quietly(self) -> None:
        """uvloop raises RuntimeError, not an OSError, for a write to a closed transport."""
        server = _server()
        client_reader = asyncio.StreamReader()
        client_reader.feed_data(b"late bytes")
        client_reader.feed_eof()
        upstream_reader = asyncio.StreamReader()
        upstream_reader.feed_eof()
        closed = MagicMock(spec=asyncio.StreamWriter)
        closed.is_closing.return_value = True
        closed.write.side_effect = RuntimeError("unable to perform operation on <TCPTransport closed=True>")
        client_writer = MagicMock(spec=asyncio.StreamWriter)
        client_writer.is_closing.return_value = False
        client_writer.drain = AsyncMock()

        result = await asyncio.wait_for(server._tunnel(client_reader, client_writer, upstream_reader, closed), 5)

        assert result == (0, 0)
        closed.write.assert_not_called()

    async def test_a_half_close_is_not_passed_on_to_a_leg_already_closing(self) -> None:
        """uvloop raises RuntimeError for write_eof on a closed transport: a CONNECT error, the other direction left running."""
        server = _server()
        client_reader = asyncio.StreamReader()
        client_reader.feed_eof()
        upstream_reader = asyncio.StreamReader()
        upstream_reader.feed_eof()
        closing = MagicMock(spec=asyncio.StreamWriter)
        closing.is_closing.return_value = True
        closing.write_eof.side_effect = RuntimeError("unable to perform operation on <TCPTransport closed=True>")
        client_writer = MagicMock(spec=asyncio.StreamWriter)
        client_writer.is_closing.return_value = False

        result = await asyncio.wait_for(server._tunnel(client_reader, client_writer, upstream_reader, closing), 5)

        assert result == (0, 0)
        closing.write_eof.assert_not_called()
        client_writer.write_eof.assert_called_once()


class TestFailedLeg:
    async def test_a_failed_leg_ends_the_tunnel_without_waiting_on_the_other_peer(self) -> None:
        """A leg the kernel gave up on can neither send nor receive, so a vendor that never speaks is not waited on."""
        server = _server()
        device = asyncio.StreamReader()
        device.set_exception(TimeoutError(errno.ETIMEDOUT, "Connection timed out"))
        silent_vendor = asyncio.StreamReader()
        vendor_writer = MagicMock(spec=asyncio.StreamWriter)
        vendor_writer.is_closing.return_value = False
        device_writer = MagicMock(spec=asyncio.StreamWriter)
        device_writer.is_closing.return_value = False

        result = await asyncio.wait_for(server._tunnel(device, device_writer, silent_vendor, vendor_writer), 5)

        assert result == (0, 0)
        device_writer.write.assert_not_called()

    async def test_a_direction_cut_while_draining_counts_its_last_chunk(self) -> None:
        """Bytes are counted as they are handed on, so the chunk written to the device before the cut is metered."""
        meter = TrafficMeter(RecordingMetering(), "proxy", "project", "connector")
        server = _server()
        device = asyncio.StreamReader()
        vendor = asyncio.StreamReader()
        vendor.feed_data(b"x" * 1000)
        device_writer = MagicMock(spec=asyncio.StreamWriter)
        device_writer.is_closing.return_value = False
        device_writer.drain = asyncio.Event().wait  # a device that never takes the chunk
        vendor_writer = MagicMock(spec=asyncio.StreamWriter)
        vendor_writer.is_closing.return_value = False
        tunnel = asyncio.create_task(server._tunnel(device, device_writer, vendor, vendor_writer, meter=meter))
        while not device_writer.write.called:
            await asyncio.sleep(0)

        device.set_exception(TimeoutError(errno.ETIMEDOUT, "Connection timed out"))

        assert await asyncio.wait_for(tunnel, 5) == (0, 1000)

    async def test_a_reset_vendor_ends_the_tunnel_with_the_device_still_open(self, pipes: list[Pipe]) -> None:
        """The device's direction is cut too, over real sockets, with what crossed before counted."""
        meter = TrafficMeter(RecordingMetering(), "proxy", "project", "connector")
        server = _server()
        device_pipe, vendor_pipe = Pipe(), Pipe()
        pipes.extend((device_pipe, vendor_pipe))
        device, client_side = await device_pipe.open()
        upstream_side, vendor = await vendor_pipe.open()
        tunnel = asyncio.create_task(server._tunnel(*client_side, *upstream_side, meter=meter))
        try:
            vendor[1].write(b"hi")
            await vendor[1].drain()
            assert await device[0].readexactly(2) == b"hi"

            # A reset, not asyncio's abort alone, which closes with a FIN: a half-close the device is waited on for.
            vendor[1].get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            vendor[1].transport.abort()

            assert await asyncio.wait_for(tunnel, 5) == (0, 2)
        finally:
            await _abort(tunnel, device[1], client_side[1], upstream_side[1], vendor[1])


class TestHalfClose:
    async def test_a_vendor_half_close_keeps_the_device_direction_running(self, pipes: list[Pipe]) -> None:
        """A natural end is passed on, not a cut: the device still sends, and the tunnel ends once it closes too."""
        meter = TrafficMeter(RecordingMetering(), "proxy", "project", "connector")
        server = _server()
        device_pipe, vendor_pipe = Pipe(), Pipe()
        pipes.extend((device_pipe, vendor_pipe))
        device, client_side = await device_pipe.open()
        upstream_side, vendor = await vendor_pipe.open()
        tunnel = asyncio.create_task(server._tunnel(*client_side, *upstream_side, meter=meter))
        try:
            vendor[1].write_eof()
            assert await asyncio.wait_for(device[0].read(), 5) == b""
            device[1].write(b"late")
            await device[1].drain()
            assert await asyncio.wait_for(vendor[0].readexactly(4), 5) == b"late"
            assert not tunnel.done()

            device[1].write_eof()

            assert await asyncio.wait_for(tunnel, 5) == (4, 0)
        finally:
            await _abort(tunnel, device[1], client_side[1], upstream_side[1], vendor[1])

class TestChunkedForwarding:
    async def test_chunked_body_is_metered_and_forwarded(self, pipes: list[Pipe]) -> None:
        metering = RecordingMetering()
        meter = TrafficMeter(metering, "proxy", "project", "connector")
        server = _server()
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
