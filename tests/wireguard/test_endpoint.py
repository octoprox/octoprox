# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A gateway's WireGuard endpoint following the feed: apply diffs, the reconcile sweep, status.

The interface is a scripted one (it carries exactly the peers wg was last
told, plus any stray a test adds) and the data plane a recording stand-in,
so these run anywhere; the data plane's own attach and re-address are
covered in tests/tunnel/test_dataplane.py.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from api.core import utc_now
from api.core.config import Settings
from api.core.job_stats import job_stats
from api.core.workers import WorkerName
from api.db.redis import RedisClient
from api.feed.models import WireGuardFeed, WireGuardPeerFeed, WireGuardServerFeed
from api.models.wireguard import WireGuardPeerReading
from api.tunnel.peers import PeerIndex
from api.tunnel.system import CommandError, CommandRunner
from api.wireguard.endpoint import TunnelReconciler, WireGuardEndpoint


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, instance_id="gw-1", **overrides)  # type: ignore[call-arg]


class InterfaceRunner(CommandRunner):
    """``ip`` and ``wg`` against an imaginary interface: records every call, fails those the test says.

    The interface exists between ``ip link add`` and ``ip link del`` and
    carries exactly the peers of the last configuration wg took, plus the
    strays a test adds behind the endpoint's back.
    """

    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.conf_files: list[str] = []
        self.failures = failures or {}
        self.up = False
        self.peers: list[str] = []
        self.strays: list[str] = []

    async def run(self, *argv: str, stdin: bytes | None = None, check: bool = True) -> str:
        self.calls.append(argv)
        joined = " ".join(argv)
        for prefix, stderr in self.failures.items():
            if joined.startswith(prefix) and check:
                raise CommandError(argv, 2, stderr)
        if argv[:3] == ("ip", "link", "show"):
            if not self.up:
                raise CommandError(argv, 1, 'Device "wg0" does not exist.')
        elif argv[:3] == ("ip", "link", "add"):
            self.up = True
        elif argv[:3] == ("ip", "link", "del"):
            self.up, self.peers = False, []
        elif argv[:2] in (("wg", "setconf"), ("wg", "syncconf")):
            conf = Path(argv[3]).read_text()
            self.conf_files.append(conf)
            self.peers = [line.split(" = ", 1)[1] for line in conf.splitlines() if line.startswith("PublicKey = ")]
        elif argv[:2] == ("wg", "show"):
            if not self.up:
                raise CommandError(argv, 1, "Unable to access interface: No such device")
            lines = ["server-private\tserver-public\t51820\toff"]
            lines += [f"{key}\t(none)\t1.2.3.4:5\t10.66.0.2/32\t1700000000\t10\t20\toff" for key in self.peers + self.strays]
            return "\n".join(lines) + "\n"
        return ""

    def wg_calls(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[0] == "wg" and call[1] in ("setconf", "syncconf")]


class FakePlane:
    """The tunnel data plane as an endpoint sees it: an index to register with, attach and detach."""

    def __init__(self, *, active_connections: int = 0) -> None:
        self.peers = PeerIndex()
        self.transparent_server = SimpleNamespace(active_connections=active_connections)
        self.attached: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []
        self.fail_attach: Exception | None = None

    async def attach(self, interface: str, gateway: str) -> None:
        self.calls.append(("attach", interface, gateway))
        if self.fail_attach is not None:
            raise self.fail_attach
        self.attached[interface] = gateway

    async def detach(self, interface: str) -> None:
        self.calls.append(("detach", interface))
        self.attached.pop(interface, None)


def _peer(name: str, key: str, address: str, *, enabled: bool = True) -> WireGuardPeerFeed:
    return WireGuardPeerFeed(
        id=f"id-{name}", project_id="p", name=name, address=address, enabled=enabled, public_key=key,
        preshared_key=f"psk-{name}", created_at=utc_now(),
    )


def _feed(*peers: WireGuardPeerFeed, subnet: str = "10.66.0.0/16", key: str = "server-key", port: int = 51820) -> WireGuardFeed:
    return WireGuardFeed(server=WireGuardServerFeed(private_key=key, endpoint_port=port, subnet=subnet), peers=list(peers))


def _endpoint(runner: InterfaceRunner, plane: FakePlane | None = None, redis: Any = None, **overrides: object) -> WireGuardEndpoint:
    return WireGuardEndpoint(
        _settings(wireguard_enabled=True, **overrides),
        plane or FakePlane(),  # type: ignore[arg-type]
        redis or MagicMock(),
        runner=runner,
    )


async def _apply(endpoint: WireGuardEndpoint, feed: WireGuardFeed | None) -> None:
    endpoint.apply(feed)
    await endpoint.settle()


TV = _peer("tv", "pub-tv", "10.66.0.2")
CONSOLE = _peer("console", "pub-console", "10.66.0.3")


class TestApply:
    async def test_first_feed_brings_the_interface_up_and_attaches_it(self) -> None:
        runner, plane = InterfaceRunner(), FakePlane(active_connections=2)
        endpoint = _endpoint(runner, plane, wireguard_listen_port=51999)
        assert endpoint.state().state == "starting" and runner.calls == []

        await _apply(endpoint, _feed(TV))
        assert endpoint.running, endpoint.state().error
        assert ("ip", "link", "add", "dev", "wg0", "type", "wireguard") in runner.calls
        assert ("ip", "address", "add", "10.66.0.1/16", "dev", "wg0") in runner.calls
        conf = runner.conf_files[0]
        assert "PrivateKey = server-key" in conf and "ListenPort = 51999" in conf
        assert "AllowedIPs = 10.66.0.2/32" in conf and "PresharedKey = psk-tv" in conf
        assert plane.attached == {"wg0": "10.66.0.1"}
        # The data plane finds the device by its tunnel address through the endpoint's directory.
        assert plane.peers.by_address("10.66.0.2") is not None
        state = endpoint.state()
        assert (state.protocol, state.instance_id, state.backend, state.listen_port) == ("wireguard", "gw-1", "kernel", 51999)
        assert state.active_connections == 2 and state.interface == "wg0" and state.error is None

    async def test_device_changes_sync_the_peers_and_nothing_else_does(self) -> None:
        runner = InterfaceRunner()
        endpoint = _endpoint(runner)
        await _apply(endpoint, _feed(TV))
        assert len(runner.wg_calls()) == 1

        # Disabling a device takes it off the interface; the session of the other stays.
        await _apply(endpoint, _feed(TV.model_copy(update={"enabled": False}), CONSOLE))
        assert runner.wg_calls()[-1][:2] == ("wg", "syncconf")
        assert "pub-tv" not in runner.conf_files[-1] and "pub-console" in runner.conf_files[-1]
        assert endpoint.directory.get(TV.id) is not None  # still known, and refused by the listener

        # A rename or a location change is nothing wg needs to hear about.
        calls = len(runner.calls)
        await _apply(endpoint, _feed(TV.model_copy(update={"enabled": False, "name": "lounge"}), CONSOLE.model_copy(update={"country": "DE"})))
        assert len(runner.calls) == calls
        assert endpoint.directory.get(CONSOLE.id) is not None and endpoint.directory.get(CONSOLE.id).country == "DE"  # type: ignore[union-attr]

        # The server key and the port reach the interface without recreating it.
        await _apply(endpoint, _feed(CONSOLE, key="rotated-key", port=51821))
        assert runner.wg_calls()[-1][:2] == ("wg", "syncconf")
        assert "PrivateKey = rotated-key" in runner.conf_files[-1] and "ListenPort = 51821" in runner.conf_files[-1]
        assert runner.calls.count(("ip", "link", "add", "dev", "wg0", "type", "wireguard")) == 1

    async def test_a_subnet_change_moves_the_interface_live(self) -> None:
        runner, plane = InterfaceRunner(), FakePlane()
        endpoint = _endpoint(runner, plane)
        await _apply(endpoint, _feed(TV))
        await _apply(endpoint, _feed(TV, subnet="10.66.0.0/15"))
        assert ("ip", "address", "flush", "dev", "wg0") in runner.calls
        assert ("ip", "address", "add", "10.66.0.1/15", "dev", "wg0") in runner.calls
        # Same gateway address here, so the listeners stay; a gateway that moves is re-attached.
        await _apply(endpoint, _feed(_peer("tv", "pub-tv", "10.77.0.2"), subnet="10.77.0.0/24"))
        assert ("ip", "address", "add", "10.77.0.1/24", "dev", "wg0") in runner.calls
        assert plane.attached == {"wg0": "10.77.0.1"}
        # The data plane finds the device at its new address only.
        assert plane.peers.by_address("10.66.0.2") is None and plane.peers.by_address("10.77.0.2") is not None
        assert plane.calls[-1] == ("attach", "wg0", "10.77.0.1")
        assert runner.calls.count(("ip", "link", "add", "dev", "wg0", "type", "wireguard")) == 1
        assert endpoint.running

    async def test_a_failed_subnet_change_takes_the_interface_down_for_the_sweep_to_bring_up(self) -> None:
        runner, plane = InterfaceRunner(), FakePlane()
        endpoint = _endpoint(runner, plane)
        await _apply(endpoint, _feed(TV))
        plane.fail_attach = OSError("Cannot assign requested address")
        await _apply(endpoint, _feed(TV, subnet="10.77.0.0/24"))
        state = endpoint.state()
        assert state.state == "failed" and "Cannot assign" in (state.error or "")
        assert plane.attached == {} and not runner.up

        plane.fail_attach = None
        assert await endpoint.reconcile() == 1
        assert endpoint.running and plane.attached == {"wg0": "10.77.0.1"}

    async def test_failure_to_bring_up_is_reported_and_retried(self) -> None:
        runner, plane = InterfaceRunner(failures={"ip link add": "RTNETLINK answers: Operation not permitted"}), FakePlane()
        endpoint = _endpoint(runner, plane)
        await _apply(endpoint, _feed(TV))
        state = endpoint.state()
        assert state.state == "failed" and state.error is not None and "CAP_NET_ADMIN" in state.error
        assert plane.attached == {}
        # The sweep tries again (nothing fixed while it fails); once the cause is gone the tunnel comes up.
        assert await endpoint.reconcile() == 0
        assert runner.calls.count(("ip", "link", "add", "dev", "wg0", "type", "wireguard")) == 2
        runner.failures.clear()
        assert await endpoint.reconcile() == 1
        assert endpoint.running and plane.attached == {"wg0": "10.66.0.1"} and endpoint.state().error is None

    async def test_a_failed_sync_keeps_the_interface_and_is_retried(self) -> None:
        runner = InterfaceRunner()
        endpoint = _endpoint(runner)
        await _apply(endpoint, _feed(TV))
        runner.failures["wg syncconf"] = "Unable to modify interface: Protocol not supported"
        await _apply(endpoint, _feed(TV, CONSOLE))
        assert endpoint.running and "Protocol not supported" in (endpoint.state().error or "")
        assert runner.peers == ["pub-tv"]
        runner.failures.clear()
        assert await endpoint.reconcile() == 1  # the sync retried, which put the device on
        assert runner.peers == ["pub-tv", "pub-console"] and endpoint.state().error is None
        assert await endpoint.reconcile() == 0

    async def test_no_settings_in_the_feed_takes_the_interface_down(self) -> None:
        runner, plane = InterfaceRunner(), FakePlane()
        endpoint = _endpoint(runner, plane)
        await _apply(endpoint, None)
        assert runner.calls == [] and endpoint.state().state == "starting"
        await _apply(endpoint, _feed(TV))
        await _apply(endpoint, None)
        assert plane.attached == {} and runner.calls[-1] == ("ip", "link", "del", "dev", "wg0")
        assert endpoint.state().state == "starting" and len(endpoint.directory) == 0

    async def test_stop_takes_everything_down(self) -> None:
        runner, plane = InterfaceRunner(), FakePlane()
        endpoint = _endpoint(runner, plane)
        await endpoint.start()
        await _apply(endpoint, _feed(TV))
        await endpoint.stop()
        assert endpoint.state().state == "stopped" and endpoint.tasks.tasks == []
        assert plane.attached == {} and runner.calls[-1] == ("ip", "link", "del", "dev", "wg0")
        # Nothing is applied after stop.
        await _apply(endpoint, _feed(TV, CONSOLE))
        assert not runner.up


class TestReconcile:
    async def test_strays_are_removed_and_missing_devices_added(self) -> None:
        runner = InterfaceRunner()
        endpoint = _endpoint(runner)
        await _apply(endpoint, _feed(TV, CONSOLE))
        assert await endpoint.reconcile() == 0
        calls = len(runner.calls)

        # A peer the feed does not have (a removal whose sync failed), and one it lost.
        runner.strays = ["pub-revoked"]
        runner.peers = ["pub-tv"]
        assert await endpoint.reconcile() == 2
        assert runner.wg_calls()[-1][:2] == ("wg", "syncconf")
        assert len(runner.calls) > calls
        runner.strays = []
        assert runner.peers == ["pub-tv", "pub-console"]
        assert await endpoint.reconcile() == 0

    async def test_a_vanished_interface_is_brought_up_again(self) -> None:
        runner, plane = InterfaceRunner(), FakePlane()
        endpoint = _endpoint(runner, plane)
        await _apply(endpoint, _feed(TV))
        runner.up = False  # wireguard-go exited and took its interface along
        assert await endpoint.reconcile() == 1
        assert runner.up and runner.peers == ["pub-tv"] and endpoint.running
        assert runner.calls.count(("ip", "link", "add", "dev", "wg0", "type", "wireguard")) == 2

    async def test_nothing_to_do_before_the_first_feed(self) -> None:
        runner = InterfaceRunner()
        endpoint = _endpoint(runner)
        assert await endpoint.reconcile() == 0
        assert runner.calls == []


class TestStatus:
    async def test_published_readings_are_what_the_api_and_the_controller_read(self, redis_client: RedisClient) -> None:
        runner = InterfaceRunner()
        endpoint = _endpoint(runner, redis=redis_client)
        assert await endpoint.publish_status() is False  # nothing up yet
        await _apply(endpoint, _feed(TV))
        assert await endpoint.publish_status() is True

        published = await redis_client.get_tunnel_peer_status("wireguard")
        readings = WireGuardPeerReading.latest(published)
        reading = readings["pub-tv"]
        assert (reading.endpoint, reading.rx_bytes, reading.tx_bytes) == ("1.2.3.4:5", 10, 20)
        assert reading.handshake_at is not None and reading.handshake_at.year == 2023
        # The gateway writes nothing to Postgres: the last sighting is the controller's to record.
        assert set(published[0]) == {"pub-tv"}

    async def test_the_publisher_loop_publishes_until_stopped(self, redis_client: RedisClient) -> None:
        runner = InterfaceRunner()
        endpoint = WireGuardEndpoint(
            _settings(wireguard_enabled=True), FakePlane(), redis_client, runner=runner, status_interval=0.01  # type: ignore[arg-type]
        )
        await _apply(endpoint, _feed(TV))
        await endpoint.start()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if await redis_client.get_tunnel_peer_status("wireguard"):
                break
        await endpoint.stop()
        assert await redis_client.get_tunnel_peer_status("wireguard")
        assert [task.get_name() for task in endpoint.tasks.tasks] == []


class _Endpoint:
    def __init__(self, fixed: int = 0, error: Exception | None = None) -> None:
        self.fixed, self.error, self.sweeps = fixed, error, 0

    async def reconcile(self) -> int:
        self.sweeps += 1
        if self.error is not None:
            raise self.error
        return self.fixed


class TestTunnelReconciler:
    async def test_a_sweep_covers_every_endpoint_even_when_one_fails(self) -> None:
        broken, fine = _Endpoint(error=RuntimeError("management socket gone")), _Endpoint(fixed=2)
        reconciler = TunnelReconciler([broken, fine])
        assert await reconciler.reconcile() == (2, 1)
        assert broken.sweeps == fine.sweeps == 1

    async def test_the_loop_counts_idle_and_failed_sweeps(self) -> None:
        job_stats.reset()
        endpoint = _Endpoint()
        reconciler = TunnelReconciler([endpoint], interval=0.01)
        task = asyncio.create_task(reconciler.run())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if endpoint.sweeps >= 2:
                break
        endpoint.error = RuntimeError("boom")
        sweeps = endpoint.sweeps
        for _ in range(100):
            await asyncio.sleep(0.01)
            if endpoint.sweeps > sweeps:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        stats = job_stats.get(WorkerName.TUNNEL_RECONCILER)
        assert stats is not None and stats.interval_seconds == 0.01
        assert stats.runs >= 3 and stats.idle_runs >= 2 and stats.failures >= 1
