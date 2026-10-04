# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The runtime's lifecycle and peer bookkeeping, without Postgres."""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core.config import Settings
from api.models.wireguard import WireGuardPeer, WireGuardServerSettings
from api.wireguard.peers import NoFreeAddressError, PeerDirectory
from api.wireguard.runtime import WireGuardRuntime, defaults_from_config
from tests.wireguard.test_system import FakeRunner


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


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
    runtime = WireGuardRuntime(_settings(), None, None, runner=FakeRunner())
    await runtime.start(MagicMock())
    status = await runtime.status()
    assert status.state == "disabled" and status.enabled is False and status.listen_port is None
    peer = WireGuardPeer(project_id="p", name="tv", public_key="k", address="10.66.0.2")
    await runtime.add_peer(peer)
    assert runtime.peers.by_address("10.66.0.2") is peer
    assert await runtime.remove_peer(peer.id) is True
    await runtime.stop()


@pytest.mark.asyncio
async def test_enabled_runtime_brings_everything_up_and_down(monkeypatch: pytest.MonkeyPatch) -> None:
    # The listeners bind the gateway address; point it at loopback for the test.
    monkeypatch.setattr(WireGuardServerSettings, "gateway", property(lambda self: "127.0.0.1"))
    runner = FakeRunner()
    settings = _settings(wireguard_enabled=True, wireguard_transparent_port=0, wireguard_dns_port=0, wireguard_listen_port=51999)
    runtime = WireGuardRuntime(settings, None, None, runner=runner)
    peer = WireGuardPeer(project_id="p", name="tv", public_key="k", address="10.66.0.2")
    runtime.peers.put(peer)

    await runtime.start(MagicMock())
    try:
        assert runtime.state == "running", runtime.error
        assert runtime.transparent_server is not None and runtime.transparent_server.is_listening
        assert runtime.dns_server is not None and runtime.dns_server.is_listening
        assert ("ip", "link", "add", "dev", "wg0", "type", "wireguard") in runner.calls
        assert "ListenPort = 51999" in runner.conf_files[0]
        assert "AllowedIPs = 10.66.0.2/32" in runner.conf_files[0]
        assert runner.calls[-1] == ("nft", "-f", "-")
        ruleset = runner.stdin[-1].decode()
        assert f"redirect to :{runtime.transparent_server.port}" in ruleset
        assert f"redirect to :{runtime.dns_server.port}" in ruleset

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
    assert ("nft", "delete", "table", "inet", "octoprox_wg") in runner.calls
    assert runner.calls[-1] == ("ip", "link", "del", "dev", "wg0")


@pytest.mark.asyncio
async def test_failure_to_bring_up_is_reported_not_fatal() -> None:
    runner = FakeRunner(failures={"ip link add": "RTNETLINK answers: Operation not permitted"})
    runtime = WireGuardRuntime(_settings(wireguard_enabled=True), None, None, runner=runner)
    await runtime.start(MagicMock())
    assert runtime.state == "failed"
    assert runtime.error is not None and "CAP_NET_ADMIN" in runtime.error
    status = await runtime.status()
    assert status.state == "failed"
    await runtime.stop()


@pytest.mark.asyncio
async def test_peer_statuses_from_redis() -> None:
    redis = MagicMock()
    now = int(time.time())
    # Two carriers behind a UDP load balancer: every peer appears in both dumps,
    # and the one with the newer handshake is where the device's traffic lands.
    redis.get_wireguard_peer_status = AsyncMock(return_value=[
        {
            "online": json.dumps({"latest_handshake": now - 30, "rx_bytes": 5, "tx_bytes": 7, "endpoint": "1.2.3.4:1", "connections_by_address": 3, "encrypted_dns_blocked": 2}),
            "stale": json.dumps({"latest_handshake": now - 3600, "rx_bytes": 0, "tx_bytes": 0, "endpoint": None}),
            "never": json.dumps({"latest_handshake": 0}),
            "junk": "{not json",
        },
        {
            "online": json.dumps({"latest_handshake": now - 900, "rx_bytes": 99, "tx_bytes": 99, "endpoint": "1.2.3.4:2"}),
            "stale": json.dumps({"latest_handshake": now - 60, "rx_bytes": 3, "tx_bytes": 4, "endpoint": "5.6.7.8:9"}),
        },
    ])
    runtime = WireGuardRuntime(_settings(), None, redis)
    statuses = await runtime.peer_statuses()
    assert statuses["online"].online is True and statuses["online"].rx_bytes == 5
    assert statuses["online"].connections_by_address == 3 and statuses["online"].encrypted_dns_blocked == 2
    assert statuses["stale"].connections_by_address == 0
    assert statuses["stale"].online is True and statuses["stale"].endpoint == "5.6.7.8:9"
    assert statuses["never"].online is False and statuses["never"].last_handshake_at is None
    assert "junk" not in statuses
