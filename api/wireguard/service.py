# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The api's WireGuard: the install's settings and the devices in Postgres, and how they stand.

A write reaches the gateways through the ``wireguard`` change entry it appends after its commit, and the routing feed.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.exc import IntegrityError

from api.core import utc_now
from api.core.stats import TunnelPeerMetricDelta
from api.db.openvpn_repository import OpenVpnSettingsRepository
from api.db.streams import ChangeKind
from api.db.wireguard_repository import WireGuardPeerRepository, WireGuardSettingsRepository
from api.models.location import LOCATION_NEEDS_COUNTRY
from api.models.tunnel import (
    DeviceTotals,
    EndpointStates,
    TunnelAddressError,
    TunnelEndpointState,
    TunnelPeerMetrics,
    TunnelProtocol,
    TunnelStatus,
    TunnelValidationError,
    device_changes,
    first_free_address,
)
from api.models.wireguard import (
    WireGuardPeer,
    WireGuardPeerConfigResponse,
    WireGuardPeerCreate,
    WireGuardPeerListResponse,
    WireGuardPeerReading,
    WireGuardPeerResponse,
    WireGuardPeerStatus,
    WireGuardPeerUpdate,
    WireGuardServerSettings,
    WireGuardServerSettingsDoc,
    WireGuardServerSettingsResponse,
)
from api.openvpn.service import seed_openvpn_settings
from api.wireguard import keys
from api.wireguard.config import client_conf_filename, render_client_conf

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.core.config import Settings
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog

logger = structlog.get_logger()

# The protocol's name in the Redis status keys and in endpoint states.
PROTOCOL: TunnelProtocol = "wireguard"
# A device whose last handshake is older than this is offline, in seconds:
# WireGuard re-handshakes every two minutes while traffic flows, three with
# keepalive slack.
ONLINE_WINDOW_SECONDS = 180


def defaults_from_config(defaults: Mapping[str, Any]) -> WireGuardServerSettings:
    """The row a fresh install starts with: a new key pair plus ``wireguard.defaults``.

    Invalid defaults are logged and the built-in settings used instead, so a
    typo in the config file never keeps the install from starting.
    """
    private, public = keys.generate_keypair()
    try:
        return WireGuardServerSettings(private_key=private, public_key=public, **dict(defaults))
    except (ValueError, TypeError) as exc:
        logger.error("Invalid wireguard.defaults in config, using built-in settings", error=str(exc))
        return WireGuardServerSettings(private_key=private, public_key=public)


async def seed_wireguard_settings(
    session: AsyncSession, defaults: Mapping[str, Any]
) -> tuple[WireGuardServerSettings, bool]:
    """The settings row, written from ``defaults`` first when the install has none, and whether this call wrote it.

    The caller commits and announces; of two processes seeding at once, the insert that commits first wins.
    """
    repo = WireGuardSettingsRepository(session)
    stored = await repo.get()
    if stored is not None:
        return stored, False
    seeded = defaults_from_config(defaults)
    await repo.create_if_absent(seeded)
    stored = await repo.get()
    assert stored is not None
    created = stored.private_key == seeded.private_key
    if created:
        logger.info("WireGuard settings created")
    return stored, created


async def seed_tunnel_settings(session_factory: SessionFactory, settings: Settings, changes: ChangeLog) -> None:
    """Create the WireGuard and OpenVPN settings rows when the install has none, in one transaction; the api calls it at start.

    Whichever process commits first writes and announces the rows; the others announce nothing, so a restart costs no compile.
    """
    async with session_factory() as session:
        _, wireguard = await seed_wireguard_settings(session, settings.wireguard_defaults)
        _, openvpn = await seed_openvpn_settings(session, settings.openvpn_defaults)
        await session.commit()
    if wireguard:
        await changes.record(ChangeKind.WIREGUARD)
    if openvpn:
        await changes.record(ChangeKind.OPENVPN)


def _conflict(exc: IntegrityError, name: str) -> Exception:
    """What a unique index a device write ran into means for the admin; the error itself for anything else."""
    text = str(exc.orig or exc)
    if "ix_wireguard_peers_project_name_unique" in text:
        return TunnelValidationError(f"A device named '{name}' already exists in this project")
    if "public_key" in text:
        return TunnelValidationError("A device with this public key already exists")
    if "address" in text:
        return TunnelAddressError("The tunnel address was taken concurrently; retry")
    return exc


def _within_online_window(seen: datetime, now: datetime) -> bool:
    return (now - seen).total_seconds() <= ONLINE_WINDOW_SECONDS


class WireGuardService:
    """WireGuard settings and devices for the admin API, over Postgres; gateways follow through the feed.

    Args:
        defaults: ``wireguard.defaults``, for a settings row created when a read finds none.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        redis_client: RedisClient,
        *,
        endpoint_states: EndpointStates,
        device_totals: DeviceTotals,
        defaults: Mapping[str, Any],
        changes: ChangeLog,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._endpoint_states = endpoint_states
        self._device_totals = device_totals
        self._defaults = defaults
        self._changes = changes

    # --- settings ----------------------------------------------------------------------

    async def settings(self) -> WireGuardServerSettings:
        """The install's settings row, created when missing."""
        async with self._session_factory() as session:
            server, created = await seed_wireguard_settings(session, self._defaults)
            await session.commit()
        await self._announce_seed(created)
        return server

    async def save_settings(
        self, doc: WireGuardServerSettingsDoc, updated_by: str | None
    ) -> WireGuardServerSettings:
        """Replace the editable settings, applied live by every gateway; TunnelValidationError when the subnet would not hold the devices.

        The subnet must hold every device's address, and share none with the OpenVPN subnet.
        """
        async with self._session_factory() as session:
            current, _ = await seed_wireguard_settings(session, self._defaults)
            updated = current.model_copy(update=doc.model_dump())
            await self._check_subnet(session, updated)
            saved = await self._save(session, updated, updated_by)
            await session.commit()
        logger.info("WireGuard settings saved", updated_by=updated_by, endpoint=saved.endpoint_host)
        await self._changes.record(ChangeKind.WIREGUARD)
        return saved

    async def rotate_key(self, updated_by: str | None) -> WireGuardServerSettings:
        """Give the install a new key pair. Every device must load a new config afterwards."""
        private, public = keys.generate_keypair()
        async with self._session_factory() as session:
            current, _ = await seed_wireguard_settings(session, self._defaults)
            updated = current.model_copy(update={"private_key": private, "public_key": public})
            saved = await self._save(session, updated, updated_by)
            await session.commit()
        logger.warning("WireGuard server key rotated; every device config is now invalid", by=updated_by)
        await self._changes.record(ChangeKind.WIREGUARD)
        return saved

    async def _save(
        self, session: AsyncSession, settings: WireGuardServerSettings, updated_by: str | None
    ) -> WireGuardServerSettings:
        """Write the row and read it back, with the time it was written."""
        repo = WireGuardSettingsRepository(session)
        await repo.save(settings, updated_by=updated_by)
        saved = await repo.get()
        assert saved is not None
        return saved

    async def _announce_seed(self, created: bool) -> None:
        """Announce the settings row a read just created (a backup restored without one); nothing when it was there."""
        if created:
            await self._changes.record(ChangeKind.WIREGUARD)

    async def _check_subnet(self, session: AsyncSession, settings: WireGuardServerSettings) -> None:
        """TunnelValidationError unless the subnet clears the OpenVPN one and still holds every device."""
        network, gateway = settings.network, settings.gateway
        other = await OpenVpnSettingsRepository(session).get()
        if other is not None and network.overlaps(other.network):
            raise TunnelValidationError(f"subnet {settings.subnet} overlaps the OpenVPN subnet {other.network}")
        used = await WireGuardPeerRepository(session).used_addresses()
        outside = [a for a in used if ipaddress.IPv4Address(a) not in network or a == gateway]
        if outside:
            raise TunnelValidationError(
                f"{len(outside)} device(s) have addresses outside {settings.subnet} "
                f"(or on its gateway {gateway}); remove them first"
            )

    # --- devices -----------------------------------------------------------------------

    async def peers(self, project_id: str) -> list[WireGuardPeer]:
        """A project's devices, oldest first."""
        async with self._session_factory() as session:
            return await WireGuardPeerRepository(session).get_by_project(project_id)

    async def peer(self, project_id: str, peer_id: str) -> WireGuardPeer | None:
        """The device, or None when it does not exist or belongs to another project."""
        async with self._session_factory() as session:
            return await WireGuardPeerRepository(session).get_in_project(project_id, peer_id)

    async def add_peer(self, project_id: str, data: WireGuardPeerCreate) -> WireGuardPeer:
        """Register a device at the lowest free tunnel address, with a key pair Octoprox generates and keeps when none is given.

        Raises TunnelValidationError for a name or key in use, and TunnelAddressError when no address is free or a
        device created at the same moment took this one.
        """
        private_key: str | None
        if data.public_key:
            private_key, public_key = None, data.public_key
        else:
            private_key, public_key = keys.generate_keypair()
        async with self._session_factory() as session:
            server, _ = await seed_wireguard_settings(session, self._defaults)
            repo = WireGuardPeerRepository(session)
            peer = WireGuardPeer(
                project_id=project_id,
                name=data.name,
                public_key=public_key,
                private_key=private_key,
                preshared_key=keys.generate_preshared_key() if data.preshared else None,
                address=first_free_address(server.network, await repo.used_addresses()),
                enabled=data.enabled,
                session_id=data.session_id,
                country=data.country,
                state=data.state,
                city=data.city,
            )
            try:
                await repo.create(peer)
                await session.commit()
            except IntegrityError as exc:
                raise _conflict(exc, peer.name) from None
        logger.info("WireGuard peer added", peer_id=peer.id, name=peer.name, project_id=project_id)
        await self._changes.record(ChangeKind.WIREGUARD, peer.id)
        return peer

    async def update_peer(self, project_id: str, peer_id: str, data: WireGuardPeerUpdate) -> WireGuardPeer | None:
        """Apply a partial update; None when the device is not the project's.

        Raises TunnelValidationError for a name in use or a state or city
        without a country.
        """
        async with self._session_factory() as session:
            repo = WireGuardPeerRepository(session)
            peer = await repo.get_in_project(project_id, peer_id)
            if peer is None:
                return None
            updated = peer.model_copy(update=device_changes(data))
            if (updated.state or updated.city) and not updated.country:
                raise TunnelValidationError(LOCATION_NEEDS_COUNTRY)
            try:
                await repo.update(updated)
                await session.commit()
            except IntegrityError as exc:
                raise _conflict(exc, updated.name) from None
        await self._changes.record(ChangeKind.WIREGUARD, peer_id)
        return updated

    async def rotate_peer_keys(self, project_id: str, peer_id: str) -> WireGuardPeer | None:
        """A new key pair for the device (and preshared key, if it had one); the old config stops working.

        None when the device is not the project's.
        """
        private, public = keys.generate_keypair()
        async with self._session_factory() as session:
            repo = WireGuardPeerRepository(session)
            peer = await repo.get_in_project(project_id, peer_id)
            if peer is None:
                return None
            updated = peer.model_copy(
                update={
                    "private_key": private,
                    "public_key": public,
                    "preshared_key": keys.generate_preshared_key() if peer.preshared_key else None,
                }
            )
            await repo.update(updated)
            await session.commit()
        await self._changes.record(ChangeKind.WIREGUARD, peer_id)
        return updated

    async def remove_peer(self, project_id: str, peer_id: str) -> bool:
        """Delete the device with its metrics history; False when it is not the project's."""
        async with self._session_factory() as session:
            repo = WireGuardPeerRepository(session)
            if await repo.get_in_project(project_id, peer_id) is None:
                return False
            await repo.delete(peer_id)
            await session.commit()
        logger.info("WireGuard peer removed", peer_id=peer_id, project_id=project_id)
        await self._changes.record(ChangeKind.WIREGUARD, peer_id)
        # The device's history went with its row; its unflushed window would
        # otherwise be flushed against a device that is gone (and dropped there).
        await self._redis.after_commit(
            lambda: self._redis.delete_tunnel_peer_window(peer_id), "drop a deleted device's metric window", peer_id=peer_id
        )
        return True

    async def client_config(self, project_id: str, peer_id: str) -> WireGuardPeerConfigResponse | None:
        """The device's wg-quick file, issued even before the endpoint is set (flagged); None when not the project's."""
        async with self._session_factory() as session:
            peer = await WireGuardPeerRepository(session).get_in_project(project_id, peer_id)
            if peer is None:
                return None
            server, created = await seed_wireguard_settings(session, self._defaults)
            await session.commit()
        await self._announce_seed(created)
        return WireGuardPeerConfigResponse(
            filename=client_conf_filename(peer.name),
            config=render_client_conf(peer, server),
            complete=peer.private_key is not None and server.configured,
            server_configured=server.configured,
        )

    # --- how the devices stand ---------------------------------------------------------

    async def peer_statuses(self) -> dict[str, WireGuardPeerStatus]:
        """Live readings per public key, merged across every gateway carrying the tunnel.

        Empty when no gateway carries it or Redis cannot be reached;
        ``peer_status`` falls back to the sighting on record then.
        """
        now = utc_now()
        statuses: dict[str, WireGuardPeerStatus] = {}
        for public_key, reading in WireGuardPeerReading.latest(await self._published()).items():
            seen = reading.handshake_at
            statuses[public_key] = WireGuardPeerStatus(
                online=seen is not None and _within_online_window(seen, now),
                last_handshake_at=seen,
                endpoint=reading.endpoint,
                live=True,
                rx_bytes=reading.rx_bytes,
                tx_bytes=reading.tx_bytes,
            )
        return statuses

    @staticmethod
    def peer_status(peer: WireGuardPeer, live: WireGuardPeerStatus | None) -> WireGuardPeerStatus:
        """Where a device stands: the live reading, or the sighting on record when that is all there is or newer.

        A gateway that just restarted has no handshake for a device that has not reconnected, while the row keeps its last one.
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

    async def responses(self, peers: Sequence[WireGuardPeer]) -> list[WireGuardPeerResponse]:
        """The devices as the admin API shows them: each with where it stands and its traffic totals."""
        live = await self.peer_statuses()
        totals = await self._device_totals([p.id for p in peers])
        return [
            WireGuardPeerResponse(
                id=peer.id,
                project_id=peer.project_id,
                name=peer.name,
                public_key=peer.public_key,
                has_private_key=peer.private_key is not None,
                has_preshared_key=peer.preshared_key is not None,
                address=peer.address,
                enabled=peer.enabled,
                session_id=peer.session_id,
                country=peer.country,
                state=peer.state,
                city=peer.city,
                created_at=peer.created_at,
                updated_at=peer.updated_at,
                status=self.peer_status(peer, live.get(peer.public_key)),
                metrics=TunnelPeerMetrics.from_delta(totals.get(peer.id)),
            )
            for peer in peers
        ]

    async def response(self, peer: WireGuardPeer) -> WireGuardPeerResponse:
        """One device as the admin API shows it."""
        return (await self.responses([peer]))[0]

    async def peer_list(self, project_id: str) -> WireGuardPeerListResponse:
        """A project's devices as the admin API lists them, with whether configs can be issued yet."""
        async with self._session_factory() as session:
            peers = await WireGuardPeerRepository(session).get_by_project(project_id)
            server, created = await seed_wireguard_settings(session, self._defaults)
            await session.commit()
        await self._announce_seed(created)
        return WireGuardPeerListResponse(
            total=len(peers),
            peers=await self.responses(peers),
            server_configured=server.configured,
            server_public_key=server.public_key,
        )

    async def settings_response(self) -> WireGuardServerSettingsResponse:
        """The settings as the admin API shows them, with the tunnel's status across the gateways."""
        server = await self.settings()
        return WireGuardServerSettingsResponse(
            public_key=server.public_key,
            endpoint_host=server.endpoint_host,
            endpoint_port=server.endpoint_port,
            subnet=server.subnet,
            gateway=server.gateway,
            persistent_keepalive=server.persistent_keepalive,
            client_mtu=server.client_mtu,
            configured=server.configured,
            updated_at=server.updated_at,
            status=await self.status(),
        )

    async def status(self) -> TunnelStatus:
        """The tunnel across the install: every gateway carrying it, and the devices.

        The degradation signals are summed over every device, all time, from
        the same totals the devices show.
        """
        async with self._session_factory() as session:
            peers = await WireGuardPeerRepository(session).get_all()
        live = await self.peer_statuses()
        totals = TunnelPeerMetricDelta.summed(*(await self._device_totals([p.id for p in peers])).values())
        return TunnelStatus(
            gateways=await self._gateways(),
            peers_total=len(peers),
            peers_enabled=sum(1 for p in peers if p.enabled),
            peers_online=sum(1 for p in peers if self.peer_status(p, live.get(p.public_key)).online),
            connections_by_address=totals.by_address,
            encrypted_dns_blocked=totals.encrypted_dns_blocked,
        )

    async def _published(self) -> list[dict[str, str]]:
        """What every gateway carrying the tunnel last published; [] when Redis cannot be reached."""
        try:
            return await self._redis.get_tunnel_peer_status(PROTOCOL)
        except Exception as exc:
            logger.debug("Peer status unavailable", error=str(exc))
            return []

    async def _gateways(self) -> list[TunnelEndpointState]:
        """The WireGuard endpoints the live gateways report, by instance; [] when the heartbeats cannot be read."""
        try:
            states = await self._endpoint_states()
        except Exception as exc:
            logger.debug("Gateway endpoint states unavailable", error=str(exc))
            return []
        return sorted((s for s in states if s.protocol == PROTOCOL), key=lambda s: s.instance_id)
