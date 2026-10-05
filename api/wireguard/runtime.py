# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Builds and runs the WireGuard endpoint for one process.

Two halves with different reach. The *management* half (settings row, peer
directory, config rendering) runs on every instance, so any instance can
serve the admin API and every instance can route a peer's traffic should it
arrive there. The *data* half runs where ``wireguard.enabled`` is set: one
instance, or every replica behind a UDP-capable balancer, since the key
pair, peer list and fake-IP mapping are shared. It is the interface, which
this module owns, attached to the tunnel data plane the process shares
with every tunnel protocol (:mod:`api.tunnel.dataplane`): nftables, DNS and
the transparent listener live there. A failure to bring the data half up is
recorded and reported, never fatal to the process: the proxy ports keep
working and the admin page says what went wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import structlog

from api.core import utc_now
from api.core.config import Settings
from api.core.event_bus import event_bus
from api.core.job_stats import job_stats
from api.core.signals import project_changed, wireguard_peer_changed, wireguard_settings_changed
from api.core.stats import TunnelPeerMetricDelta
from api.core.workers import WorkerName
from api.db.redis import TUNNEL_STATUS_INTERVAL, RedisClient
from api.db.session import SessionFactory
from api.db.wireguard_repository import WireGuardPeerRepository, WireGuardSettingsRepository
from api.models.wireguard import (
    WireGuardPeer,
    WireGuardPeerStatus,
    WireGuardServerSettings,
    WireGuardState,
    WireGuardStatus,
)
from api.tunnel.dataplane import TunnelDataPlane
from api.tunnel.system import CommandError, CommandRunner, explain
from api.wireguard import keys
from api.wireguard.peers import PeerDirectory
from api.wireguard.system import WireGuardInterface

logger = structlog.get_logger()

# A peer whose last handshake is older than this is offline (WireGuard
# re-handshakes every two minutes while traffic flows, three with keepalive slack).
ONLINE_WINDOW_SECONDS = 180
SETTINGS_ENTITY_ID = "default"


def defaults_from_config(settings: Settings) -> WireGuardServerSettings:
    """The row a fresh install starts with: a new key pair plus ``wireguard.defaults``."""
    private, public = keys.generate_keypair()
    seed: dict[str, Any] = dict(settings.wireguard_defaults or {})
    try:
        return WireGuardServerSettings(private_key=private, public_key=public, **seed)
    except ValueError as exc:
        logger.error("Invalid wireguard.defaults in config, using built-in settings", error=str(exc))
        return WireGuardServerSettings(private_key=private, public_key=public)


class WireGuardSettingsStore:
    """Read-through cache of the ``wireguard_settings`` row, created on first load."""

    def __init__(self, settings: Settings, session_factory: SessionFactory | None) -> None:
        self._config = settings
        self._session_factory = session_factory
        self._settings: WireGuardServerSettings | None = None

    @property
    def settings(self) -> WireGuardServerSettings:
        if self._settings is None:
            self._settings = defaults_from_config(self._config)
        return self._settings

    async def load(self) -> WireGuardServerSettings:
        """Read the row; on a fresh install write the seed first (one instance wins, the rest adopt it)."""
        if self._session_factory is None:
            return self.settings
        async with self._session_factory() as session:
            repo = WireGuardSettingsRepository(session)
            stored = await repo.get()
            if stored is None:
                await repo.create_if_absent(defaults_from_config(self._config))
                await session.commit()
                stored = await repo.get()
        if stored is not None:
            self._settings = stored
        return self.settings

    async def save(self, settings: WireGuardServerSettings, updated_by: str | None) -> WireGuardServerSettings:
        if self._session_factory is not None:
            async with self._session_factory() as session:
                await WireGuardSettingsRepository(session).save(settings, updated_by=updated_by)
                await session.commit()
        self._settings = settings
        logger.info("WireGuard settings saved", updated_by=updated_by, endpoint=settings.endpoint_host)
        return settings


class WireGuardRuntime:
    """The WireGuard components of one instance and their lifecycle."""

    def __init__(
        self,
        settings: Settings,
        session_factory: SessionFactory | None,
        redis_client: RedisClient | None,
        tunnel: TunnelDataPlane,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self._config = settings
        self._session_factory = session_factory
        self._redis = redis_client
        self.settings_store = WireGuardSettingsStore(settings, session_factory)
        self.peers = PeerDirectory(session_factory)
        # The data plane routes a connection by the address it came from;
        # our peers are found there whichever instance the connection lands on.
        self.tunnel = tunnel
        tunnel.peers.add(self.peers)
        self.interface = WireGuardInterface(settings.wireguard_interface, runner)
        self.enabled = settings.wireguard_enabled
        self.state: WireGuardState = "disabled"
        self.error: str | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._sync_lock = asyncio.Lock()
        # A device's traffic totals, by device id. The proxy manager meters
        # tunnel devices alongside proxies and projects and holds the running
        # totals; the lifespan points this at it once the manager exists.
        # Left None where the runtime stands alone, and every total reads zero.
        self.peer_metrics: Callable[[str], TunnelPeerMetricDelta] | None = None

    # --- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        """Load the row and the peers; bring the tunnel up when this instance is the endpoint.

        The data plane must be started first: the interface is attached to it.
        """
        await self.settings_store.load()
        await self.peers.load()
        # A deleted project takes its peers with it in Postgres (cascade). The
        # directory follows at once: this receiver covers a deletion made on
        # this instance, the cross-instance handler (on_project_change, wired
        # by the lifespan) one made on a peer.
        project_changed.connect(self._on_project_changed)
        if not self.enabled:
            logger.info("WireGuard endpoint disabled on this instance", peers=len(self.peers))
            return
        self.state = "starting"
        try:
            await self._bring_up()
        except CommandError as exc:
            self.error = explain(exc)
        except Exception as exc:
            self.error = str(exc)
        if self.error:
            self.state = "failed"
            logger.error("WireGuard endpoint failed to start", error=self.error)
            await self._tear_down()
        else:
            self.state = "running"

    async def _bring_up(self) -> None:
        server = self.settings_store.settings
        gateway = server.gateway
        await self.interface.up(
            private_key=server.private_key,
            listen_port=self.listen_port or server.endpoint_port,
            address=f"{gateway}/{server.network.prefixlen}",
            mtu=self._config.wireguard_mtu,
            peers=self.peers.all(),
        )
        await self.tunnel.attach(self.interface.name, gateway)
        self._spawn(WorkerName.WIREGUARD_STATUS_PUBLISHER, self._status_publisher_loop())
        logger.info(
            "WireGuard endpoint running",
            interface=self.interface.name,
            backend=self.interface.backend,
            gateway=gateway,
            listen_port=self.listen_port or server.endpoint_port,
            peers=len(self.peers),
        )

    async def stop(self) -> None:
        project_changed.disconnect(self._on_project_changed)
        if self.state in ("disabled", "stopped"):
            return
        await self._tear_down()
        self.state = "stopped"

    async def _on_project_changed(
        self, _sender: Any, entity_id: str | None = None, op: str | None = None, **_: Any
    ) -> None:
        """Local blinker receiver: a project deleted on this instance."""
        if entity_id is not None:
            await self.on_project_change(entity_id, op)

    async def on_project_change(self, project_id: str, op: str | None) -> None:
        """A project changed here or on a peer (cross-instance handler for ``project_changed``).

        Deleting a project cascades to its peers in Postgres; the directory
        and the interface follow here, on every instance, so the devices lose
        access now rather than at the next full reload.
        """
        if op != "removed":
            return
        orphaned = [p.id for p in self.peers.all() if p.project_id == project_id]
        for peer_id in orphaned:
            self.peers.remove(peer_id)
        if orphaned:
            logger.info("Dropped peers of a deleted project", project_id=project_id, peers=len(orphaned))
            await self._sync_interface()

    async def _tear_down(self) -> None:
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        with contextlib.suppress(Exception):
            await self.tunnel.detach(self.interface.name)
        with contextlib.suppress(Exception):
            await self.interface.down()

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
        """The UDP port this instance listens on: the per-node override, else the install's endpoint port."""
        return self._config.wireguard_listen_port

    # --- what the proxy manager runs on our behalf -----------------------------------

    async def reload_peer(self, peer_id: str, op: str | None) -> None:
        """Cross-instance handler for ``wireguard_peer_changed``."""
        await self.peers.reload_one(peer_id, op)
        await self._sync_interface()

    async def reload_settings(self, _entity_id: str, _op: str | None) -> None:
        """Cross-instance handler for ``wireguard_settings_changed``."""
        before = self.settings_store.settings
        after = await self.settings_store.load()
        if self.running and before.gateway != after.gateway:
            logger.warning("WireGuard subnet changed; restart this instance to re-address the tunnel")
        await self._sync_interface()

    async def resync(self) -> None:
        """Reload hook: re-read settings and peers on the periodic full reload."""
        await self.settings_store.load()
        await self.peers.load()
        await self._sync_interface()

    async def _sync_interface(self) -> None:
        if not self.running:
            return
        server = self.settings_store.settings
        async with self._sync_lock:
            try:
                await self.interface.sync(
                    private_key=server.private_key,
                    listen_port=self.listen_port or server.endpoint_port,
                    peers=self.peers.all(),
                )
            except CommandError as exc:
                logger.error("WireGuard peer sync failed", error=explain(exc))

    # --- writes ----------------------------------------------------------------------

    async def add_peer(self, peer: WireGuardPeer) -> WireGuardPeer:
        if self._session_factory is not None:
            async with self._session_factory() as session:
                await WireGuardPeerRepository(session).create(peer)
                await session.commit()
        self.peers.put(peer)
        await self._sync_interface()
        await event_bus.publish(wireguard_peer_changed, self, entity_id=peer.id, op="added")
        logger.info("WireGuard peer added", peer_id=peer.id, name=peer.name, project_id=peer.project_id)
        return peer

    async def update_peer(self, peer: WireGuardPeer) -> WireGuardPeer:
        if self._session_factory is not None:
            async with self._session_factory() as session:
                await WireGuardPeerRepository(session).update(peer)
                await session.commit()
        self.peers.put(peer)
        await self._sync_interface()
        await event_bus.publish(wireguard_peer_changed, self, entity_id=peer.id, op="updated")
        return peer

    async def remove_peer(self, peer_id: str) -> bool:
        if self._session_factory is not None:
            async with self._session_factory() as session:
                await WireGuardPeerRepository(session).delete(peer_id)
                await session.commit()
        removed = self.peers.remove(peer_id) is not None
        await self._sync_interface()
        if self._redis is not None:
            # The device's history went with its row; its unflushed window
            # would otherwise be flushed against a device that is gone (and
            # dropped there, with a warning).
            with contextlib.suppress(Exception):
                await self._redis.reset_tunnel_peer_metrics(peer_id)
        await event_bus.publish(wireguard_peer_changed, self, entity_id=peer_id, op="removed")
        return removed

    async def save_settings(self, settings: WireGuardServerSettings, updated_by: str | None) -> WireGuardServerSettings:
        saved = await self.settings_store.save(settings, updated_by)
        await self._sync_interface()
        await event_bus.publish(wireguard_settings_changed, self, entity_id=SETTINGS_ENTITY_ID, op="updated")
        return saved

    # --- status ----------------------------------------------------------------------

    async def _status_publisher_loop(self) -> None:
        """Every instance that carries the tunnel publishes what ``wg`` reports about its peers."""
        job_stats.declare_interval(WorkerName.WIREGUARD_STATUS_PUBLISHER, TUNNEL_STATUS_INTERVAL)
        while True:
            try:
                with job_stats.track(WorkerName.WIREGUARD_STATUS_PUBLISHER) as run:
                    if not await self._publish_status():
                        run.idle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("WireGuard status publish failed", error=str(exc))
            await asyncio.sleep(TUNNEL_STATUS_INTERVAL)

    async def _publish_status(self) -> bool:
        """Push the peer counters to Redis and persist new handshakes; False when there was nothing to publish.

        The Redis reading is the live view, gone half a minute after this
        instance stops publishing. A handshake newer than the device's row
        knows is also written to the row, so when a device was last seen,
        and from where, survives this instance restarting (which starts the
        interface's counters from zero) and is visible from every instance.
        """
        if self._redis is None:
            return False
        by_key = {p.public_key: p for p in self.peers.all()}
        statuses = {}
        advanced: list[tuple[WireGuardPeer, datetime, str | None]] = []
        for dump in await self.interface.dump():
            statuses[dump.public_key] = json.dumps(
                {
                    "latest_handshake": dump.latest_handshake,
                    "rx_bytes": dump.rx_bytes,
                    "tx_bytes": dump.tx_bytes,
                    "endpoint": dump.endpoint,
                }
            )
            peer = by_key.get(dump.public_key)
            if peer is not None and dump.latest_handshake:
                seen = _naive_utc(dump.latest_handshake)
                if peer.last_handshake_at is None or peer.last_handshake_at < seen:
                    advanced.append((peer, seen, dump.endpoint))
        await self._redis.set_tunnel_peer_status("wireguard", self._config.instance_id, statuses)
        await self._persist_last_seen(advanced)
        return bool(statuses)

    async def _persist_last_seen(self, advanced: list[tuple[WireGuardPeer, datetime, str | None]]) -> None:
        """Record handshakes newer than the cached rows know; the cache follows, so the next tick skips them.

        The row only moves forward (see the repository), so two instances
        carrying the device in turn cannot regress it. The cache is updated
        after the write: a failed write is retried on the next tick.
        """
        if not advanced:
            return
        if self._session_factory is not None:
            async with self._session_factory() as session:
                await WireGuardPeerRepository(session).record_last_seen(
                    [(peer.id, seen, endpoint) for peer, seen, endpoint in advanced]
                )
                await session.commit()
        for peer, seen, endpoint in advanced:
            peer.last_handshake_at = seen
            peer.last_endpoint = endpoint

    async def peer_statuses(self) -> dict[str, WireGuardPeerStatus]:
        """Live readings per public key, merged across every instance carrying the tunnel.

        Behind a UDP load balancer several instances carry sessions at once
        and each publishes what its interface knows. A peer appears in every
        carrier's dump (they all configure every peer), so the reading that
        counts is the one with the latest handshake: that is where the device's
        traffic currently lands. Empty when nothing is carrying the tunnel;
        ``peer_status`` fills in the persisted sighting then.
        """
        if self._redis is None:
            return {}
        try:
            published = await self._redis.get_tunnel_peer_status("wireguard")
        except Exception as exc:
            logger.debug("Peer status unavailable", error=str(exc))
            return {}
        now = utc_now()
        latest: dict[str, int] = {}
        statuses: dict[str, WireGuardPeerStatus] = {}
        for carrier in published:
            for public_key, payload in carrier.items():
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                handshake = int(data.get("latest_handshake") or 0)
                if public_key in statuses and handshake <= latest[public_key]:
                    continue
                latest[public_key] = handshake
                seen = _naive_utc(handshake) if handshake else None
                statuses[public_key] = WireGuardPeerStatus(
                    online=seen is not None and _within_online_window(seen, now),
                    last_handshake_at=seen,
                    endpoint=data.get("endpoint"),
                    live=True,
                    rx_bytes=int(data.get("rx_bytes") or 0),
                    tx_bytes=int(data.get("tx_bytes") or 0),
                )
        return statuses

    @staticmethod
    def peer_status(peer: WireGuardPeer, live: WireGuardPeerStatus | None) -> WireGuardPeerStatus:
        """Where a device stands: the live reading, or the persisted sighting when that is all there is or it is newer.

        A carrier that just restarted reports no handshake for a device that
        has not reconnected yet; the row still knows when it was last seen.
        """
        if live is not None and live.last_handshake_at is not None and (
            peer.last_handshake_at is None or peer.last_handshake_at <= live.last_handshake_at
        ):
            return live
        seen = peer.last_handshake_at
        return WireGuardPeerStatus(
            online=seen is not None and _within_online_window(seen, utc_now()),
            last_handshake_at=seen,
            endpoint=peer.last_endpoint if seen is not None else None,
            live=False,
            rx_bytes=live.rx_bytes if live is not None else 0,
            tx_bytes=live.tx_bytes if live is not None else 0,
        )

    def metrics_of(self, peer_id: str) -> TunnelPeerMetricDelta:
        """A device's traffic totals as the proxy manager has them; zero without a manager."""
        if self.peer_metrics is None:
            return TunnelPeerMetricDelta()
        return self.peer_metrics(peer_id)

    async def status(self) -> WireGuardStatus:
        live = await self.peer_statuses()
        peers = self.peers.all()
        server = self.tunnel.transparent_server
        # Every device's name-resolution signals, all time, from the same
        # totals the device rows show: cluster-wide, not this instance's.
        totals = TunnelPeerMetricDelta.summed(*(self.metrics_of(p.id) for p in peers))
        return WireGuardStatus(
            enabled=self.enabled,
            state=self.state,
            error=self.error,
            instance_id=self._config.instance_id,
            interface=self.interface.name,
            backend=self.interface.backend,
            listen_port=(self.listen_port or self.settings_store.settings.endpoint_port) if self.enabled else None,
            transparent_port=self._config.tunnel_transparent_port,
            dns_port=self._config.tunnel_dns_port,
            fake_ip_range=str(self.tunnel.pool.network),
            active_connections=server.active_connections if server else 0,
            peers_total=len(peers),
            peers_enabled=self.peers.enabled_count,
            peers_online=sum(1 for p in peers if self.peer_status(p, live.get(p.public_key)).online),
            connections_by_address=totals.by_address,
            encrypted_dns_blocked=totals.encrypted_dns_blocked,
            block_encrypted_dns=self._config.tunnel_block_encrypted_dns,
        )


def _naive_utc(epoch: int) -> datetime:
    """Naive UTC, like every other timestamp the API serialises."""
    return datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None)


def _within_online_window(seen: datetime, now: datetime) -> bool:
    return (now - seen).total_seconds() <= ONLINE_WINDOW_SECONDS
