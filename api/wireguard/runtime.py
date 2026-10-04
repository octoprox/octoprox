# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Builds and runs the WireGuard endpoint for one process.

Two halves with different reach. The *management* half (settings row, peer
directory, config rendering) runs on every instance, so any instance can
serve the admin API and every instance can route a peer's traffic should it
arrive there. The *data* half (interface, nftables, DNS, transparent
listener) runs where ``wireguard.enabled`` is set: one instance, or every
replica behind a UDP-capable balancer, since the key pair, peer list and
fake-IP mapping are shared. A failure to bring the data half up is
recorded and reported, never fatal to the process: the proxy ports keep
working and the admin page says what went wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from api.core.config import Settings
from api.core.event_bus import event_bus
from api.core.job_stats import job_stats
from api.core.signals import project_changed, wireguard_peer_changed, wireguard_settings_changed
from api.core.workers import WorkerName
from api.db.redis import WIREGUARD_STATUS_INTERVAL, RedisClient
from api.db.session import SessionFactory
from api.db.wireguard_repository import WireGuardPeerRepository, WireGuardSettingsRepository
from api.models.wireguard import (
    WireGuardPeer,
    WireGuardPeerStatus,
    WireGuardServerSettings,
    WireGuardState,
    WireGuardStatus,
)
from api.wireguard import keys
from api.wireguard.config import render_nft_ruleset
from api.wireguard.dns import DnsServer, FakeIpDirectory, FakeIpPool, FakeIpResolver
from api.wireguard.peers import PeerDirectory
from api.wireguard.system import CommandError, CommandRunner, Netfilter, WireGuardInterface, explain
from api.wireguard.transparent import TransparentProxyServer

if TYPE_CHECKING:
    from api.core.mitm import MitmHandler
    from api.core.proxy_manager import ProxyManager
    from api.geo.verifier import ExitVerifier

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
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self._config = settings
        self._session_factory = session_factory
        self._redis = redis_client
        self.settings_store = WireGuardSettingsStore(settings, session_factory)
        self.peers = PeerDirectory(session_factory)
        self.pool = FakeIpPool(settings.wireguard_fake_ip_range)
        # Shared through Redis so every instance carrying the tunnel agrees on the mapping.
        self.fake_ips = FakeIpDirectory(self.pool, redis_client)
        self.interface = WireGuardInterface(settings.wireguard_interface, runner)
        self.netfilter = Netfilter(runner=runner)
        self.dns_server: DnsServer | None = None
        self.transparent_server: TransparentProxyServer | None = None
        self.enabled = settings.wireguard_enabled
        self.state: WireGuardState = "disabled"
        self.error: str | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._sync_lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------------------

    async def start(
        self,
        proxy_manager: ProxyManager,
        *,
        mitm_handler: MitmHandler | None = None,
        exit_verifier: ExitVerifier | None = None,
    ) -> None:
        """Load the row and the peers; bring the tunnel up when this instance is the endpoint."""
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
            await self._bring_up(proxy_manager, mitm_handler, exit_verifier)
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

    async def _bring_up(
        self,
        proxy_manager: ProxyManager,
        mitm_handler: MitmHandler | None,
        exit_verifier: ExitVerifier | None,
    ) -> None:
        server = self.settings_store.settings
        gateway = server.gateway
        await self.interface.up(
            private_key=server.private_key,
            listen_port=self.listen_port or server.endpoint_port,
            address=f"{gateway}/{server.network.prefixlen}",
            mtu=self._config.wireguard_mtu,
            peers=self.peers.all(),
        )
        self.dns_server = DnsServer(FakeIpResolver(self.fake_ips), gateway, self._config.wireguard_dns_port)
        await self.dns_server.start()
        self.transparent_server = TransparentProxyServer(
            proxy_manager,
            self.peers,
            self.fake_ips,
            host=gateway,
            port=self._config.wireguard_transparent_port,
            mitm_handler=mitm_handler,
            exit_verifier=exit_verifier,
            sniff_timeout=self._config.wireguard_sniff_timeout_seconds,
            block_encrypted_dns=self._config.wireguard_block_encrypted_dns,
        )
        await self.transparent_server.start()
        await self.netfilter.apply(
            render_nft_ruleset(
                self.netfilter.table,
                self.interface.name,
                self.transparent_server.port,
                self.dns_server.port,
            )
        )
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
            await self.netfilter.remove()
        if self.transparent_server is not None:
            with contextlib.suppress(Exception):
                await self.transparent_server.stop()
            self.transparent_server = None
        if self.dns_server is not None:
            with contextlib.suppress(Exception):
                await self.dns_server.stop()
            self.dns_server = None
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
        job_stats.declare_interval(WorkerName.WIREGUARD_STATUS_PUBLISHER, WIREGUARD_STATUS_INTERVAL)
        while True:
            try:
                with job_stats.track(WorkerName.WIREGUARD_STATUS_PUBLISHER) as run:
                    if not await self._publish_status():
                        run.idle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("WireGuard status publish failed", error=str(exc))
            await asyncio.sleep(WIREGUARD_STATUS_INTERVAL)

    async def _publish_status(self) -> bool:
        """Push the peer counters to Redis; False when there was nothing to publish."""
        if self._redis is None:
            return False
        peer_ids = {p.public_key: p.id for p in self.peers.all()}
        server = self.transparent_server
        statuses = {}
        for dump in await self.interface.dump():
            peer_id = peer_ids.get(dump.public_key, "")
            statuses[dump.public_key] = json.dumps(
                {
                    "latest_handshake": dump.latest_handshake,
                    "rx_bytes": dump.rx_bytes,
                    "tx_bytes": dump.tx_bytes,
                    "endpoint": dump.endpoint,
                    "connections_by_address": server.by_address.get(peer_id, 0) if server else 0,
                    "encrypted_dns_blocked": server.encrypted_dns_blocked.get(peer_id, 0) if server else 0,
                }
            )
        await self._redis.set_wireguard_peer_status(self._config.instance_id, statuses)
        return bool(statuses)

    async def peer_statuses(self) -> dict[str, WireGuardPeerStatus]:
        """Live state per public key, merged across every instance carrying the tunnel.

        Behind a UDP load balancer several instances carry sessions at once
        and each publishes what its interface knows. A peer appears in every
        carrier's dump (they all configure every peer), so the reading that
        counts is the one with the latest handshake: that is where the device's
        traffic currently lands.
        """
        if self._redis is None:
            return {}
        try:
            published = await self._redis.get_wireguard_peer_status()
        except Exception as exc:
            logger.debug("Peer status unavailable", error=str(exc))
            return {}
        now = time.time()
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
                statuses[public_key] = WireGuardPeerStatus(
                    online=handshake > 0 and now - handshake <= ONLINE_WINDOW_SECONDS,
                    # Naive UTC, like every other timestamp the API serialises.
                    last_handshake_at=datetime.fromtimestamp(handshake, UTC).replace(tzinfo=None) if handshake else None,
                    rx_bytes=int(data.get("rx_bytes") or 0),
                    tx_bytes=int(data.get("tx_bytes") or 0),
                    endpoint=data.get("endpoint"),
                    connections_by_address=int(data.get("connections_by_address") or 0),
                    encrypted_dns_blocked=int(data.get("encrypted_dns_blocked") or 0),
                )
        return statuses

    async def status(self) -> WireGuardStatus:
        statuses = await self.peer_statuses()
        return WireGuardStatus(
            enabled=self.enabled,
            state=self.state,
            error=self.error,
            instance_id=self._config.instance_id,
            interface=self.interface.name,
            backend=self.interface.backend,
            listen_port=(self.listen_port or self.settings_store.settings.endpoint_port) if self.enabled else None,
            transparent_port=self._config.wireguard_transparent_port,
            dns_port=self._config.wireguard_dns_port,
            fake_ip_range=str(self.pool.network),
            active_connections=self.transparent_server.active_connections if self.transparent_server else 0,
            peers_total=len(self.peers),
            peers_enabled=self.peers.enabled_count,
            peers_online=sum(1 for s in statuses.values() if s.online),
            connections_by_address=sum(self.transparent_server.by_address.values()) if self.transparent_server else 0,
            encrypted_dns_blocked=sum(self.transparent_server.encrypted_dns_blocked.values()) if self.transparent_server else 0,
            block_encrypted_dns=self._config.wireguard_block_encrypted_dns,
        )
