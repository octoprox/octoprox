# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The OpenVPN endpoint one gateway runs, following the routing feed: the daemon, the admission of each device, and its sessions.

It never touches Postgres: the feed's global section carries what the daemon runs with and every device.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

import structlog

from api.core.job_stats import job_stats
from api.core.tasks import TaskSet, spawn_held
from api.core.workers import WorkerName
from api.db.redis import TUNNEL_STATUS_INTERVAL
from api.models.openvpn import OpenVpnSessionReading
from api.models.tunnel import TunnelEndpointPhase, TunnelEndpointState, TunnelProtocol
from api.openvpn.config import render_server_conf
from api.openvpn.daemon import OpenVpnDaemon
from api.openvpn.management import ClientEvent, ManagementClient, ManagementError, SessionStatus
from api.tunnel.peers import AddressDirectory
from api.tunnel.system import CommandError, CommandRunner, explain

if TYPE_CHECKING:
    from api.core.config import Settings
    from api.db.redis import RedisClient
    from api.feed.models import OpenVpnFeed, OpenVpnPeerFeed, OpenVpnServerFeed
    from api.tunnel.dataplane import TunnelDataPlane

logger = structlog.get_logger()

# The protocol's name in the Redis status keys and in endpoint states.
PROTOCOL: TunnelProtocol = "openvpn"
# The supervisor's wait before starting a failed daemon again, in seconds,
# doubled after each failure up to the maximum: a broken configuration is
# retried without flooding the log.
RESTART_BACKOFF_SECONDS = 5.0
RESTART_BACKOFF_MAX_SECONDS = 60.0
# How long the daemon gets to create its management socket and bring its
# interface up, in seconds, before the start counts as failed.
STARTUP_TIMEOUT_SECONDS = 30.0

# Builds the daemon of one start; tests pass a scripted one.
DaemonFactory = Callable[[], OpenVpnDaemon]
# Connects to a daemon's management socket with the admission handler: (socket path, handler).
ManagementFactory = Callable[[str, Callable[[ClientEvent], Coroutine[Any, Any, None]]], ManagementClient]


def _must_disconnect(before: OpenVpnPeerFeed | None, after: OpenVpnPeerFeed | None) -> bool:
    """Whether a change to a device means a session it may have must end now."""
    if after is None:
        return before is not None
    if not after.enabled:
        return before is None or before.enabled
    return before is not None and before.serial != after.serial


class OpenVpnEndpoint:
    """The OpenVPN daemon one gateway runs, kept in line with the feed.

    Args:
        plane: the process's tunnel data plane, started before the first feed is applied; the daemon's interface attaches to it.
        status_interval: seconds between two publications of the sessions.
    """

    def __init__(
        self,
        settings: Settings,
        plane: TunnelDataPlane,
        redis_client: RedisClient,
        *,
        runner: CommandRunner | None = None,
        daemon_factory: DaemonFactory | None = None,
        management_factory: ManagementFactory | None = None,
        status_interval: float = TUNNEL_STATUS_INTERVAL,
    ) -> None:
        self._instance_id = settings.instance_id
        self.interface = settings.openvpn_interface
        self._listen_port = settings.openvpn_listen_port
        self._mtu = settings.openvpn_mtu
        self._fake_ip_range = settings.tunnel_fake_ip_range
        self._block_encrypted_dns = settings.tunnel_block_encrypted_dns
        self._plane = plane
        self._redis = redis_client
        self._status_interval = status_interval
        # The devices of the feed applied last: admission decides from it,
        # and the data plane tells a connection's device by its source
        # address through it (registered with its index).
        self.directory: AddressDirectory[OpenVpnPeerFeed] = AddressDirectory()
        plane.peers.add(self.directory)
        command_runner = runner or CommandRunner()
        self._daemon_factory: DaemonFactory = daemon_factory or (lambda: OpenVpnDaemon(command_runner))
        self._management_factory: ManagementFactory = management_factory or (
            lambda path, on_client: ManagementClient(path, on_client=on_client)
        )
        self.tasks = TaskSet()
        self._feed: OpenVpnFeed | None = None
        # What the daemon was last started with, kept after a failed start
        # too: the supervisor retries that, and an apply restarts the daemon
        # only when the feed carries something else. None before the first.
        self._launched: OpenVpnServerFeed | None = None
        self.daemon: OpenVpnDaemon | None = None
        self.management: ManagementClient | None = None
        self.daemon_version: str | None = None
        # Since the gateway started: daemon exits that were restarted, and
        # connections refused at admission.
        self.restarts = 0
        self.denied = 0
        # The certificate serial each live session presented, by the daemon's
        # client id: the reconcile sweep ends a session whose device has been
        # given another certificate since it connected.
        self._admitted: dict[int, str] = {}
        self._phase: TunnelEndpointPhase = "starting"
        self._error: str | None = None
        self._dir: str | None = None
        self._stopping = False
        # Taken by every path that starts or stops the daemon (an apply, the
        # supervisor's retries, stop), so two daemons are never started over
        # each other and a release never catches a start halfway.
        self._launch_lock = asyncio.Lock()
        # Set when a start of the daemon ends, whether it came up or failed:
        # the supervisor, idle while no daemon runs, takes up the new one (or
        # the retry of a failed start) at once.
        self._launched_event = asyncio.Event()
        self._held: set[asyncio.Task[Any]] = set()

    @property
    def running(self) -> bool:
        """Whether the daemon is up and its interface attached."""
        return self._phase == "running"

    async def start(self) -> None:
        """Start the supervisor and the session publisher; the daemon itself starts with the first feed."""
        self.tasks.spawn(WorkerName.OPENVPN_SUPERVISOR, self._supervise())
        self.tasks.spawn(WorkerName.OPENVPN_STATUS_PUBLISHER, self._publish_loop())

    async def stop(self) -> None:
        """Stop the loops, drop any change under way and stop the daemon."""
        self._stopping = True
        await self.tasks.stop()
        for task in list(self._held):
            task.cancel()
        await asyncio.gather(*self._held, return_exceptions=True)
        async with self._launch_lock:
            await self._release()
        self._phase = "stopped"

    # --- following the feed --------------------------------------------------------------

    def apply(self, feed: OpenVpnFeed | None) -> None:
        """Follow the feed's OpenVPN settings and devices, None while the install has no settings row; the directory changes at once.

        Revoked sessions end and the daemon restarts in the background, a failure reported in :meth:`state`, never raised.
        """
        before = {peer.id: peer for peer in self.directory.all()}
        self._feed = feed
        self.directory.replace_all(feed.peers if feed is not None else [])
        revoked = [
            peer_id
            for peer_id in sorted(before.keys() | {peer.id for peer in self.directory.all()})
            if _must_disconnect(before.get(peer_id), self.directory.get(peer_id))
        ]
        spawn_held(self._held, self._follow(revoked), name="openvpn_apply")

    async def settle(self) -> None:
        """Wait until the feeds applied so far have reached the daemon and their revoked sessions ended."""
        while self._held:
            await asyncio.gather(*list(self._held), return_exceptions=True)

    async def reconcile(self) -> int:
        """End every session the directory no longer allows; how many devices it ended sessions of.

        A session is a stray when its device is gone, disabled, or was given
        a new certificate after the session presented the old one.
        """
        management = self.management
        if management is None or not self.running:
            return 0
        sessions = await management.status()
        live = {session.cid for session in sessions if session.cid is not None}
        self._admitted = {cid: serial for cid, serial in self._admitted.items() if cid in live}
        strays = sorted({session.common_name for session in sessions if not self._allows(session)})
        if strays:
            logger.warning("OpenVPN sessions out of step with the feed", devices=len(strays))
        for peer_id in strays:
            await self._end_session(peer_id)
        return len(strays)

    def _allows(self, session: SessionStatus) -> bool:
        """Whether the directory still admits the session's device with the certificate it presented."""
        peer = self.directory.get(session.common_name)
        if peer is None or not peer.enabled:
            return False
        presented = self._admitted.get(session.cid) if session.cid is not None else None
        return presented is None or presented == peer.serial

    async def _follow(self, revoked: list[str]) -> None:
        """Bring the daemon in line with the feed, then end the sessions the change revoked.

        A restart ends every session anyway, so the kills are skipped then.
        """
        if await self._converge():
            return
        for peer_id in revoked:
            await self._end_session(peer_id)

    async def _converge(self) -> bool:
        """Start, restart or stop the daemon to run what the feed says; whether it did (sessions gone then)."""
        async with self._launch_lock:
            if self._stopping:
                return False
            feed = self._feed
            if feed is None:
                if self._launched is None:
                    return False
                await self._release()
                self._launched = None
                self._phase, self._error = "starting", None
                logger.info("OpenVPN endpoint down: the feed carries no OpenVPN settings")
                return True
            if feed.server == self._launched:
                # Running with it, or failed with it and retried by the supervisor.
                return False
            if self._launched is not None:
                logger.info("Restarting the OpenVPN daemon", reason="settings changed")
            await self._relaunch(feed.server)
            return True

    # --- the daemon ------------------------------------------------------------------------

    async def _relaunch(self, server: OpenVpnServerFeed) -> bool:
        """Under the launch lock: release what runs, start with ``server``, record the outcome; whether it runs."""
        await self._release()
        self._launched = server
        self._phase, self._error = "starting", None
        try:
            await self._launch(server)
        except Exception as exc:
            self._error = explain(exc) if isinstance(exc, CommandError) else str(exc)
            self._phase = "failed"
            logger.error("OpenVPN endpoint failed to start", error=self._error)
            await self._release()
            self._launched_event.set()
            return False
        self._phase = "running"
        self._launched_event.set()
        return True

    async def _launch(self, server: OpenVpnServerFeed) -> None:
        """Write the config, start the daemon, wait for its interface, attach it to the data plane."""
        if self._dir is None:
            # Private to this user (0700): the config carries the server key.
            self._dir = tempfile.mkdtemp(prefix="octoprox-openvpn-")
        socket_path = os.path.join(self._dir, "management.sock")
        config_path = os.path.join(self._dir, "server.conf")
        port = self._listen_port or server.endpoint_port
        text = render_server_conf(
            server, interface=self.interface, port=port, mtu=self._mtu, management_socket=socket_path
        )
        with contextlib.suppress(FileNotFoundError):
            os.unlink(socket_path)
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)

        daemon = self._daemon_factory()
        self.daemon = daemon
        await daemon.start(config_path)
        management = self._management_factory(socket_path, self._on_client)
        self.management = management
        try:
            await management.connect(timeout=STARTUP_TIMEOUT_SECONDS, still_alive=lambda: daemon.running)
            await management.wait_until_ready(timeout=STARTUP_TIMEOUT_SECONDS, still_alive=lambda: daemon.running)
            self.daemon_version = await management.version()
            await self._plane.attach(self.interface, server.gateway)
        except ManagementError as exc:
            raise RuntimeError(self._daemon_error(str(exc))) from None
        logger.info(
            "OpenVPN endpoint running",
            interface=self.interface,
            protocol=server.protocol,
            gateway=server.gateway,
            listen_port=port,
            version=self.daemon_version,
            peers=len(self.directory),
        )

    def _daemon_error(self, message: str) -> str:
        """The failure with what the daemon last said, which is where the reason usually is."""
        output = self.daemon.recent_output() if self.daemon is not None else ""
        return f"{message}\n{output}".strip() if output else message

    async def _release(self) -> None:
        """Detach, close the management link, stop the daemon; the config directory stays for a restart."""
        with contextlib.suppress(Exception):
            await self._plane.detach(self.interface)
        management, self.management = self.management, None
        if management is not None:
            with contextlib.suppress(Exception):
                await management.close()
        # Unset before the stop, so the supervisor waking on the exit sees a
        # replaced daemon, not one that died on its own.
        daemon, self.daemon = self.daemon, None
        if daemon is not None:
            with contextlib.suppress(Exception):
                await daemon.stop()
        self._admitted.clear()
        if self._stopping and self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    async def _supervise(self) -> None:
        """Start the daemon again when it exits on its own or a start failed, with backoff while it keeps failing.

        It waits on the daemon's exit while one runs, and on the next start
        while none does (before the first feed, or under a feed without
        OpenVPN settings): no timer looks again.
        """
        backoff = RESTART_BACKOFF_SECONDS
        while True:
            daemon = self.daemon
            if daemon is not None:
                code = await daemon.wait()
                if self._stopping:
                    return
                async with self._launch_lock:
                    if daemon is not self.daemon:
                        # Stopped by a deliberate restart: nothing to do.
                        continue
                    self.restarts += 1
                    self._error = self._daemon_error(f"openvpn exited with status {code}")
                    self._phase = "failed"
                    logger.error("OpenVPN daemon exited", status=code, restarts=self.restarts)
                    await self._release()
            if self._phase != "failed":
                # No daemon, and no start has failed: idle until the next start ends.
                self._launched_event.clear()
                await self._launched_event.wait()
                continue
            await asyncio.sleep(backoff)
            async with self._launch_lock:
                if self._stopping or self._phase != "failed" or self._feed is None:
                    continue
                if await self._relaunch(self._feed.server):
                    backoff = RESTART_BACKOFF_SECONDS
                else:
                    logger.error("OpenVPN endpoint will be started again", error=self._error, retry_in=backoff)
                    backoff = min(backoff * 2, RESTART_BACKOFF_MAX_SECONDS)

    # --- admitting devices ---------------------------------------------------------------

    async def _on_client(self, event: ClientEvent) -> None:
        """Answer the daemon about a connecting device, as the directory decides: admitted with its address, or refused.

        The certificate serial must be the one on record, so a rotated certificate stops working once the feed carries the new one.
        """
        management = self.management
        if management is None or event.kid is None or event.kind not in ("CONNECT", "REAUTH"):
            return
        peer = self.directory.get(event.common_name)
        reason: str | None = None
        if peer is None:
            reason = "unknown device"
        elif not peer.enabled:
            reason = "device disabled"
        elif event.serial is not None and event.serial != peer.serial:
            reason = "certificate replaced"
        if reason is not None:
            self.denied += 1
            logger.info("OpenVPN device refused", common_name=event.common_name, reason=reason, remote=event.remote)
            await management.deny(event.cid, event.kid, reason)
            return
        assert peer is not None
        if event.serial is not None:
            self._admitted[event.cid] = event.serial
        if event.kind == "REAUTH":
            await management.approve_without_push(event.cid, event.kid)
            return
        launched = self._launched
        if launched is None:
            return
        # The address is in the subnet the daemon runs, which is the one it was started with.
        await management.approve(event.cid, event.kid, [f"ifconfig-push {peer.address} {launched.network.netmask}"])
        logger.debug("OpenVPN device admitted", peer=peer.name, address=peer.address, remote=event.remote)

    async def _end_session(self, peer_id: str) -> None:
        """Drop the device's session on this gateway, if the daemon carries one."""
        management = self.management
        if management is None or not self.running:
            return
        try:
            if await management.kill(peer_id):
                logger.info("OpenVPN session ended", peer_id=peer_id)
        except ManagementError as exc:
            logger.warning("Could not end an OpenVPN session", peer_id=peer_id, error=str(exc))

    # --- status ----------------------------------------------------------------------------

    def state(self) -> TunnelEndpointState:
        """What this endpoint is doing, for the gateway's heartbeat snapshot."""
        transparent = self._plane.transparent_server
        server = self._feed.server if self._feed is not None else None
        return TunnelEndpointState(
            protocol=PROTOCOL,
            instance_id=self._instance_id,
            state=self._phase,
            error=self._error,
            listen_port=self._listen_port or (server.endpoint_port if server is not None else None),
            active_connections=transparent.active_connections if transparent is not None else 0,
            restarts=self.restarts,
            denied=self.denied,
            daemon_version=self.daemon_version,
            interface=self.interface,
            fake_ip_range=self._fake_ip_range,
            block_encrypted_dns=self._block_encrypted_dns,
        )

    async def publish_status(self) -> bool:
        """Publish the sessions the daemon carries, by device id; False when it runs none.

        Only devices of the directory are published: a session the directory
        does not know is the reconcile sweep's to end, not anyone's to show.
        """
        management = self.management
        if management is None or not self.running:
            return False
        statuses = {
            session.common_name: OpenVpnSessionReading(
                connected_since=session.connected_since,
                rx_bytes=session.rx_bytes,
                tx_bytes=session.tx_bytes,
                endpoint=session.real_address or None,
            ).model_dump_json()
            for session in await management.status()
            if self.directory.get(session.common_name) is not None
        }
        await self._redis.set_tunnel_peer_status(PROTOCOL, self._instance_id, statuses)
        return bool(statuses)

    async def _publish_loop(self) -> None:
        """Publisher loop: every gateway with OpenVPN enabled runs one."""
        job_stats.declare_interval(WorkerName.OPENVPN_STATUS_PUBLISHER, self._status_interval)
        while True:
            try:
                await asyncio.sleep(self._status_interval)
                with job_stats.track(WorkerName.OPENVPN_STATUS_PUBLISHER) as run:
                    if not await self.publish_status():
                        run.idle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("OpenVPN status publish failed", error=str(exc))
