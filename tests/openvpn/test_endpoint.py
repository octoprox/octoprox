# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A gateway's OpenVPN endpoint following the feed: daemon, admission, revocation, supervision, status.

The daemon and its management link are scripted, and the data plane is the
recording stand-in of the WireGuard endpoint tests.
"""

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from api.core import utc_now
from api.core.config import Settings
from api.db.redis import RedisClient
from api.feed.models import OpenVpnFeed, OpenVpnPeerFeed, OpenVpnServerFeed
from api.models.openvpn import OpenVpnSessionReading
from api.openvpn import endpoint as endpoint_module
from api.openvpn.daemon import OpenVpnDaemon
from api.openvpn.endpoint import OpenVpnEndpoint
from api.openvpn.management import ClientEvent, ManagementClient, ManagementError, SessionStatus
from tests.tunnel.test_system import RecordingRunner
from tests.wireguard.test_endpoint import FakePlane


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, instance_id="gw-1", openvpn_enabled=True, **overrides)  # type: ignore[call-arg]


class FakeDaemon(OpenVpnDaemon):
    """Runs until told to exit; records the config it was started with."""

    configs: list[str] = []
    instances: list["FakeDaemon"] = []
    fail_start = False

    def __init__(self) -> None:
        self._exited = asyncio.Event()
        self._code: int | None = None
        self.alive = False
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
        if FakeDaemon.fail_start:
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

    instances: list["FakeManagement"] = []

    def __init__(self, path: str, on_client: Callable[[ClientEvent], Coroutine[Any, Any, None]]) -> None:
        super().__init__(path, on_client=on_client)
        self.on_client = on_client
        self.approved: list[tuple[int, int, list[str]]] = []
        self.reauthed: list[tuple[int, int]] = []
        self.denied: list[tuple[int, int, str]] = []
        self.killed: list[str] = []
        self.sessions: list[SessionStatus] = []
        self.fail_kills = False
        self.open = False
        FakeManagement.instances.append(self)

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
        if self.fail_kills:
            raise ManagementError("no reply to 'kill' in 10s")
        self.killed.append(common_name)
        self.sessions = [s for s in self.sessions if s.common_name != common_name]
        return True


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeDaemon.configs = []
    FakeDaemon.instances = []
    FakeDaemon.fail_start = False
    FakeManagement.instances = []
    monkeypatch.setattr(endpoint_module, "RESTART_BACKOFF_SECONDS", 0.05)


def _server(**overrides: Any) -> OpenVpnServerFeed:
    fields: dict[str, Any] = {
        "ca_cert": "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n",
        "server_cert": "-----BEGIN CERTIFICATE-----\nSERVER\n-----END CERTIFICATE-----\n",
        "server_key": "-----BEGIN PRIVATE KEY-----\nSERVERKEY\n-----END PRIVATE KEY-----\n",
        "tls_crypt_key": "-----BEGIN OpenVPN Static key V1-----\nabc\n-----END OpenVPN Static key V1-----\n",
        "endpoint_port": 1194,
        "protocol": "udp",
        "subnet": "10.67.0.0/16",
        "keepalive_interval": 10,
        "keepalive_timeout": 60,
    }
    fields.update(overrides)
    return OpenVpnServerFeed(**fields)


def _peer(name: str, address: str, serial: str, *, project_id: str = "p", enabled: bool = True) -> OpenVpnPeerFeed:
    return OpenVpnPeerFeed(
        id=f"id-{name}", project_id=project_id, name=name, address=address, enabled=enabled, serial=serial,
        created_at=utc_now(),
    )


def _feed(*peers: OpenVpnPeerFeed, server: OpenVpnServerFeed | None = None) -> OpenVpnFeed:
    return OpenVpnFeed(server=server or _server(), peers=list(peers))


def _endpoint(plane: FakePlane | None = None, redis: Any = None, **overrides: object) -> OpenVpnEndpoint:
    return OpenVpnEndpoint(
        _settings(**overrides),
        plane or FakePlane(),  # type: ignore[arg-type]
        redis or MagicMock(),
        runner=RecordingRunner(),
        daemon_factory=FakeDaemon,
        management_factory=FakeManagement,
    )


async def _apply(endpoint: OpenVpnEndpoint, feed: OpenVpnFeed | None) -> None:
    endpoint.apply(feed)
    await endpoint.settle()


def _management(endpoint: OpenVpnEndpoint) -> FakeManagement:
    management = endpoint.management
    assert isinstance(management, FakeManagement)
    return management


def _session(peer_id: str, cid: int, *, since: int = 1700000000) -> SessionStatus:
    return SessionStatus(
        common_name=peer_id, real_address="203.0.113.9:1", virtual_address="", rx_bytes=5, tx_bytes=7,
        connected_since=since, cid=cid,
    )


TV = _peer("tv", "10.67.0.2", "1001")
CONSOLE = _peer("console", "10.67.0.3", "1002")


class TestApply:
    async def test_first_feed_starts_the_daemon_and_admits_devices(self) -> None:
        plane = FakePlane(active_connections=4)
        endpoint = _endpoint(plane, openvpn_listen_port=1195)
        off = _peer("off", "10.67.0.4", "1003", enabled=False)
        await _apply(endpoint, _feed(TV, off))
        try:
            assert endpoint.running, endpoint.state().error
            assert plane.attached == {"ovpn0": "10.67.0.1"}
            conf = FakeDaemon.configs[0]
            assert "port 1195\n" in conf and "management-client-auth\n" in conf and "dev ovpn0\n" in conf
            assert "<ca>\n-----BEGIN CERTIFICATE-----\nCA" in conf and "ca_key" not in conf
            assert plane.peers.by_address("10.67.0.2") is not None

            # The directory decides who connects: known and enabled with the certificate on record.
            management = _management(endpoint)
            await management.on_client(ClientEvent(kind="CONNECT", cid=1, kid=0, env={"common_name": TV.id, "tls_serial_0": "1001"}))
            assert management.approved == [(1, 0, ["ifconfig-push 10.67.0.2 255.255.0.0"])]
            await management.on_client(ClientEvent(kind="REAUTH", cid=1, kid=1, env={"common_name": TV.id, "tls_serial_0": "1001"}))
            assert management.reauthed == [(1, 1)]
            await management.on_client(ClientEvent(kind="CONNECT", cid=2, kid=0, env={"common_name": "nobody"}))
            await management.on_client(ClientEvent(kind="CONNECT", cid=3, kid=0, env={"common_name": off.id, "tls_serial_0": "1003"}))
            await management.on_client(ClientEvent(kind="CONNECT", cid=4, kid=0, env={"common_name": TV.id, "tls_serial_0": "999"}))
            assert [d[2] for d in management.denied] == ["unknown device", "device disabled", "certificate replaced"]

            state = endpoint.state()
            assert (state.protocol, state.instance_id, state.state, state.listen_port) == ("openvpn", "gw-1", "running", 1195)
            assert state.denied == 3 and state.restarts == 0 and state.daemon_version == "OpenVPN 2.6.3"
            assert state.active_connections == 4 and state.interface == "ovpn0" and state.backend is None
        finally:
            await endpoint.stop()
        assert endpoint.state().state == "stopped" and plane.attached == {}
        assert FakeDaemon.instances[0].alive is False

    async def test_revocations_end_sessions_and_nothing_else_does(self) -> None:
        endpoint = _endpoint()
        await _apply(endpoint, _feed(TV, CONSOLE))
        management = _management(endpoint)
        try:
            # Disabling ends the session; enabling again does not; a new certificate does.
            await _apply(endpoint, _feed(TV.model_copy(update={"enabled": False}), CONSOLE))
            await _apply(endpoint, _feed(TV, CONSOLE))
            await _apply(endpoint, _feed(TV.model_copy(update={"serial": "2001"}), CONSOLE))
            assert management.killed == [TV.id, TV.id]
            # A rename or a location change ends nothing.
            await _apply(endpoint, _feed(TV.model_copy(update={"serial": "2001", "name": "lounge"}), CONSOLE.model_copy(update={"country": "DE"})))
            assert management.killed == [TV.id, TV.id]
            # A device removed, and every device of a project deleted since (the rows went with it).
            await _apply(endpoint, _feed(CONSOLE))
            other = _peer("phone", "10.67.0.5", "3001", project_id="q")
            await _apply(endpoint, _feed(CONSOLE, other))
            await _apply(endpoint, _feed())
            assert management.killed == [TV.id, TV.id, TV.id, CONSOLE.id, other.id]
            assert len(FakeDaemon.instances) == 1
        finally:
            await endpoint.stop()

    async def test_daemon_changes_restart_it(self) -> None:
        plane = FakePlane()
        endpoint = _endpoint(plane)
        await _apply(endpoint, _feed(TV))
        try:
            await _apply(endpoint, _feed(TV, CONSOLE))
            assert len(FakeDaemon.instances) == 1
            await _apply(endpoint, _feed(TV, server=_server(protocol="tcp", endpoint_port=443)))
            assert len(FakeDaemon.instances) == 2 and "proto tcp-server\nport 443\n" in FakeDaemon.configs[-1]
            assert FakeDaemon.instances[0].alive is False and endpoint.running
            # A rotated identity: the daemon runs with the new CA, and the restart ended every session.
            await _apply(endpoint, _feed(TV.model_copy(update={"serial": "2001"}), server=_server(protocol="tcp", endpoint_port=443, ca_cert="NEWCA")))
            assert len(FakeDaemon.instances) == 3 and "<ca>\nNEWCA\n</ca>" in FakeDaemon.configs[-1]
            assert _management(endpoint).killed == []
            # A subnet change re-attaches the interface on the new gateway.
            await _apply(endpoint, _feed(server=_server(subnet="10.68.0.0/24")))
            assert "server 10.68.0.0 255.255.255.0\n" in FakeDaemon.configs[-1]
            assert plane.attached == {"ovpn0": "10.68.0.1"}
        finally:
            await endpoint.stop()

    async def test_daemon_exit_is_reported_and_the_daemon_restarted(self) -> None:
        plane = FakePlane()
        endpoint = _endpoint(plane)
        await endpoint.start()
        await _apply(endpoint, _feed(TV))
        try:
            first = FakeDaemon.instances[-1]
            first.exit(1)
            for _ in range(100):
                await asyncio.sleep(0.02)
                if endpoint.running and FakeDaemon.instances[-1] is not first:
                    break
            assert endpoint.running, endpoint.state().error
            assert endpoint.state().restarts == 1 and len(FakeDaemon.instances) == 2
            assert plane.attached == {"ovpn0": "10.67.0.1"}
        finally:
            await endpoint.stop()

    async def test_a_daemon_started_while_the_supervisor_idles_is_watched_at_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The supervisor has no timer to look again: the start wakes it, and it waits on the daemon's exit."""
        monkeypatch.setattr(endpoint_module, "RESTART_BACKOFF_SECONDS", 3600.0)
        endpoint = _endpoint()
        await endpoint.start()
        # Idle before the first feed, with nothing to supervise.
        await asyncio.sleep(0.05)
        await _apply(endpoint, _feed(TV))
        try:
            assert endpoint.running
            FakeDaemon.instances[-1].exit(1)
            for _ in range(50):
                await asyncio.sleep(0.01)
                if endpoint.state().state == "failed":
                    break
            # Seen at once; the restart itself waits out the (here hour-long) backoff.
            state = endpoint.state()
            assert (state.state, state.restarts) == ("failed", 1)
            assert state.error is not None and "openvpn exited with status 1" in state.error
        finally:
            await endpoint.stop()

    async def test_a_start_that_failed_on_an_apply_is_retried_by_the_supervisor(self) -> None:
        """A failed start wakes the supervisor too: the daemon comes up with no new feed."""
        FakeDaemon.fail_start = True
        endpoint = _endpoint()
        await endpoint.start()
        await _apply(endpoint, _feed(TV))
        try:
            assert endpoint.state().state == "failed"
            FakeDaemon.fail_start = False
            for _ in range(100):
                await asyncio.sleep(0.02)
                if endpoint.running:
                    break
            assert endpoint.running, endpoint.state().error
            assert len(FakeDaemon.instances) == 2
        finally:
            await endpoint.stop()

    async def test_failure_to_start_is_reported_not_fatal(self) -> None:
        FakeDaemon.fail_start = True
        plane = FakePlane()
        endpoint = _endpoint(plane)
        await _apply(endpoint, _feed(TV))
        state = endpoint.state()
        assert state.state == "failed" and state.error is not None and "not installed" in state.error
        assert plane.attached == {}
        # A feed that changes nothing the daemon runs with leaves the retries to the supervisor.
        await _apply(endpoint, _feed(TV, CONSOLE))
        assert len(FakeDaemon.instances) == 1
        await endpoint.stop()

    async def test_no_settings_in_the_feed_stops_the_daemon(self) -> None:
        plane = FakePlane()
        endpoint = _endpoint(plane)
        await _apply(endpoint, None)
        assert FakeDaemon.instances == [] and endpoint.state().state == "starting"
        await _apply(endpoint, _feed(TV))
        await _apply(endpoint, None)
        assert FakeDaemon.instances[0].alive is False and plane.attached == {}
        assert endpoint.state().state == "starting" and len(endpoint.directory) == 0
        await endpoint.stop()


class TestReconcile:
    async def test_sessions_the_directory_no_longer_allows_are_ended(self) -> None:
        endpoint = _endpoint()
        await _apply(endpoint, _feed(TV, CONSOLE))
        management = _management(endpoint)
        try:
            await management.on_client(ClientEvent(kind="CONNECT", cid=1, kid=0, env={"common_name": TV.id, "tls_serial_0": "1001"}))
            await management.on_client(ClientEvent(kind="CONNECT", cid=2, kid=0, env={"common_name": CONSOLE.id, "tls_serial_0": "1002"}))
            management.sessions = [_session(TV.id, 1), _session(CONSOLE.id, 2), _session("stranger", 3)]
            assert await endpoint.reconcile() == 1
            assert management.killed == ["stranger"]

            # The kills of a revocation failed: TV's certificate was replaced and the console disabled.
            management.fail_kills = True
            await _apply(endpoint, _feed(TV.model_copy(update={"serial": "2001"}), CONSOLE.model_copy(update={"enabled": False})))
            assert management.killed == ["stranger"]
            management.fail_kills = False
            assert await endpoint.reconcile() == 2
            assert management.killed == ["stranger", CONSOLE.id, TV.id]
            assert await endpoint.reconcile() == 0
        finally:
            await endpoint.stop()

    async def test_nothing_to_do_without_a_daemon(self) -> None:
        endpoint = _endpoint()
        assert await endpoint.reconcile() == 0


class TestStatus:
    async def test_sessions_are_published_for_the_api_and_the_controller(self, redis_client: RedisClient) -> None:
        endpoint = _endpoint(redis=redis_client)
        assert await endpoint.publish_status() is False  # no daemon yet
        await _apply(endpoint, _feed(TV))
        management = _management(endpoint)
        try:
            management.sessions = [_session(TV.id, 1, since=1700000030), _session("stranger", 2)]
            assert await endpoint.publish_status() is True
            published = await redis_client.get_tunnel_peer_status("openvpn")
            assert [set(carrier) for carrier in published] == [{TV.id}]
            reading = OpenVpnSessionReading.latest(published)[TV.id]
            assert (reading.endpoint, reading.rx_bytes, reading.tx_bytes) == ("203.0.113.9:1", 5, 7)
            assert reading.connected_at is not None and reading.connected_at.year == 2023
        finally:
            await endpoint.stop()
