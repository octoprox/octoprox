# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The runtime's lifecycle, device admission and supervision, with a scripted daemon and management link."""

import asyncio
import json
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core.config import Settings
from api.models.openvpn import OpenVpnServerSettings
from api.openvpn import pki
from api.openvpn import runtime as runtime_module
from api.openvpn.daemon import OpenVpnDaemon
from api.openvpn.management import ClientEvent, ManagementClient, SessionStatus
from api.openvpn.runtime import OpenVpnRuntime, defaults_from_config
from api.tunnel.dataplane import TunnelDataPlane
from tests.tunnel.test_system import RecordingRunner


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, tunnel_transparent_port=0, tunnel_dns_port=0, **overrides)  # type: ignore[call-arg]


class FakeDaemon(OpenVpnDaemon):
    """Runs until told to exit; records the config it was started with."""

    configs: list[str] = []
    instances: list["FakeDaemon"] = []

    def __init__(self) -> None:
        self._exited = asyncio.Event()
        self._code: int | None = None
        self.alive = False
        self.fail_start = False
        FakeDaemon.instances.append(self)

    @property
    def running(self) -> bool:
        return self.alive

    @property
    def returncode(self) -> int | None:
        return self._code

    def recent_output(self) -> str:
        return "Options error: something" if self._code else ""

    async def start(self, config_path: str) -> None:
        if self.fail_start:
            raise RuntimeError("openvpn is not installed")
        FakeDaemon.configs.append(Path(config_path).read_text())
        self.alive = True

    async def wait(self) -> int:
        await self._exited.wait()
        return self._code or 0

    async def stop(self) -> None:
        self.exit(0)

    def exit(self, code: int) -> None:
        self.alive = False
        self._code = code
        self._exited.set()


class FakeManagement(ManagementClient):
    """Answers like a healthy daemon and records what it was told."""

    def __init__(self, path: str, on_client: Callable[[ClientEvent], Coroutine[Any, Any, None]]) -> None:
        super().__init__(path, on_client=on_client)
        self.on_client = on_client
        self.approved: list[tuple[int, int, list[str]]] = []
        self.reauthed: list[tuple[int, int]] = []
        self.denied: list[tuple[int, int, str]] = []
        self.killed: list[str] = []
        self.sessions: list[SessionStatus] = []
        self.open = False

    @property
    def connected(self) -> bool:
        return self.open

    async def connect(self, *, timeout: float = 15.0, still_alive: Callable[[], bool] | None = None) -> None:
        self.open = True

    async def close(self) -> None:
        self.open = False

    async def wait_until_ready(self, *, timeout: float = 30.0, still_alive: Callable[[], bool] | None = None) -> None:
        pass

    async def version(self) -> str | None:
        return "OpenVPN 2.6.3"

    async def status(self) -> list[SessionStatus]:
        return list(self.sessions)

    async def approve(self, cid: int, kid: int, directives: list[str]) -> None:
        self.approved.append((cid, kid, directives))

    async def approve_without_push(self, cid: int, kid: int) -> None:
        self.reauthed.append((cid, kid))

    async def deny(self, cid: int, kid: int, reason: str) -> None:
        self.denied.append((cid, kid, reason))

    async def kill(self, common_name: str) -> bool:
        self.killed.append(common_name)
        return True


async def _runtime(settings: Settings, redis: object = None) -> OpenVpnRuntime:
    tunnel = TunnelDataPlane(settings, redis, runner=RecordingRunner())  # type: ignore[arg-type]
    await tunnel.start(MagicMock())
    return OpenVpnRuntime(
        settings, None, redis, tunnel,  # type: ignore[arg-type]
        runner=RecordingRunner(), daemon_factory=FakeDaemon, management_factory=FakeManagement,
    )


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeDaemon.configs = []
    FakeDaemon.instances = []
    # The listeners bind the gateway address; point it at loopback for the test.
    monkeypatch.setattr(OpenVpnServerSettings, "gateway", property(lambda self: "127.0.0.1"))
    monkeypatch.setattr(runtime_module, "RESTART_BACKOFF_SECONDS", 0.05)


def test_defaults_seed_from_config() -> None:
    seeded = defaults_from_config(_settings(openvpn_defaults={"endpoint_host": "vpn.example.net", "protocol": "tcp", "subnet": "10.77.0.0/24"}))
    assert seeded.endpoint_host == "vpn.example.net" and seeded.protocol == "tcp"
    assert pki.is_issued_by(seeded.server_cert, seeded.ca_cert) and pki.is_tls_crypt_key(seeded.tls_crypt_key)
    bad = defaults_from_config(_settings(openvpn_defaults={"subnet": "nonsense"}))
    assert bad.subnet == "10.67.0.0/16"


@pytest.mark.asyncio
async def test_disabled_runtime_only_loads_and_issues() -> None:
    runtime = await _runtime(_settings())
    await runtime.start()
    assert (await runtime.status()).state == "disabled"
    peer = runtime.issue_peer(project_id="p", name="tv", country="DE")
    assert peer.address == "10.67.0.2" and pki.common_name_of(peer.certificate) == peer.id
    assert pki.is_issued_by(peer.certificate, runtime.settings_store.settings.ca_cert)
    await runtime.add_peer(peer)
    assert runtime.tunnel.peers.by_address("10.67.0.2") is peer
    reissued = runtime.reissue_certificate(peer)
    assert reissued.serial != peer.serial and reissued.address == peer.address
    await runtime.update_peer(reissued)
    assert await runtime.remove_peer(peer.id) is True
    await runtime.stop()
    await runtime.tunnel.stop()


@pytest.mark.asyncio
async def test_enabled_runtime_runs_the_daemon_and_admits_devices() -> None:
    runtime = await _runtime(_settings(openvpn_enabled=True, openvpn_listen_port=1195))
    peer = runtime.issue_peer(project_id="p", name="tv")
    runtime.peers.put(peer)
    disabled = runtime.issue_peer(project_id="p", name="off", enabled=False)
    runtime.peers.put(disabled)

    await runtime.start()
    try:
        assert runtime.state == "running", runtime.error
        assert runtime.tunnel.interfaces == {"ovpn0": "127.0.0.1"}
        conf = FakeDaemon.configs[0]
        assert "port 1195\n" in conf and "management-client-auth\n" in conf and "dev ovpn0\n" in conf
        assert runtime.daemon_version == "OpenVPN 2.6.3"
        management = runtime.management
        assert isinstance(management, FakeManagement)

        # The directory decides who connects: known and enabled with the certificate on record.
        await management.on_client(ClientEvent(kind="CONNECT", cid=1, kid=0, env={"common_name": peer.id, "tls_serial_0": peer.serial}))
        assert management.approved == [(1, 0, [f"ifconfig-push {peer.address} 255.255.0.0"])]
        await management.on_client(ClientEvent(kind="REAUTH", cid=1, kid=1, env={"common_name": peer.id, "tls_serial_0": peer.serial}))
        assert management.reauthed == [(1, 1)]
        await management.on_client(ClientEvent(kind="CONNECT", cid=2, kid=0, env={"common_name": "nobody"}))
        await management.on_client(ClientEvent(kind="CONNECT", cid=3, kid=0, env={"common_name": disabled.id, "tls_serial_0": disabled.serial}))
        await management.on_client(ClientEvent(kind="CONNECT", cid=4, kid=0, env={"common_name": peer.id, "tls_serial_0": "999"}))
        assert [d[2] for d in management.denied] == ["unknown device", "device disabled", "certificate replaced"]
        assert runtime.denied == 3

        # Disabling or rotating a device ends its session here; enabling does not.
        await runtime.update_peer(peer.model_copy(update={"enabled": False}))
        await runtime.update_peer(peer.model_copy(update={"enabled": True}))
        await runtime.update_peer(runtime.reissue_certificate(peer))
        await runtime.remove_peer(disabled.id)
        assert management.killed == [peer.id, peer.id, disabled.id]

        status = await runtime.status()
        assert status.protocol == "udp" and status.listen_port == 1195 and status.peers_total == 1
        assert status.daemon_version == "OpenVPN 2.6.3" and status.restarts == 0
    finally:
        await runtime.stop()
    assert runtime.state == "stopped" and runtime.tunnel.interfaces == {}
    assert FakeDaemon.instances[0].alive is False
    await runtime.tunnel.stop()


@pytest.mark.asyncio
async def test_daemon_exit_is_reported_and_the_daemon_restarted() -> None:
    runtime = await _runtime(_settings(openvpn_enabled=True))
    await runtime.start()
    try:
        assert runtime.state == "running"
        first = FakeDaemon.instances[-1]
        first.exit(1)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if runtime.state == "running" and FakeDaemon.instances[-1] is not first:
                break
        assert runtime.state == "running", runtime.error
        assert runtime.restarts == 1 and len(FakeDaemon.instances) == 2
        assert runtime.tunnel.interfaces == {"ovpn0": "127.0.0.1"}
    finally:
        await runtime.stop()
    await runtime.tunnel.stop()


@pytest.mark.asyncio
async def test_failure_to_start_is_reported_not_fatal() -> None:
    class BrokenDaemon(FakeDaemon):
        def __init__(self) -> None:
            super().__init__()
            self.fail_start = True

    tunnel = TunnelDataPlane(_settings(), None, runner=RecordingRunner())
    await tunnel.start(MagicMock())
    runtime = OpenVpnRuntime(
        _settings(openvpn_enabled=True), None, None, tunnel,
        runner=RecordingRunner(), daemon_factory=BrokenDaemon, management_factory=FakeManagement,
    )
    await runtime.start()
    assert runtime.state == "failed" and runtime.error is not None and "not installed" in runtime.error
    assert tunnel.interfaces == {}
    await runtime.stop()
    await tunnel.stop()


@pytest.mark.asyncio
async def test_settings_and_identity_changes_restart_the_daemon() -> None:
    runtime = await _runtime(_settings(openvpn_enabled=True))
    peer = runtime.issue_peer(project_id="p", name="tv")
    runtime.peers.put(peer)
    await runtime.start()
    try:
        server = runtime.settings_store.settings
        await runtime.save_settings(server.model_copy(update={"endpoint_host": "vpn.example.net"}), "admin")
        assert len(FakeDaemon.instances) == 1  # the endpoint host is not the daemon's business
        await runtime.save_settings(server.model_copy(update={"protocol": "tcp", "endpoint_port": 443}), "admin")
        assert len(FakeDaemon.instances) == 2 and "proto tcp-server\n" in FakeDaemon.configs[-1]
        assert runtime.state == "running"

        old_ca = server.ca_cert
        rotated = await runtime.rotate_identity("admin")
        assert rotated.ca_cert != old_ca and len(FakeDaemon.instances) == 3
        reissued = runtime.peers.get(peer.id)
        assert reissued is not None and reissued.serial != peer.serial
        assert pki.is_issued_by(reissued.certificate, rotated.ca_cert)
        assert f"<ca>\n{rotated.ca_cert.strip()}" in FakeDaemon.configs[-1]
    finally:
        await runtime.stop()
    await runtime.tunnel.stop()


@pytest.mark.asyncio
async def test_status_publishing_and_merging() -> None:
    redis = MagicMock()
    redis.set_tunnel_peer_status = AsyncMock()
    now = int(time.time())
    runtime = await _runtime(_settings(openvpn_enabled=True), redis=redis)
    peer = runtime.issue_peer(project_id="p", name="tv")
    runtime.peers.put(peer)
    await runtime.start()
    try:
        management = runtime.management
        assert isinstance(management, FakeManagement)
        management.sessions = [
            SessionStatus(common_name=peer.id, real_address="203.0.113.9:1", virtual_address=peer.address, rx_bytes=5, tx_bytes=7, connected_since=now - 30, cid=1),
            SessionStatus(common_name="stranger", real_address="x", virtual_address="", rx_bytes=0, tx_bytes=0, connected_since=now, cid=2),
        ]
        assert await runtime._publish_status() is True
        protocol, _instance, statuses = redis.set_tunnel_peer_status.await_args.args
        assert protocol == "openvpn" and list(statuses) == [peer.id]
        assert peer.last_connected_at is not None and peer.last_endpoint == "203.0.113.9:1"

        # Two carriers: the newer session is where the device is now.
        redis.get_tunnel_peer_status = AsyncMock(return_value=[
            {peer.id: json.dumps({"connected_since": now - 300, "rx_bytes": 1, "tx_bytes": 1, "endpoint": "old:1"}), "junk": "{"},
            {peer.id: json.dumps({"connected_since": now - 30, "rx_bytes": 5, "tx_bytes": 7, "endpoint": "203.0.113.9:1"})},
        ])
        live = await runtime.peer_statuses()
        assert live[peer.id].online and live[peer.id].endpoint == "203.0.113.9:1" and live[peer.id].rx_bytes == 5
        assert "junk" not in live
        status = runtime.peer_status(peer, live.get(peer.id))
        assert status.live is True and status.connected_since is not None
        offline = runtime.peer_status(peer, None)
        assert offline.online is False and offline.live is False and offline.last_seen_at == peer.last_connected_at
        assert (await runtime.status()).peers_online == 1
    finally:
        await runtime.stop()
    await runtime.tunnel.stop()
