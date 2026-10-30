# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A tunnel connection from a peer ends up as a CONNECT through the project's upstream."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from blinker import Signal

from api.core.signals import request_completed, tunnel_encrypted_dns_blocked, tunnel_name_unresolved
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol
from api.routing.router import Router
from api.tunnel.dns import FakeIpDirectory, FakeIpPool
from api.tunnel.peers import AddressDirectory
from api.tunnel.transparent import DOT_PORT, TransparentProxyServer
from tests.gateway.doubles import RecordingMetering
from tests.tunnel.test_sniff import client_hello


class _ConnectProxy:
    """An HTTP CONNECT upstream that records the target and echoes the tunnel bytes."""

    def __init__(self) -> None:
        self.targets: list[str] = []
        self.server: asyncio.Server | None = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = await reader.readline()
        self.targets.append(request_line.decode().split(" ")[1])
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        while data := await reader.read(65536):
            writer.write(data)
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


PROJECT = Project(id="proj", name="TVs", username="tv", password="pw")


@dataclass(frozen=True)
class _Peer:
    """A device of no particular tunnel protocol: what the listener needs and nothing more."""

    project_id: str
    name: str
    address: str
    id: str = "peer-1"
    enabled: bool = True
    session_id: str | None = None
    location: LocationTarget | None = None
    created_at: datetime = field(default_factory=datetime.now)

    def model_copy(self, update: dict[str, object]) -> "_Peer":
        from dataclasses import replace

        return replace(self, **update)  # type: ignore[arg-type]


PEER = _Peer(project_id="proj", name="tv", address="127.0.0.1", session_id="sofa", location=LocationTarget(country="DE"))
PeerDirectory = AddressDirectory[_Peer]


@pytest.fixture
async def upstream() -> AsyncIterator[_ConnectProxy]:
    proxy = _ConnectProxy()
    await proxy.start()
    yield proxy
    await proxy.stop()


class _Received:
    """Every emission of a signal while the test runs; the listener reports through signals, not counters."""

    def __init__(self, signal: Signal) -> None:
        self.signal = signal
        self.calls: list[dict[str, Any]] = []

    async def _receive(self, _sender: object, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    def __enter__(self) -> "_Received":
        self.signal.connect(self._receive)
        return self

    def __exit__(self, *_: object) -> None:
        self.signal.disconnect(self._receive)


@pytest.fixture
def name_unresolved() -> Iterator[_Received]:
    with _Received(tunnel_name_unresolved) as received:
        yield received


@pytest.fixture
def encrypted_dns() -> Iterator[_Received]:
    with _Received(tunnel_encrypted_dns_blocked) as received:
        yield received


def _server(
    upstream: _ConnectProxy, destination: tuple[str, int] | None, pool: FakeIpPool, peers: AddressDirectory[_Peer]
) -> tuple[TransparentProxyServer, MagicMock, RecordingMetering]:
    """The listener with the gateway router's surface answering every request with ``upstream``, and recording metering."""
    router = MagicMock(spec=Router)
    router.get_project.return_value = PROJECT
    router.select_proxy_for_project.return_value = Proxy(
        host="127.0.0.1", port=upstream.port, protocol=ProxyProtocol.HTTP, connector_id="c1"
    )
    router.are_all_proxies_quarantined.return_value = False
    router.traffic_limit_status.return_value = None
    metering = RecordingMetering()
    server = TransparentProxyServer(
        router, metering, peers, FakeIpDirectory(pool), port=0, timeout=30.0,
        destination_of=lambda writer: destination, sniff_timeout=0.2,
    )
    return server, router, metering


async def _roundtrip(server: TransparentProxyServer, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    writer.write(payload)
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(len(payload)), 3)
    finally:
        writer.close()


@pytest.mark.asyncio
async def test_fake_ip_destination_is_connected_by_name(upstream: _ConnectProxy) -> None:
    pool = FakeIpPool("198.18.0.0/15")
    fake = str(pool.ip_for("media.example.net"))
    peers = PeerDirectory()
    peers.put(PEER)
    server, router, metering = _server(upstream, (fake, 8443), pool, peers)
    await server.listen("127.0.0.1")
    try:
        with _Received(request_completed) as completed:
            assert await _roundtrip(server, b"hello tunnel") == b"hello tunnel"
            await server.stop()
    finally:
        await server.stop()
    assert upstream.targets == ["media.example.net:8443"]
    router.select_proxy_for_project.assert_awaited_once_with(
        "proj", "sofa", "media.example.net", LocationTarget(country="DE"), pinned_proxy_id=None
    )
    # The completion event names the device, so the request is metered on it
    # as well as on the proxy, connector and project; so is the meter. Both
    # carry the destination too, for the per-host history.
    assert [c["peer_id"] for c in completed.calls] == [PEER.id]
    assert completed.calls[0]["success"] is True and completed.calls[0]["project_id"] == "proj"
    assert completed.calls[0]["target_host"] == "media.example.net"
    asked = metering.meters[0]
    assert (asked.peer_id, asked.target_host) == (PEER.id, "media.example.net")


@pytest.mark.asyncio
async def test_literal_destination_uses_sni_and_keeps_the_hello(upstream: _ConnectProxy) -> None:
    pool = FakeIpPool("198.18.0.0/15")
    peers = PeerDirectory()
    peers.put(PEER)
    server, _, _ = _server(upstream, ("93.184.216.34", 443), pool, peers)
    await server.listen("127.0.0.1")
    hello = client_hello("example.com")
    try:
        assert await _roundtrip(server, hello) == hello
    finally:
        await server.stop()
    assert upstream.targets == ["example.com:443"]


@pytest.mark.asyncio
async def test_literal_destination_without_a_name_goes_by_address(upstream: _ConnectProxy) -> None:
    pool = FakeIpPool("198.18.0.0/15")
    peers = PeerDirectory()
    peers.put(PEER)
    server, _, _ = _server(upstream, ("203.0.113.7", 22), pool, peers)
    await server.listen("127.0.0.1")
    try:
        # SSH: the client waits for the server banner, so sniffing times out and the address is used.
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        await asyncio.sleep(0.4)
        writer.write(b"SSH-2.0-client\r\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(16), 3) == b"SSH-2.0-client\r\n"
        writer.close()
    finally:
        await server.stop()
    assert upstream.targets == ["203.0.113.7:22"]


@pytest.mark.asyncio
async def test_unknown_or_disabled_peer_is_closed_without_bytes(upstream: _ConnectProxy) -> None:
    pool = FakeIpPool("198.18.0.0/15")
    peers = PeerDirectory()
    server, _, _ = _server(upstream, ("203.0.113.7", 80), pool, peers)
    await server.listen("127.0.0.1")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        assert await asyncio.wait_for(reader.read(10), 3) == b""
        writer.close()
        assert server.unknown_peers == 1

        peers.put(PEER.model_copy(update={"enabled": False}))
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        assert await asyncio.wait_for(reader.read(10), 3) == b""
        writer.close()
    finally:
        await server.stop()
    assert upstream.targets == []


@pytest.mark.asyncio
async def test_no_upstream_closes_the_connection(upstream: _ConnectProxy) -> None:
    pool = FakeIpPool("198.18.0.0/15")
    peers = PeerDirectory()
    peers.put(PEER)
    server, router, _ = _server(upstream, ("203.0.113.7", 80), pool, peers)
    router.select_proxy_for_project.return_value = None
    await server.listen("127.0.0.1")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"GET / HTTP/1.1\r\nHost: x.test\r\n\r\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(10), 3) == b""
        writer.close()
    finally:
        await server.stop()
    router.traffic_limit_status.assert_called_once()


@pytest.mark.asyncio
async def test_expired_fake_ip_without_name_is_dropped(upstream: _ConnectProxy) -> None:
    pool = FakeIpPool("198.18.0.0/15")
    peers = PeerDirectory()
    peers.put(PEER)
    server, router, _ = _server(upstream, ("198.18.5.5", 443), pool, peers)
    await server.listen("127.0.0.1")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"\x00\x00\x00")
        assert await asyncio.wait_for(reader.read(10), 3) == b""
        writer.close()
    finally:
        await server.stop()
    router.select_proxy_for_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_encrypted_dns_is_closed_and_counted(upstream: _ConnectProxy, encrypted_dns: _Received) -> None:
    pool = FakeIpPool("198.18.0.0/15")
    fake_doh = str(pool.ip_for("dns.google"))
    peers = PeerDirectory()
    peers.put(PEER)
    # DoH: the device resolved the resolver's name through us, so the fake address names it.
    server, router, _ = _server(upstream, (fake_doh, 443), pool, peers)
    await server.listen("127.0.0.1")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        assert await asyncio.wait_for(reader.read(10), 3) == b""
        writer.close()
    finally:
        await server.stop()
    # DoT: a literal resolver address on port 853, named only by its SNI.
    server2, _, _ = _server(upstream, ("1.1.1.1", DOT_PORT), pool, peers)
    await server2.listen("127.0.0.1")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server2.port)
        writer.write(client_hello("cloudflare-dns.com"))
        await writer.drain()
        assert await asyncio.wait_for(reader.read(10), 3) == b""
        writer.close()
    finally:
        await server2.stop()
    assert upstream.targets == []
    assert encrypted_dns.calls == [{"peer_id": PEER.id}, {"peer_id": PEER.id}]
    router.select_proxy_for_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_encrypted_dns_passes_when_blocking_is_off(upstream: _ConnectProxy) -> None:
    pool = FakeIpPool("198.18.0.0/15")
    peers = PeerDirectory()
    peers.put(PEER)
    server, _, _ = _server(upstream, ("9.9.9.9", DOT_PORT), pool, peers)
    server._block_encrypted_dns = False
    await server.listen("127.0.0.1")
    try:
        hello = client_hello("dns.quad9.net")
        assert await _roundtrip(server, hello) == hello
    finally:
        await server.stop()
    assert upstream.targets == ["dns.quad9.net:853"]


@pytest.mark.asyncio
async def test_by_address_connections_are_counted(upstream: _ConnectProxy, name_unresolved: _Received) -> None:
    pool = FakeIpPool("198.18.0.0/15")
    peers = PeerDirectory()
    peers.put(PEER)
    server, _, _ = _server(upstream, ("203.0.113.7", 22), pool, peers)
    await server.listen("127.0.0.1")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        await asyncio.sleep(0.4)
        writer.write(b"SSH-2.0-client\r\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(16), 3) == b"SSH-2.0-client\r\n"
        writer.close()
    finally:
        await server.stop()
    assert name_unresolved.calls == [{"peer_id": PEER.id}]
