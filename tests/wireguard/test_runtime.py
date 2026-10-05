# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The runtime's lifecycle and peer bookkeeping, without Postgres."""

import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core import utc_now
from api.core.config import Settings
from api.core.stats import TunnelPeerMetricDelta
from api.models.wireguard import WireGuardPeer, WireGuardPeerStatus, WireGuardServerSettings
from api.tunnel.dataplane import TunnelDataPlane
from api.tunnel.peers import NoFreeAddressError
from api.wireguard.peers import PeerDirectory
from api.wireguard.runtime import WireGuardRuntime, defaults_from_config
from tests.wireguard.test_system import FakeRunner


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


async def _runtime(settings: Settings, runner: FakeRunner | None = None, redis: object = None) -> WireGuardRuntime:
    """A runtime on a started data plane, as the lifespan builds them."""
    tunnel = TunnelDataPlane(settings, redis, runner=runner)  # type: ignore[arg-type]
    await tunnel.start(MagicMock())
    return WireGuardRuntime(settings, None, redis, tunnel, runner=runner)  # type: ignore[arg-type]


def test_defaults_seed_from_config() -> None:
    settings = _settings(wireguard_defaults={"endpoint_host": "vpn.example.net", "subnet": "10.77.0.0/24"})
    seeded = defaults_from_config(settings)
    assert seeded.endpoint_host == "vpn.example.net"
    assert seeded.gateway == "10.77.0.1"
    assert len(seeded.private_key) == 44 and seeded.public_key != seeded.private_key

    bad = defaults_from_config(_settings(wireguard_defaults={"subnet": "nonsense"}))
    assert bad.subnet == "10.66.0.0/16"


class TestPeerDirectory:
    def test_allocation_skips_gateway_and_used(self) -> None:
        directory = PeerDirectory(None)
        network = WireGuardServerSettings(private_key="a", public_key="b", subnet="10.66.0.0/29").network
        assert directory.allocate_address(network) == "10.66.0.2"
        directory.put(WireGuardPeer(project_id="p", name="a", public_key="k1", address="10.66.0.2"))
        directory.put(WireGuardPeer(project_id="p", name="b", public_key="k2", address="10.66.0.3"))
        assert directory.allocate_address(network) == "10.66.0.4"
        for i, key in ((4, "k3"), (5, "k4"), (6, "k5")):
            directory.put(WireGuardPeer(project_id="p", name=key, public_key=key, address=f"10.66.0.{i}"))
        with pytest.raises(NoFreeAddressError):
            directory.allocate_address(network)

    def test_address_index_follows_updates(self) -> None:
        directory = PeerDirectory(None)
        peer = WireGuardPeer(id="x", project_id="p", name="a", public_key="k1", address="10.66.0.2")
        directory.put(peer)
        directory.put(peer.model_copy(update={"address": "10.66.0.9"}))
        assert directory.by_address("10.66.0.2") is None
        assert directory.by_address("10.66.0.9") is not None
        assert directory.remove("x") is not None
        assert len(directory) == 0


@pytest.mark.asyncio
async def test_disabled_runtime_only_loads_and_reports() -> None:
    runtime = await _runtime(_settings(), FakeRunner())
    await runtime.start()
    status = await runtime.status()
    assert status.state == "disabled" and status.enabled is False and status.listen_port is None
    # The peers are findable by address through the data plane whether or not this instance carries the tunnel.
    peer = WireGuardPeer(project_id="p", name="tv", public_key="k", address="10.66.0.2")
    await runtime.add_peer(peer)
    assert runtime.peers.by_address("10.66.0.2") is peer
    assert runtime.tunnel.peers.by_address("10.66.0.2") is peer
    assert await runtime.remove_peer(peer.id) is True
    await runtime.stop()
    await runtime.tunnel.stop()


@pytest.mark.asyncio
async def test_enabled_runtime_brings_everything_up_and_down(monkeypatch: pytest.MonkeyPatch) -> None:
    # The listeners bind the gateway address; point it at loopback for the test.
    monkeypatch.setattr(WireGuardServerSettings, "gateway", property(lambda self: "127.0.0.1"))
    runner = FakeRunner()
    settings = _settings(wireguard_enabled=True, tunnel_transparent_port=0, tunnel_dns_port=0, wireguard_listen_port=51999)
    runtime = await _runtime(settings, runner)
    peer = WireGuardPeer(project_id="p", name="tv", public_key="k", address="10.66.0.2")
    runtime.peers.put(peer)

    await runtime.start()
    try:
        assert runtime.state == "running", runtime.error
        tunnel = runtime.tunnel
        assert tunnel.interfaces == {"wg0": "127.0.0.1"}
        assert tunnel.transparent_server is not None and tunnel.transparent_server.is_listening
        assert tunnel.dns_server is not None and tunnel.dns_server.is_listening
        assert ("ip", "link", "add", "dev", "wg0", "type", "wireguard") in runner.calls
        assert "ListenPort = 51999" in runner.conf_files[0]
        assert "AllowedIPs = 10.66.0.2/32" in runner.conf_files[0]
        assert runner.calls[-1] == ("nft", "-f", "-")
        ruleset = runner.stdin[-1].decode()
        assert 'iifname { "wg0" }' in ruleset
        assert f"redirect to :{tunnel.transparent_server.port}" in ruleset
        assert f"redirect to :{tunnel.dns_server.port}" in ruleset

        status = await runtime.status()
        assert status.backend == "kernel" and status.listen_port == 51999 and status.peers_total == 1
        assert status.block_encrypted_dns is True and status.connections_by_address == 0

        # A peer change re-syncs the interface.
        await runtime.update_peer(peer.model_copy(update={"enabled": False}))
        assert runner.calls[-1][:2] == ("wg", "syncconf")
        assert "[Peer]" not in runner.conf_files[-1]
    finally:
        await runtime.stop()
    assert runtime.state == "stopped"
    assert runtime.tunnel.interfaces == {}
    assert ("nft", "delete", "table", "inet", "octoprox_tunnel") in runner.calls
    assert runner.calls[-1] == ("ip", "link", "del", "dev", "wg0")
    await runtime.tunnel.stop()


@pytest.mark.asyncio
async def test_failure_to_bring_up_is_reported_not_fatal() -> None:
    runner = FakeRunner(failures={"ip link add": "RTNETLINK answers: Operation not permitted"})
    runtime = await _runtime(_settings(wireguard_enabled=True), runner)
    await runtime.start()
    assert runtime.state == "failed"
    assert runtime.error is not None and "CAP_NET_ADMIN" in runtime.error
    assert runtime.tunnel.interfaces == {}
    status = await runtime.status()
    assert status.state == "failed"
    await runtime.stop()
    await runtime.tunnel.stop()


@pytest.mark.asyncio
async def test_peer_statuses_from_redis() -> None:
    redis = MagicMock()
    now = int(time.time())
    # Two carriers behind a UDP load balancer: every peer appears in both dumps,
    # and the one with the newer handshake is where the device's traffic lands.
    redis.get_tunnel_peer_status = AsyncMock(return_value=[
        {
            "online": json.dumps({"latest_handshake": now - 30, "rx_bytes": 5, "tx_bytes": 7, "endpoint": "1.2.3.4:1"}),
            "stale": json.dumps({"latest_handshake": now - 3600, "rx_bytes": 0, "tx_bytes": 0, "endpoint": None}),
            "never": json.dumps({"latest_handshake": 0}),
            "junk": "{not json",
        },
        {
            "online": json.dumps({"latest_handshake": now - 900, "rx_bytes": 99, "tx_bytes": 99, "endpoint": "1.2.3.4:2"}),
            "stale": json.dumps({"latest_handshake": now - 60, "rx_bytes": 3, "tx_bytes": 4, "endpoint": "5.6.7.8:9"}),
        },
    ])
    runtime = await _runtime(_settings(), redis=redis)
    statuses = await runtime.peer_statuses()
    assert statuses["online"].online is True and statuses["online"].rx_bytes == 5 and statuses["online"].live is True
    assert statuses["stale"].online is True and statuses["stale"].endpoint == "5.6.7.8:9"
    assert statuses["never"].online is False and statuses["never"].last_handshake_at is None
    assert "junk" not in statuses


class TestPeerStatus:
    """The live reading when a carrier has one; the persisted sighting otherwise, or when it is newer."""

    def test_nothing_carrying_falls_back_to_the_row(self) -> None:
        seen = utc_now() - timedelta(seconds=30)
        peer = WireGuardPeer(project_id="p", name="tv", public_key="k", address="10.66.0.2", last_handshake_at=seen, last_endpoint="9.9.9.9:1")
        status = WireGuardRuntime.peer_status(peer, None)
        assert status.online is True and status.live is False
        assert status.last_handshake_at == seen and status.endpoint == "9.9.9.9:1"
        assert (status.rx_bytes, status.tx_bytes) == (0, 0)
        never = WireGuardRuntime.peer_status(WireGuardPeer(project_id="p", name="tv", public_key="k", address="10.66.0.3"), None)
        assert never.online is False and never.last_handshake_at is None and never.endpoint is None

    def test_restarted_carrier_without_a_handshake_keeps_the_last_sighting(self) -> None:
        seen = utc_now() - timedelta(hours=2)
        peer = WireGuardPeer(project_id="p", name="tv", public_key="k", address="10.66.0.2", last_handshake_at=seen, last_endpoint="9.9.9.9:1")
        live = WireGuardPeerStatus(online=False, last_handshake_at=None, live=True, rx_bytes=12, tx_bytes=34)
        status = WireGuardRuntime.peer_status(peer, live)
        assert status.live is False and status.last_handshake_at == seen and status.online is False
        # The interface counters are still the carrier's.
        assert (status.rx_bytes, status.tx_bytes) == (12, 34)

    def test_live_reading_wins_when_it_is_newer(self) -> None:
        seen = utc_now() - timedelta(minutes=10)
        peer = WireGuardPeer(project_id="p", name="tv", public_key="k", address="10.66.0.2", last_handshake_at=seen)
        live = WireGuardPeerStatus(online=True, last_handshake_at=seen + timedelta(minutes=9), endpoint="1.1.1.1:5", live=True)
        assert WireGuardRuntime.peer_status(peer, live) is live
        older = WireGuardPeerStatus(online=False, last_handshake_at=seen - timedelta(days=1), endpoint="old", live=True)
        assert WireGuardRuntime.peer_status(peer, older).last_handshake_at == seen


@pytest.mark.asyncio
async def test_publish_status_persists_new_handshakes() -> None:
    """The carrier records a handshake newer than the row knows, and the cache follows."""
    redis = MagicMock()
    redis.set_tunnel_peer_status = AsyncMock()
    runtime = await _runtime(_settings(), FakeRunner(), redis=redis)
    # FakeRunner's dump: peer "pub" handshaked at 1700000000 from 1.2.3.4:5.
    peer = WireGuardPeer(project_id="p", name="tv", public_key="pub", address="10.66.0.2")
    runtime.peers.put(peer)
    assert await runtime._publish_status() is True
    published = redis.set_tunnel_peer_status.await_args.args[2]
    assert json.loads(published["pub"])["endpoint"] == "1.2.3.4:5"
    assert "connections_by_address" not in json.loads(published["pub"])
    seen = datetime.fromtimestamp(1700000000, UTC).replace(tzinfo=None)
    assert peer.last_handshake_at == seen and peer.last_endpoint == "1.2.3.4:5"
    # Nothing newer on the next tick: the row is left alone (no session factory here, so the cache is the proof).
    peer.last_endpoint = "unchanged"
    await runtime._publish_status()
    assert peer.last_endpoint == "unchanged"


@pytest.mark.asyncio
async def test_status_sums_device_metrics_from_the_manager() -> None:
    runtime = await _runtime(_settings(), FakeRunner())
    runtime.peers.put(WireGuardPeer(id="a", project_id="p", name="a", public_key="ka", address="10.66.0.2"))
    runtime.peers.put(WireGuardPeer(id="b", project_id="p", name="b", public_key="kb", address="10.66.0.3"))
    totals = {"a": TunnelPeerMetricDelta(by_address=2, encrypted_dns_blocked=1), "b": TunnelPeerMetricDelta(by_address=3)}
    runtime.peer_metrics = lambda peer_id: totals.get(peer_id) or TunnelPeerMetricDelta()
    status = await runtime.status()
    assert (status.connections_by_address, status.encrypted_dns_blocked) == (5, 1)
    assert runtime.metrics_of("nope") == TunnelPeerMetricDelta()
