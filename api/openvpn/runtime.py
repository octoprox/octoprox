# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Builds and runs the OpenVPN endpoint for one process.

The same two halves as WireGuard. The *management* half (settings row with
the CA, peer directory, profile rendering) runs on every instance. The
*data* half runs where ``openvpn.enabled`` is set: the daemon this module
starts and supervises, admitting devices through its management interface
against the peer directory, with its tun interface attached to the tunnel
data plane the process shares with every tunnel protocol
(:mod:`api.tunnel.dataplane`). A failure to bring it up is recorded and
reported, never fatal to the process; a daemon that exits on its own is
started again with backoff.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog

from api.core.config import Settings
from api.core.event_bus import event_bus
from api.core.job_stats import job_stats
from api.core.signals import openvpn_peer_changed, openvpn_settings_changed, project_changed
from api.core.stats import TunnelPeerMetricDelta
from api.core.workers import WorkerName
from api.db.openvpn_repository import OpenVpnPeerRepository, OpenVpnSettingsRepository
from api.db.redis import TUNNEL_STATUS_INTERVAL, RedisClient
from api.db.session import SessionFactory
from api.models.openvpn import (
    OpenVpnPeer,
    OpenVpnPeerStatus,
    OpenVpnServerSettings,
    OpenVpnState,
    OpenVpnStatus,
)
from api.openvpn import pki
from api.openvpn.config import render_server_conf
from api.openvpn.daemon import OpenVpnDaemon
from api.openvpn.management import ClientEvent, ManagementClient, ManagementError
from api.openvpn.peers import PeerDirectory
from api.tunnel.dataplane import TunnelDataPlane
from api.tunnel.system import CommandError, CommandRunner, explain

logger = structlog.get_logger()

PROTOCOL = "openvpn"
SETTINGS_ENTITY_ID = "default"
RESTART_BACKOFF_SECONDS = 5.0
RESTART_BACKOFF_MAX_SECONDS = 60.0
# How long the daemon gets to create its management socket and bring the interface up.
STARTUP_TIMEOUT_SECONDS = 30.0

DaemonFactory = Callable[[], OpenVpnDaemon]
ManagementFactory = Callable[[str, Callable[[ClientEvent], Coroutine[Any, Any, None]]], ManagementClient]


def new_identity() -> dict[str, Any]:
    """A fresh CA, a server certificate it signed and a tls-crypt key."""
    ca_cert, ca_key = pki.generate_ca()
    server_cert, server_key = pki.issue_server_certificate(ca_cert, ca_key)
    return {
        "ca_cert": ca_cert,
        "ca_key": ca_key,
        "server_cert": server_cert,
        "server_key": server_key,
        "tls_crypt_key": pki.generate_tls_crypt_key(),
    }


def defaults_from_config(settings: Settings) -> OpenVpnServerSettings:
    """The row a fresh install starts with: a new identity plus ``openvpn.defaults``."""
    identity = new_identity()
    seed: dict[str, Any] = dict(settings.openvpn_defaults or {})
    try:
        return OpenVpnServerSettings(**identity, **seed)
    except (ValueError, TypeError) as exc:
        logger.error("Invalid openvpn.defaults in config, using built-in settings", error=str(exc))
        return OpenVpnServerSettings(**identity)


class OpenVpnSettingsStore:
    """Read-through cache of the ``openvpn_settings`` row, created on first load."""

    def __init__(self, settings: Settings, session_factory: SessionFactory | None) -> None:
        self._config = settings
        self._session_factory = session_factory
        self._settings: OpenVpnServerSettings | None = None

    @property
    def settings(self) -> OpenVpnServerSettings:
        if self._settings is None:
            self._settings = defaults_from_config(self._config)
        return self._settings

    async def load(self) -> OpenVpnServerSettings:
        """Read the row; on a fresh install write the seed first (one instance wins, the rest adopt it)."""
        if self._session_factory is None:
            return self.settings
        async with self._session_factory() as session:
            repo = OpenVpnSettingsRepository(session)
            stored = await repo.get()
            if stored is None:
                await repo.create_if_absent(defaults_from_config(self._config))
                await session.commit()
                stored = await repo.get()
        if stored is not None:
            self._settings = stored
        return self.settings

    async def save(self, settings: OpenVpnServerSettings, updated_by: str | None) -> OpenVpnServerSettings:
        if self._session_factory is not None:
            async with self._session_factory() as session:
                await OpenVpnSettingsRepository(session).save(settings, updated_by=updated_by)
                await session.commit()
        self.adopt(settings)
        logger.info("OpenVPN settings saved", updated_by=updated_by, endpoint=settings.endpoint_host)
        return settings

    def adopt(self, settings: OpenVpnServerSettings) -> None:
        """Take a row written elsewhere (in a transaction of the caller's) as the current settings."""
        self._settings = settings


class OpenVpnRuntime:
    """The OpenVPN components of one instance and their lifecycle."""

    def __init__(
        self,
        settings: Settings,
        session_factory: SessionFactory | None,
        redis_client: RedisClient | None,
        tunnel: TunnelDataPlane,
        *,
        runner: CommandRunner | None = None,
        daemon_factory: DaemonFactory | None = None,
        management_factory: ManagementFactory | None = None,
    ) -> None:
        self._config = settings
        self._session_factory = session_factory
        self._redis = redis_client
        self.settings_store = OpenVpnSettingsStore(settings, session_factory)
        self.peers = PeerDirectory(session_factory)
        self.tunnel = tunnel
        tunnel.peers.add(self.peers)
        self.interface = settings.openvpn_interface
        self.enabled = settings.openvpn_enabled
        self.state: OpenVpnState = "disabled"
        self.error: str | None = None
        self.daemon_version: str | None = None
        self.restarts = 0
        self.denied = 0
        runner = runner or CommandRunner()
        self._daemon_factory: DaemonFactory = daemon_factory or (lambda: OpenVpnDaemon(runner))
        self._management_factory: ManagementFactory = management_factory or (
            lambda path, on_client: ManagementClient(path, on_client=on_client)
        )
        self.daemon: OpenVpnDaemon | None = None
        self.management: ManagementClient | None = None
        self._dir: str | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._launch_lock = asyncio.Lock()
        # A device's traffic totals, by device id; the lifespan points this at
        # the proxy manager once it exists (see the WireGuard runtime).
        self.peer_metrics: Callable[[str], TunnelPeerMetricDelta] | None = None

    # --- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        """Load the row and the peers; start the daemon when this instance is the endpoint.

        The data plane must be started first: the daemon's interface is attached to it.
        """
        await self.settings_store.load()
        await self.peers.load()
        project_changed.connect(self._on_project_changed)
        if not self.enabled:
            logger.info("OpenVPN endpoint disabled on this instance", peers=len(self.peers))
            return
        self.state = "starting"
        async with self._launch_lock:
            try:
                await self._launch()
            except CommandError as exc:
                self.error = explain(exc)
            except Exception as exc:
                self.error = str(exc)
            if self.error:
                self.state = "failed"
                logger.error("OpenVPN endpoint failed to start", error=self.error)
                await self._release()
            else:
                self.state = "running"
                self._ensure_workers()

    async def stop(self) -> None:
        project_changed.disconnect(self._on_project_changed)
        if self.state in ("disabled", "stopped"):
            return
        self._stopping = True
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        # A restart under way finishes first, so nothing is launched after the release.
        async with self._launch_lock:
            await self._release()
        self.state = "stopped"

    async def _launch(self) -> None:
        """Write the config, start the daemon, wait for its interface, attach it to the data plane.

        Called with ``_launch_lock`` held: every path that starts or stops
        the daemon (start, a restart, the supervisor's retries, stop) takes
        it, so two daemons are never started over each other and a release
        never catches a launch halfway.
        """
        server = self.settings_store.settings
        if self._dir is None:
            # Private to this user (0700): the config carries the server key.
            self._dir = tempfile.mkdtemp(prefix="octoprox-openvpn-")
        socket_path = os.path.join(self._dir, "management.sock")
        config_path = os.path.join(self._dir, "server.conf")
        text = render_server_conf(
            server,
            interface=self.interface,
            port=self.listen_port or server.endpoint_port,
            mtu=self._config.openvpn_mtu,
            management_socket=socket_path,
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
            await self.tunnel.attach(self.interface, server.gateway)
        except ManagementError as exc:
            raise RuntimeError(self._daemon_error(str(exc))) from None
        logger.info(
            "OpenVPN endpoint running",
            interface=self.interface,
            protocol=server.protocol,
            gateway=server.gateway,
            listen_port=self.listen_port or server.endpoint_port,
            version=self.daemon_version,
            peers=len(self.peers),
        )

    def _daemon_error(self, message: str) -> str:
        """The failure with what the daemon last said, which is where the reason usually is."""
        output = self.daemon.recent_output() if self.daemon is not None else ""
        return f"{message}\n{output}".strip() if output else message

    async def _release(self) -> None:
        """Detach, close the management connection, stop the daemon; the config directory stays for a relaunch."""
        with contextlib.suppress(Exception):
            await self.tunnel.detach(self.interface)
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
        if self._stopping and self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    async def _relaunch(self) -> bool:
        """Under the launch lock: release what runs, start again, record the outcome; whether it is running."""
        await self._release()
        self.state = "starting"
        self.error = None
        try:
            await self._launch()
        except Exception as exc:
            self.error = explain(exc) if isinstance(exc, CommandError) else str(exc)
            self.state = "failed"
            logger.error("OpenVPN endpoint failed to start", error=self.error)
            await self._release()
            return False
        self.state = "running"
        self._ensure_workers()
        return True

    async def _restart(self, reason: str) -> None:
        """Stop and start the daemon (sessions drop; devices reconnect on their own).

        Also how a failed endpoint comes back once its settings are fixed.
        Waits for a launch or retry in progress, then decides on the state
        that left behind, so a change made during one is not lost.
        """
        if not self.enabled:
            return
        async with self._launch_lock:
            if self._stopping or self.state not in ("running", "failed"):
                return
            logger.info("Restarting the OpenVPN daemon", reason=reason)
            await self._relaunch()

    async def _supervisor_loop(self) -> None:
        """Start the daemon again when it exits on its own or a relaunch failed, with backoff while it keeps failing."""
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
                    self.error = self._daemon_error(f"openvpn exited with status {code}")
                    self.state = "failed"
                    logger.error("OpenVPN daemon exited", status=code, restarts=self.restarts)
                    await self._release()
            if self.state != "failed":
                # Running, or a restart is bringing a new daemon up: look again shortly.
                await asyncio.sleep(RESTART_BACKOFF_SECONDS)
                continue
            await asyncio.sleep(backoff)
            async with self._launch_lock:
                if self._stopping or self.state != "failed":
                    continue
                if await self._relaunch():
                    backoff = RESTART_BACKOFF_SECONDS
                else:
                    logger.error("OpenVPN endpoint will be started again", error=self.error, retry_in=backoff)
                    backoff = min(backoff * 2, RESTART_BACKOFF_MAX_SECONDS)

    def _ensure_workers(self) -> None:
        """The supervisor and the status publisher, started once the daemon first runs."""
        if not self._tasks:
            self._spawn(WorkerName.OPENVPN_SUPERVISOR, self._supervisor_loop())
            self._spawn(WorkerName.OPENVPN_STATUS_PUBLISHER, self._status_publisher_loop())

    def _spawn(self, name: str, coro: Coroutine[Any, Any, None]) -> None:
        self._tasks.append(asyncio.create_task(coro, name=name))

    @property
    def background_tasks(self) -> list[asyncio.Task[None]]:
        return list(self._tasks)

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def listen_port(self) -> int | None:
        return self._config.openvpn_listen_port

    # --- admitting devices ----------------------------------------------------------

    async def _on_client(self, event: ClientEvent) -> None:
        """Answer the daemon about a connecting device: admitted with its address, or refused.

        The peer directory decides. The common name is the device id; the
        certificate serial must be the one on record, so a rotated
        certificate stops working the moment the row changes, with no
        revocation list.
        """
        management = self.management
        if management is None or event.kid is None or event.kind not in ("CONNECT", "REAUTH"):
            return
        peer = self.peers.get(event.common_name)
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
        if event.kind == "REAUTH":
            await management.approve_without_push(event.cid, event.kid)
            return
        netmask = self.settings_store.settings.network.netmask
        await management.approve(event.cid, event.kid, [f"ifconfig-push {peer.address} {netmask}"])
        logger.debug("OpenVPN device admitted", peer=peer.name, address=peer.address, remote=event.remote)

    async def _end_session(self, peer_id: str) -> None:
        """Drop the device's session on this instance, if the daemon carries one."""
        management = self.management
        if management is None or not self.running:
            return
        try:
            if await management.kill(peer_id):
                logger.info("OpenVPN session ended", peer_id=peer_id)
        except ManagementError as exc:
            logger.warning("Could not end an OpenVPN session", peer_id=peer_id, error=str(exc))

    # --- what the proxy manager runs on our behalf -----------------------------------

    async def reload_peer(self, peer_id: str, op: str | None) -> None:
        """Cross-instance handler for ``openvpn_peer_changed``."""
        before = self.peers.get(peer_id)
        after = await self.peers.reload_one(peer_id, op)
        if _must_disconnect(before, after):
            await self._end_session(peer_id)

    async def reload_settings(self, _entity_id: str, _op: str | None) -> None:
        """Cross-instance handler for ``openvpn_settings_changed``."""
        await self._adopt_settings()

    async def resync(self) -> None:
        """Reload hook: re-read settings and peers on the periodic full reload.

        The safety net for a missed change event and what a restored backup
        relies on, so the daemon follows the row here just as on the event.
        """
        await self.peers.load()
        await self._adopt_settings()

    async def _adopt_settings(self) -> None:
        """Re-read the row and follow it: peers reissued under a new CA, the daemon restarted when what it runs with changed."""
        before = self.settings_store.settings
        after = await self.settings_store.load()
        if before.ca_cert != after.ca_cert:
            # A new CA reissued every device's certificate with it.
            await self.peers.load()
        if before.daemon_signature() != after.daemon_signature():
            await self._restart("settings changed")

    async def _on_project_changed(
        self, _sender: Any, entity_id: str | None = None, op: str | None = None, **_: Any
    ) -> None:
        if entity_id is not None:
            await self.on_project_change(entity_id, op)

    async def on_project_change(self, project_id: str, op: str | None) -> None:
        """A deleted project takes its devices with it (cascade); drop them and their sessions here too."""
        if op != "removed":
            return
        orphaned = [p.id for p in self.peers.all() if p.project_id == project_id]
        for peer_id in orphaned:
            self.peers.remove(peer_id)
            await self._end_session(peer_id)
        if orphaned:
            logger.info("Dropped OpenVPN peers of a deleted project", project_id=project_id, peers=len(orphaned))

    # --- writes ----------------------------------------------------------------------

    def issue_peer(
        self,
        *,
        project_id: str,
        name: str,
        enabled: bool = True,
        session_id: str | None = None,
        country: str | None = None,
        state: str | None = None,
        city: str | None = None,
    ) -> OpenVpnPeer:
        """A new device with a certificate from the install's CA and the next free address (not yet stored)."""
        server = self.settings_store.settings
        peer_id = str(uuid4())
        certificate, private_key = pki.issue_client_certificate(server.ca_cert, server.ca_key, peer_id)
        return OpenVpnPeer(
            id=peer_id,
            project_id=project_id,
            name=name,
            certificate=certificate,
            private_key=private_key,
            serial=pki.serial_of(certificate),
            certificate_expires_at=pki.not_after_of(certificate),
            address=self.peers.allocate_address(server.network),
            enabled=enabled,
            session_id=session_id,
            country=country,
            state=state,
            city=city,
        )

    def reissue_certificate(self, peer: OpenVpnPeer, server: OpenVpnServerSettings | None = None) -> OpenVpnPeer:
        """The device with a new certificate and key under the CA (the current one unless given)."""
        server = server or self.settings_store.settings
        certificate, private_key = pki.issue_client_certificate(server.ca_cert, server.ca_key, peer.id)
        return peer.model_copy(
            update={
                "certificate": certificate,
                "private_key": private_key,
                "serial": pki.serial_of(certificate),
                "certificate_expires_at": pki.not_after_of(certificate),
            }
        )

    async def add_peer(self, peer: OpenVpnPeer) -> OpenVpnPeer:
        if self._session_factory is not None:
            async with self._session_factory() as session:
                await OpenVpnPeerRepository(session).create(peer)
                await session.commit()
        self.peers.put(peer)
        await event_bus.publish(openvpn_peer_changed, self, entity_id=peer.id, op="added")
        logger.info("OpenVPN peer added", peer_id=peer.id, name=peer.name, project_id=peer.project_id)
        return peer

    async def update_peer(self, peer: OpenVpnPeer) -> OpenVpnPeer:
        before = self.peers.get(peer.id)
        if self._session_factory is not None:
            async with self._session_factory() as session:
                await OpenVpnPeerRepository(session).update(peer)
                await session.commit()
        self.peers.put(peer)
        if _must_disconnect(before, peer):
            await self._end_session(peer.id)
        await event_bus.publish(openvpn_peer_changed, self, entity_id=peer.id, op="updated")
        return peer

    async def remove_peer(self, peer_id: str) -> bool:
        if self._session_factory is not None:
            async with self._session_factory() as session:
                await OpenVpnPeerRepository(session).delete(peer_id)
                await session.commit()
        removed = self.peers.remove(peer_id) is not None
        await self._end_session(peer_id)
        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.reset_tunnel_peer_metrics(peer_id)
        await event_bus.publish(openvpn_peer_changed, self, entity_id=peer_id, op="removed")
        return removed

    async def save_settings(self, settings: OpenVpnServerSettings, updated_by: str | None) -> OpenVpnServerSettings:
        before = self.settings_store.settings
        saved = await self.settings_store.save(settings, updated_by)
        if before.daemon_signature() != saved.daemon_signature():
            await self._restart("settings changed")
        await event_bus.publish(openvpn_settings_changed, self, entity_id=SETTINGS_ENTITY_ID, op="updated")
        return saved

    async def rotate_identity(self, updated_by: str | None) -> OpenVpnServerSettings:
        """A new CA, server certificate and tls-crypt key, and every device reissued under them.

        One transaction: the row and every peer change together, so no
        instance ever sees a device whose certificate the CA did not sign.
        The devices are the rows, not this instance's directory: one added
        elsewhere that has not reached the directory yet is reissued too,
        since a certificate the new CA did not sign would never connect
        again. Every device profile has to be loaded again afterwards.
        """
        updated = self.settings_store.settings.model_copy(update=new_identity())
        if self._session_factory is not None:
            async with self._session_factory() as session:
                await OpenVpnSettingsRepository(session).save(updated, updated_by=updated_by)
                repo = OpenVpnPeerRepository(session)
                reissued = [self.reissue_certificate(peer, updated) for peer in await repo.get_all()]
                for peer in reissued:
                    await repo.update(peer)
                await session.commit()
        else:
            reissued = [self.reissue_certificate(peer, updated) for peer in self.peers.all()]
        self.settings_store.adopt(updated)
        self.peers.replace_all(reissued)
        logger.warning("OpenVPN identity rotated; every device profile is now invalid", by=updated_by, devices=len(reissued))
        await self._restart("identity rotated")
        await event_bus.publish(openvpn_settings_changed, self, entity_id=SETTINGS_ENTITY_ID, op="updated")
        return updated

    # --- status ----------------------------------------------------------------------

    async def _status_publisher_loop(self) -> None:
        job_stats.declare_interval(WorkerName.OPENVPN_STATUS_PUBLISHER, TUNNEL_STATUS_INTERVAL)
        while True:
            try:
                with job_stats.track(WorkerName.OPENVPN_STATUS_PUBLISHER) as run:
                    if not await self._publish_status():
                        run.idle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("OpenVPN status publish failed", error=str(exc))
            await asyncio.sleep(TUNNEL_STATUS_INTERVAL)

    async def _publish_status(self) -> bool:
        """Push the sessions the daemon carries to Redis and persist new ones; False when nothing was published."""
        management = self.management
        if self._redis is None or management is None or not self.running:
            return False
        statuses: dict[str, str] = {}
        advanced: list[tuple[OpenVpnPeer, datetime, str | None]] = []
        for session in await management.status():
            peer = self.peers.get(session.common_name)
            if peer is None:
                continue
            statuses[peer.id] = json.dumps(
                {
                    "connected_since": session.connected_since,
                    "rx_bytes": session.rx_bytes,
                    "tx_bytes": session.tx_bytes,
                    "endpoint": session.real_address or None,
                }
            )
            if session.connected_since:
                seen = _naive_utc(session.connected_since)
                if peer.last_connected_at is None or peer.last_connected_at < seen:
                    advanced.append((peer, seen, session.real_address or None))
        await self._redis.set_tunnel_peer_status(PROTOCOL, self._config.instance_id, statuses)
        await self._persist_last_seen(advanced)
        return bool(statuses)

    async def _persist_last_seen(self, advanced: list[tuple[OpenVpnPeer, datetime, str | None]]) -> None:
        if not advanced:
            return
        if self._session_factory is not None:
            async with self._session_factory() as session:
                await OpenVpnPeerRepository(session).record_last_seen(
                    [(peer.id, seen, endpoint) for peer, seen, endpoint in advanced]
                )
                await session.commit()
        for peer, seen, endpoint in advanced:
            peer.last_connected_at = seen
            peer.last_endpoint = endpoint

    async def peer_statuses(self) -> dict[str, OpenVpnPeerStatus]:
        """Live sessions per device id, merged across every instance carrying the endpoint.

        A device has one session at a time; should two carriers both claim
        it (a reconnect landing elsewhere before the old session timed out),
        the newer session wins.
        """
        if self._redis is None:
            return {}
        try:
            published = await self._redis.get_tunnel_peer_status(PROTOCOL)
        except Exception as exc:
            logger.debug("Peer status unavailable", error=str(exc))
            return {}
        latest: dict[str, int] = {}
        statuses: dict[str, OpenVpnPeerStatus] = {}
        for carrier in published:
            for peer_id, payload in carrier.items():
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                since = int(data.get("connected_since") or 0)
                if peer_id in statuses and since <= latest[peer_id]:
                    continue
                latest[peer_id] = since
                seen = _naive_utc(since) if since else None
                statuses[peer_id] = OpenVpnPeerStatus(
                    online=True,
                    connected_since=seen,
                    last_seen_at=seen,
                    endpoint=data.get("endpoint"),
                    live=True,
                    rx_bytes=int(data.get("rx_bytes") or 0),
                    tx_bytes=int(data.get("tx_bytes") or 0),
                )
        return statuses

    @staticmethod
    def peer_status(peer: OpenVpnPeer, live: OpenVpnPeerStatus | None) -> OpenVpnPeerStatus:
        """Where a device stands: the live session, or the persisted last sighting."""
        if live is not None:
            return live
        return OpenVpnPeerStatus(
            online=False,
            connected_since=None,
            last_seen_at=peer.last_connected_at,
            endpoint=peer.last_endpoint if peer.last_connected_at is not None else None,
            live=False,
        )

    def metrics_of(self, peer_id: str) -> TunnelPeerMetricDelta:
        if self.peer_metrics is None:
            return TunnelPeerMetricDelta()
        return self.peer_metrics(peer_id)

    async def status(self) -> OpenVpnStatus:
        live = await self.peer_statuses()
        peers = self.peers.all()
        server = self.settings_store.settings
        transparent = self.tunnel.transparent_server
        totals = TunnelPeerMetricDelta.summed(*(self.metrics_of(p.id) for p in peers))
        return OpenVpnStatus(
            enabled=self.enabled,
            state=self.state,
            error=self.error,
            instance_id=self._config.instance_id,
            interface=self.interface,
            protocol=server.protocol,
            listen_port=(self.listen_port or server.endpoint_port) if self.enabled else None,
            daemon_version=self.daemon_version,
            restarts=self.restarts,
            denied=self.denied,
            transparent_port=self._config.tunnel_transparent_port,
            dns_port=self._config.tunnel_dns_port,
            fake_ip_range=str(self.tunnel.pool.network),
            active_connections=transparent.active_connections if transparent else 0,
            peers_total=len(peers),
            peers_enabled=self.peers.enabled_count,
            peers_online=sum(1 for p in peers if p.id in live),
            connections_by_address=totals.by_address,
            encrypted_dns_blocked=totals.encrypted_dns_blocked,
            block_encrypted_dns=self._config.tunnel_block_encrypted_dns,
        )


def _must_disconnect(before: OpenVpnPeer | None, after: OpenVpnPeer | None) -> bool:
    """Whether a change to a device means a session it may have must end now."""
    if after is None:
        return before is not None
    if not after.enabled:
        return before is None or before.enabled
    return before is not None and before.serial != after.serial


def _naive_utc(epoch: int) -> datetime:
    """Naive UTC, like every other timestamp the API serialises."""
    return datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None)
