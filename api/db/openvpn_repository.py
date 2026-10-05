# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Repositories for OpenVPN: the single settings row and the peers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, column, delete, or_, select, update, values
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.db.models import OpenVpnPeerModel, OpenVpnSettingsModel, TunnelPeerMetricsModel
from api.models.openvpn import OpenVpnPeer, OpenVpnServerSettings

# One sighting: (peer id, when the session started, the address it came from).
Sighting = tuple[str, datetime, str | None]


class OpenVpnSettingsRepository:
    """The single ``openvpn_settings`` row (see ``OpenVpnSettingsModel``)."""

    _COLUMNS = (
        "ca_cert",
        "ca_key",
        "server_cert",
        "server_key",
        "tls_crypt_key",
        "endpoint_host",
        "endpoint_port",
        "protocol",
        "subnet",
        "keepalive_interval",
        "keepalive_timeout",
        "client_mtu",
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> OpenVpnServerSettings | None:
        result = await self._session.execute(select(OpenVpnSettingsModel).where(OpenVpnSettingsModel.id == 1))
        model = result.scalar_one_or_none()
        if model is None:
            return None
        values: dict[str, Any] = {name: getattr(model, name) for name in self._COLUMNS}
        return OpenVpnServerSettings(**values, updated_at=model.updated_at)

    async def create_if_absent(self, settings: OpenVpnServerSettings) -> None:
        """Seed the row on a fresh install; an instance that got there first wins."""
        values: dict[str, Any] = {name: getattr(settings, name) for name in self._COLUMNS}
        values["updated_at"] = utc_now()
        statement = pg_insert(OpenVpnSettingsModel).values(id=1, **values)
        await self._session.execute(statement.on_conflict_do_nothing(index_elements=[OpenVpnSettingsModel.id]))
        await self._session.flush()

    async def save(self, settings: OpenVpnServerSettings, updated_by: str | None = None) -> None:
        values: dict[str, Any] = {name: getattr(settings, name) for name in self._COLUMNS}
        values["updated_by"] = updated_by
        values["updated_at"] = utc_now()
        statement = pg_insert(OpenVpnSettingsModel).values(id=1, **values)
        statement = statement.on_conflict_do_update(index_elements=[OpenVpnSettingsModel.id], set_=values)
        await self._session.execute(statement)
        await self._session.flush()


class OpenVpnPeerRepository:
    """Peers, ordered by creation time."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[OpenVpnPeer]:
        result = await self._session.execute(
            select(OpenVpnPeerModel).order_by(OpenVpnPeerModel.created_at, OpenVpnPeerModel.id)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def get_by_id(self, peer_id: str) -> OpenVpnPeer | None:
        result = await self._session.execute(select(OpenVpnPeerModel).where(OpenVpnPeerModel.id == peer_id))
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def create(self, peer: OpenVpnPeer) -> OpenVpnPeer:
        self._session.add(
            OpenVpnPeerModel(
                id=peer.id,
                project_id=peer.project_id,
                name=peer.name,
                certificate=peer.certificate,
                private_key=peer.private_key,
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
            )
        )
        await self._session.flush()
        return peer

    async def update(self, peer: OpenVpnPeer) -> OpenVpnPeer:
        result = await self._session.execute(select(OpenVpnPeerModel).where(OpenVpnPeerModel.id == peer.id))
        model = result.scalar_one_or_none()
        if model:
            model.name = peer.name
            model.certificate = peer.certificate
            model.private_key = peer.private_key
            model.serial = peer.serial
            model.certificate_expires_at = peer.certificate_expires_at
            model.address = peer.address
            model.enabled = peer.enabled
            model.session_id = peer.session_id
            model.country = peer.country
            model.state = peer.state
            model.city = peer.city
            model.version += 1
            model.updated_at = utc_now()
            peer.updated_at = model.updated_at
            await self._session.flush()
        return peer

    async def delete(self, peer_id: str) -> bool:
        """Delete the device and its metrics history (no foreign key links the two, see the model)."""
        result = await self._session.execute(delete(OpenVpnPeerModel).where(OpenVpnPeerModel.id == peer_id))
        await self._session.execute(delete(TunnelPeerMetricsModel).where(TunnelPeerMetricsModel.peer_id == peer_id))
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    async def record_last_seen(self, sightings: Sequence[Sighting]) -> int:
        """Advance devices' last connections in one statement; the number of rows that moved.

        Same contract as the WireGuard repository: the column only ever moves
        forward, ``version`` is not bumped and ``updated_at`` is pinned (a
        sighting is not an edit).
        """
        if not sightings:
            return 0
        seen = values(
            column("id", String), column("connected_at", DateTime), column("endpoint", String), name="seen"
        ).data(list(sightings))
        result = await self._session.execute(
            update(OpenVpnPeerModel)
            .where(OpenVpnPeerModel.id == seen.c.id)
            .where(
                or_(
                    OpenVpnPeerModel.last_connected_at.is_(None),
                    OpenVpnPeerModel.last_connected_at < seen.c.connected_at,
                )
            )
            .values(
                last_connected_at=seen.c.connected_at,
                last_endpoint=seen.c.endpoint,
                updated_at=OpenVpnPeerModel.updated_at,
            )
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    @staticmethod
    def _to_domain(model: OpenVpnPeerModel) -> OpenVpnPeer:
        return OpenVpnPeer(
            id=model.id,
            project_id=model.project_id,
            name=model.name,
            certificate=model.certificate,
            private_key=model.private_key,
            serial=model.serial,
            certificate_expires_at=model.certificate_expires_at,
            address=model.address,
            enabled=model.enabled,
            session_id=model.session_id,
            country=model.country,
            state=model.state,
            city=model.city,
            last_connected_at=model.last_connected_at,
            last_endpoint=model.last_endpoint,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )
