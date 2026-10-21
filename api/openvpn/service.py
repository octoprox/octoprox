# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The api's OpenVPN: the install's CA and settings and the devices in Postgres, and how they stand.

A device's certificate is issued here with the CA key, which only Postgres roles hold.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog
from sqlalchemy.exc import IntegrityError

from api.core.stats import TunnelPeerMetricDelta
from api.db.openvpn_repository import OpenVpnPeerRepository, OpenVpnSettingsRepository
from api.db.streams import ChangeKind
from api.db.wireguard_repository import WireGuardSettingsRepository
from api.models.location import LOCATION_NEEDS_COUNTRY
from api.models.openvpn import (
    OpenVpnPeer,
    OpenVpnPeerConfigResponse,
    OpenVpnPeerCreate,
    OpenVpnPeerListResponse,
    OpenVpnPeerResponse,
    OpenVpnPeerStatus,
    OpenVpnPeerUpdate,
    OpenVpnServerSettings,
    OpenVpnServerSettingsDoc,
    OpenVpnServerSettingsResponse,
    OpenVpnSessionReading,
)
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
from api.openvpn import pki
from api.openvpn.config import client_profile_filename, render_client_profile

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog

logger = structlog.get_logger()

# The protocol's name in the Redis status keys and in endpoint states.
PROTOCOL: TunnelProtocol = "openvpn"


def new_identity() -> dict[str, Any]:
    """A fresh CA, a server certificate it signed and a tls-crypt key, as the settings row's fields."""
    ca_cert, ca_key = pki.generate_ca()
    server_cert, server_key = pki.issue_server_certificate(ca_cert, ca_key)
    return {
        "ca_cert": ca_cert,
        "ca_key": ca_key,
        "server_cert": server_cert,
        "server_key": server_key,
        "tls_crypt_key": pki.generate_tls_crypt_key(),
    }


def defaults_from_config(defaults: Mapping[str, Any]) -> OpenVpnServerSettings:
    """The row a fresh install starts with: a new identity plus ``openvpn.defaults``.

    Invalid defaults are logged and the built-in settings used instead, so a
    typo in the config file never keeps the install from starting.
    """
    identity = new_identity()
    try:
        return OpenVpnServerSettings(**identity, **dict(defaults))
    except (ValueError, TypeError) as exc:
        logger.error("Invalid openvpn.defaults in config, using built-in settings", error=str(exc))
        return OpenVpnServerSettings(**identity)


async def seed_openvpn_settings(
    session: AsyncSession, defaults: Mapping[str, Any]
) -> tuple[OpenVpnServerSettings, bool]:
    """The settings row, written from ``defaults`` first when the install has none, and whether this call wrote it.

    The caller commits and announces; of two processes seeding at once, the insert that commits first wins.
    """
    repo = OpenVpnSettingsRepository(session)
    stored = await repo.get()
    if stored is not None:
        return stored, False
    seeded = defaults_from_config(defaults)
    await repo.create_if_absent(seeded)
    stored = await repo.get()
    assert stored is not None
    created = stored.ca_cert == seeded.ca_cert
    if created:
        logger.info("OpenVPN settings created")
    return stored, created


def _certificate(server: OpenVpnServerSettings, peer_id: str) -> dict[str, Any]:
    """A new certificate and key for the device from the install's CA, with what its row keeps of them.

    The common name is the device id; the serial is what tells this
    certificate from a rotated one when the device connects.
    """
    certificate, private_key = pki.issue_client_certificate(server.ca_cert, server.ca_key, peer_id)
    return {
        "certificate": certificate,
        "private_key": private_key,
        "serial": pki.serial_of(certificate),
        "certificate_expires_at": pki.not_after_of(certificate),
    }


def _conflict(exc: IntegrityError, name: str) -> Exception:
    """What a unique index a device write ran into means for the admin; the error itself for anything else."""
    text = str(exc.orig or exc)
    if "ix_openvpn_peers_project_name_unique" in text:
        return TunnelValidationError(f"A device named '{name}' already exists in this project")
    if "address" in text:
        return TunnelAddressError("The tunnel address was taken concurrently; retry")
    return exc


class OpenVpnService:
    """OpenVPN settings, CA and devices for the admin API, over Postgres; gateways follow through the feed.

    Args:
        defaults: ``openvpn.defaults``, for a settings row created when a read finds none.
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

    async def settings(self) -> OpenVpnServerSettings:
        """The install's settings row, created when missing.

        The CA key is included, for issuing; it is never shown or fed to gateways.
        """
        async with self._session_factory() as session:
            server, created = await seed_openvpn_settings(session, self._defaults)
            await session.commit()
        await self._announce_seed(created)
        return server

    async def save_settings(self, doc: OpenVpnServerSettingsDoc, updated_by: str | None) -> OpenVpnServerSettings:
        """Replace the editable settings; TunnelValidationError when the subnet would not hold the devices.

        The subnet must hold every device's address off its gateway and share no address with the WireGuard one;
        gateways restart their daemons when what a daemon runs with changed.
        """
        async with self._session_factory() as session:
            current, _ = await seed_openvpn_settings(session, self._defaults)
            updated = current.model_copy(update=doc.model_dump())
            await self._check_subnet(session, updated)
            saved = await self._save(session, updated, updated_by)
            await session.commit()
        logger.info("OpenVPN settings saved", updated_by=updated_by, endpoint=saved.endpoint_host)
        await self._changes.record(ChangeKind.OPENVPN)
        return saved

    async def rotate_identity(self, updated_by: str | None) -> OpenVpnServerSettings:
        """A new CA, server certificate and tls-crypt key, and every device reissued under them; every profile must be loaded again.

        The row and every device change in one transaction, so no gateway is ever handed a device whose certificate the
        CA did not sign.
        """
        async with self._session_factory() as session:
            current, _ = await seed_openvpn_settings(session, self._defaults)
            updated = current.model_copy(update=new_identity())
            saved = await self._save(session, updated, updated_by)
            repo = OpenVpnPeerRepository(session)
            # In id order, the order every batch writer of a feed table keeps.
            devices = sorted(await repo.get_all(), key=lambda device: device.id)
            for device in devices:
                await repo.update(device.model_copy(update=_certificate(updated, device.id)))
            await session.commit()
        logger.warning(
            "OpenVPN identity rotated; every device profile is now invalid", by=updated_by, devices=len(devices)
        )
        await self._changes.record(ChangeKind.OPENVPN)
        return saved

    async def _save(
        self, session: AsyncSession, settings: OpenVpnServerSettings, updated_by: str | None
    ) -> OpenVpnServerSettings:
        """Write the row and read it back, with the time it was written."""
        repo = OpenVpnSettingsRepository(session)
        await repo.save(settings, updated_by=updated_by)
        saved = await repo.get()
        assert saved is not None
        return saved

    async def _announce_seed(self, created: bool) -> None:
        """Announce the settings row a read just created (a backup restored without one); nothing when it was there."""
        if created:
            await self._changes.record(ChangeKind.OPENVPN)

    async def _check_subnet(self, session: AsyncSession, settings: OpenVpnServerSettings) -> None:
        """TunnelValidationError unless the subnet clears the WireGuard one and still holds every device."""
        network, gateway = settings.network, settings.gateway
        other = await WireGuardSettingsRepository(session).get()
        if other is not None and network.overlaps(other.network):
            raise TunnelValidationError(f"subnet {settings.subnet} overlaps the WireGuard subnet {other.network}")
        used = await OpenVpnPeerRepository(session).used_addresses()
        outside = [a for a in used if ipaddress.IPv4Address(a) not in network or a == gateway]
        if outside:
            raise TunnelValidationError(
                f"{len(outside)} device(s) have addresses outside {settings.subnet} "
                f"(or on its gateway {gateway}); remove them first"
            )

    # --- devices -----------------------------------------------------------------------

    async def peers(self, project_id: str) -> list[OpenVpnPeer]:
        """A project's devices, oldest first."""
        async with self._session_factory() as session:
            return await OpenVpnPeerRepository(session).get_by_project(project_id)

    async def peer(self, project_id: str, peer_id: str) -> OpenVpnPeer | None:
        """The device, or None when it does not exist or belongs to another project."""
        async with self._session_factory() as session:
            return await OpenVpnPeerRepository(session).get_in_project(project_id, peer_id)

    async def add_peer(self, project_id: str, data: OpenVpnPeerCreate) -> OpenVpnPeer:
        """Register a device: a certificate from the install's CA and the lowest free tunnel address.

        Raises TunnelValidationError for a name in use and TunnelAddressError
        when no address is free or a device created at the same moment took
        this one.
        """
        peer_id = str(uuid4())
        async with self._session_factory() as session:
            server, _ = await seed_openvpn_settings(session, self._defaults)
            repo = OpenVpnPeerRepository(session)
            peer = OpenVpnPeer(
                id=peer_id,
                project_id=project_id,
                name=data.name,
                address=first_free_address(server.network, await repo.used_addresses()),
                enabled=data.enabled,
                session_id=data.session_id,
                country=data.country,
                state=data.state,
                city=data.city,
                **_certificate(server, peer_id),
            )
            try:
                await repo.create(peer)
                await session.commit()
            except IntegrityError as exc:
                raise _conflict(exc, peer.name) from None
        logger.info("OpenVPN peer added", peer_id=peer.id, name=peer.name, project_id=project_id)
        await self._changes.record(ChangeKind.OPENVPN, peer.id)
        return peer

    async def update_peer(self, project_id: str, peer_id: str, data: OpenVpnPeerUpdate) -> OpenVpnPeer | None:
        """Apply a partial update, a disabled device's session ending on every gateway as the feed reaches it.

        None when the device is not the project's; TunnelValidationError for a name in use or a state or city without a
        country.
        """
        async with self._session_factory() as session:
            repo = OpenVpnPeerRepository(session)
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
        await self._changes.record(ChangeKind.OPENVPN, peer_id)
        return updated

    async def rotate_certificate(self, project_id: str, peer_id: str) -> OpenVpnPeer | None:
        """A new certificate and key for the device, its session ending on every gateway as the feed carries the serial.

        None when the device is not the project's; the profile it has stops working.
        """
        async with self._session_factory() as session:
            repo = OpenVpnPeerRepository(session)
            peer = await repo.get_in_project(project_id, peer_id)
            if peer is None:
                return None
            server, _ = await seed_openvpn_settings(session, self._defaults)
            updated = peer.model_copy(update=_certificate(server, peer.id))
            await repo.update(updated)
            await session.commit()
        await self._changes.record(ChangeKind.OPENVPN, peer_id)
        return updated

    async def remove_peer(self, project_id: str, peer_id: str) -> bool:
        """Delete the device with its metrics history; False when it is not the project's."""
        async with self._session_factory() as session:
            repo = OpenVpnPeerRepository(session)
            if await repo.get_in_project(project_id, peer_id) is None:
                return False
            await repo.delete(peer_id)
            await session.commit()
        logger.info("OpenVPN peer removed", peer_id=peer_id, project_id=project_id)
        await self._changes.record(ChangeKind.OPENVPN, peer_id)
        # The device's history went with its row; its unflushed window would
        # otherwise be flushed against a device that is gone (and dropped there).
        await self._redis.after_commit(
            lambda: self._redis.delete_tunnel_peer_window(peer_id), "drop a deleted device's metric window", peer_id=peer_id
        )
        return True

    async def client_profile(self, project_id: str, peer_id: str) -> OpenVpnPeerConfigResponse | None:
        """The device's ``.ovpn`` profile (also before the endpoint is set, flagged); None when not the project's."""
        async with self._session_factory() as session:
            peer = await OpenVpnPeerRepository(session).get_in_project(project_id, peer_id)
            if peer is None:
                return None
            server, created = await seed_openvpn_settings(session, self._defaults)
            await session.commit()
        await self._announce_seed(created)
        return OpenVpnPeerConfigResponse(
            filename=client_profile_filename(peer.name),
            config=render_client_profile(peer, server),
            complete=server.configured,
            server_configured=server.configured,
        )

    # --- how the devices stand ---------------------------------------------------------

    async def peer_statuses(self) -> dict[str, OpenVpnPeerStatus]:
        """Live sessions per device id, merged across every gateway running the endpoint.

        Empty when no gateway runs it or Redis cannot be reached;
        ``peer_status`` falls back to the last connection on record then.
        """
        return {
            peer_id: OpenVpnPeerStatus(
                online=True,
                connected_since=reading.connected_at,
                last_seen_at=reading.connected_at,
                endpoint=reading.endpoint,
                live=True,
                rx_bytes=reading.rx_bytes,
                tx_bytes=reading.tx_bytes,
            )
            for peer_id, reading in OpenVpnSessionReading.latest(await self._published()).items()
        }

    @staticmethod
    def peer_status(peer: OpenVpnPeer, live: OpenVpnPeerStatus | None) -> OpenVpnPeerStatus:
        """Where a device stands: the live session, or the last connection on record (the ``tunnel_last_seen`` job's)."""
        if live is not None:
            return live
        return OpenVpnPeerStatus(
            online=False,
            connected_since=None,
            last_seen_at=peer.last_connected_at,
            endpoint=peer.last_endpoint if peer.last_connected_at is not None else None,
            live=False,
        )

    async def responses(self, peers: Sequence[OpenVpnPeer]) -> list[OpenVpnPeerResponse]:
        """The devices as the admin API shows them: each with where it stands and its traffic totals."""
        live = await self.peer_statuses()
        totals = await self._device_totals([p.id for p in peers])
        return [
            OpenVpnPeerResponse(
                id=peer.id,
                project_id=peer.project_id,
                name=peer.name,
                serial=peer.serial,
                certificate_expires_at=peer.certificate_expires_at,
                address=peer.address,
                enabled=peer.enabled,
                session_id=peer.session_id,
                country=peer.country,
                state=peer.state,
                city=peer.city,
                created_at=peer.created_at,
                updated_at=peer.updated_at,
                status=self.peer_status(peer, live.get(peer.id)),
                metrics=TunnelPeerMetrics.from_delta(totals.get(peer.id)),
            )
            for peer in peers
        ]

    async def response(self, peer: OpenVpnPeer) -> OpenVpnPeerResponse:
        """One device as the admin API shows it."""
        return (await self.responses([peer]))[0]

    async def peer_list(self, project_id: str) -> OpenVpnPeerListResponse:
        """A project's devices as the admin API lists them, with whether profiles can be completed yet."""
        async with self._session_factory() as session:
            peers = await OpenVpnPeerRepository(session).get_by_project(project_id)
            server, created = await seed_openvpn_settings(session, self._defaults)
            await session.commit()
        await self._announce_seed(created)
        return OpenVpnPeerListResponse(
            total=len(peers),
            peers=await self.responses(peers),
            server_configured=server.configured,
            ca_fingerprint=pki.fingerprint_of(server.ca_cert),
        )

    async def settings_response(self) -> OpenVpnServerSettingsResponse:
        """The settings as the admin API shows them (the CA by fingerprint), with the endpoint's status."""
        server = await self.settings()
        return OpenVpnServerSettingsResponse(
            ca_fingerprint=pki.fingerprint_of(server.ca_cert),
            ca_expires_at=pki.not_after_of(server.ca_cert),
            endpoint_host=server.endpoint_host,
            endpoint_port=server.endpoint_port,
            protocol=server.protocol,
            subnet=server.subnet,
            gateway=server.gateway,
            keepalive_interval=server.keepalive_interval,
            keepalive_timeout=server.keepalive_timeout,
            client_mtu=server.client_mtu,
            configured=server.configured,
            updated_at=server.updated_at,
            status=await self.status(),
        )

    async def status(self) -> TunnelStatus:
        """The endpoint across the install: every gateway running it, and the devices.

        The degradation signals are summed over every device, all time, from
        the same totals the devices show.
        """
        async with self._session_factory() as session:
            peers = await OpenVpnPeerRepository(session).get_all()
        live = await self.peer_statuses()
        totals = TunnelPeerMetricDelta.summed(*(await self._device_totals([p.id for p in peers])).values())
        return TunnelStatus(
            gateways=await self._gateways(),
            peers_total=len(peers),
            peers_enabled=sum(1 for p in peers if p.enabled),
            peers_online=sum(1 for p in peers if p.id in live),
            connections_by_address=totals.by_address,
            encrypted_dns_blocked=totals.encrypted_dns_blocked,
        )

    async def _published(self) -> list[dict[str, str]]:
        """What every gateway running the endpoint last published; [] when Redis cannot be reached."""
        try:
            return await self._redis.get_tunnel_peer_status(PROTOCOL)
        except Exception as exc:
            logger.debug("Peer status unavailable", error=str(exc))
            return []

    async def _gateways(self) -> list[TunnelEndpointState]:
        """The OpenVPN endpoints the live gateways report, by instance; [] when the heartbeats cannot be read."""
        try:
            states = await self._endpoint_states()
        except Exception as exc:
            logger.debug("Gateway endpoint states unavailable", error=str(exc))
            return []
        return sorted((s for s in states if s.protocol == PROTOCOL), key=lambda s: s.instance_id)
