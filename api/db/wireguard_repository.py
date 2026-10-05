# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Repositories for WireGuard: the single settings row and the peers."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, column, delete, or_, select, update, values
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.db.models import TunnelPeerMetricsModel, WireGuardPeerModel, WireGuardSettingsModel
from api.models.wireguard import WireGuardPeer, WireGuardServerSettings

# One handshake sighting: (peer id, when, the endpoint it came from).
Sighting = tuple[str, datetime, str | None]


class WireGuardSettingsRepository:
    """The single ``wireguard_settings`` row (see ``WireGuardSettingsModel``)."""

    _COLUMNS = (
        "private_key",
        "public_key",
        "endpoint_host",
        "endpoint_port",
        "subnet",
        "persistent_keepalive",
        "client_mtu",
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> WireGuardServerSettings | None:
        result = await self._session.execute(
            select(WireGuardSettingsModel).where(WireGuardSettingsModel.id == 1)
        )
        model = result.scalar_one_or_none()
        if model is None:
            return None
        values: dict[str, Any] = {name: getattr(model, name) for name in self._COLUMNS}
        return WireGuardServerSettings(**values, updated_at=model.updated_at)

    async def create_if_absent(self, settings: WireGuardServerSettings) -> None:
        """Seed the row on a fresh install; a peer that got there first wins."""
        values: dict[str, Any] = {name: getattr(settings, name) for name in self._COLUMNS}
        values["updated_at"] = utc_now()
        statement = pg_insert(WireGuardSettingsModel).values(id=1, **values)
        await self._session.execute(statement.on_conflict_do_nothing(index_elements=[WireGuardSettingsModel.id]))
        await self._session.flush()

    async def save(self, settings: WireGuardServerSettings, updated_by: str | None = None) -> None:
        values: dict[str, Any] = {name: getattr(settings, name) for name in self._COLUMNS}
        values["updated_by"] = updated_by
        values["updated_at"] = utc_now()
        statement = pg_insert(WireGuardSettingsModel).values(id=1, **values)
        statement = statement.on_conflict_do_update(index_elements=[WireGuardSettingsModel.id], set_=values)
        await self._session.execute(statement)
        await self._session.flush()


class WireGuardPeerRepository:
    """Peers, ordered by creation time."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[WireGuardPeer]:
        result = await self._session.execute(
            select(WireGuardPeerModel).order_by(WireGuardPeerModel.created_at, WireGuardPeerModel.id)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def get_by_id(self, peer_id: str) -> WireGuardPeer | None:
        result = await self._session.execute(
            select(WireGuardPeerModel).where(WireGuardPeerModel.id == peer_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def create(self, peer: WireGuardPeer) -> WireGuardPeer:
        self._session.add(
            WireGuardPeerModel(
                id=peer.id,
                project_id=peer.project_id,
                name=peer.name,
                public_key=peer.public_key,
                private_key=peer.private_key,
                preshared_key=peer.preshared_key,
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

    async def update(self, peer: WireGuardPeer) -> WireGuardPeer:
        result = await self._session.execute(
            select(WireGuardPeerModel).where(WireGuardPeerModel.id == peer.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.name = peer.name
            model.public_key = peer.public_key
            model.private_key = peer.private_key
            model.preshared_key = peer.preshared_key
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
        result = await self._session.execute(
            delete(WireGuardPeerModel).where(WireGuardPeerModel.id == peer_id)
        )
        await self._session.execute(
            delete(TunnelPeerMetricsModel).where(TunnelPeerMetricsModel.peer_id == peer_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    async def record_last_seen(self, sightings: Sequence[Sighting]) -> int:
        """Advance devices' last handshakes in one statement; the number of rows that moved.

        One ``UPDATE ... FROM (VALUES ...)`` for the whole batch, so a
        carrier's status tick costs one round trip however many devices
        handshaked. A row holding a newer handshake, or no row at all, is
        left alone and not counted: several instances may carry a device in
        turn, and whichever saw the latest handshake wins, so the column
        only ever moves forward. Operational data: ``version`` is not
        bumped, and ``updated_at`` is pinned to its current value to keep
        its ``onupdate`` hook from firing (a sighting is not an edit).
        """
        if not sightings:
            return 0
        seen = values(
            column("id", String), column("handshake_at", DateTime), column("endpoint", String), name="seen"
        ).data(list(sightings))
        result = await self._session.execute(
            update(WireGuardPeerModel)
            .where(WireGuardPeerModel.id == seen.c.id)
            .where(
                or_(
                    WireGuardPeerModel.last_handshake_at.is_(None),
                    WireGuardPeerModel.last_handshake_at < seen.c.handshake_at,
                )
            )
            .values(
                last_handshake_at=seen.c.handshake_at,
                last_endpoint=seen.c.endpoint,
                updated_at=WireGuardPeerModel.updated_at,
            )
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    @staticmethod
    def _to_domain(model: WireGuardPeerModel) -> WireGuardPeer:
        return WireGuardPeer(
            id=model.id,
            project_id=model.project_id,
            name=model.name,
            public_key=model.public_key,
            private_key=model.private_key,
            preshared_key=model.preshared_key,
            address=model.address,
            enabled=model.enabled,
            session_id=model.session_id,
            country=model.country,
            state=model.state,
            city=model.city,
            last_handshake_at=model.last_handshake_at,
            last_endpoint=model.last_endpoint,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )
