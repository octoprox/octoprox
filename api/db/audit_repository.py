# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Repositories for the audit log's rows and the install's settings row, in the caller's session; the caller commits."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Row, delete, literal, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from api.db.models import AuditLogModel, SystemSettingsModel
from api.models.audit import AuditEntry, AuditHistory
from api.models.system import SystemSettings

# An entry's JSON columns, dumped as JSON values (a time in the details as its ISO text).
_JSON_FIELDS = frozenset({"changes", "details"})


class AuditRepository:
    """The ``audit_log`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, entries: Sequence[AuditEntry]) -> None:
        """Insert ``entries``: one statement executed with each row's values, the same text however many there are."""
        if not entries:
            return
        rows = [
            {**entry.model_dump(exclude=set(_JSON_FIELDS)), **entry.model_dump(mode="json", include=set(_JSON_FIELDS))}
            for entry in entries
        ]
        # NULLs rendered, or the ORM's bulk insert leaves a row's None columns out: a statement per set of them.
        await self._session.execute(pg_insert(AuditLogModel).execution_options(render_nulls=True), rows)

    async def history(
        self, entity_type: str, entity_id: str, limit: int, before: str | None = None
    ) -> AuditHistory:
        """One entity's entries, newest first: ``limit`` of them, older than the entry ``before`` names when given.

        The cursor is the entry's ``(created_at, id)``, so entries recorded while someone pages never shift a page.
        """
        query = (
            select(AuditLogModel)
            .where(AuditLogModel.entity_type == entity_type, AuditLogModel.entity_id == entity_id)
            .order_by(AuditLogModel.created_at.desc(), AuditLogModel.id.desc())
            .limit(limit + 1)
        )
        if before:
            # An entry retention removed is older than every entry left: nothing comes after it.
            anchor = aliased(AuditLogModel)
            anchor_time = select(anchor.created_at).where(anchor.id == before).scalar_subquery()
            query = query.where(tuple_(AuditLogModel.created_at, AuditLogModel.id) < tuple_(anchor_time, literal(before)))
        models = list((await self._session.execute(query)).scalars())
        return AuditHistory(entries=[self._to_domain(m) for m in models[:limit]], more=len(models) > limit)

    async def delete_older_than(self, cutoff: datetime) -> int:
        """Delete the entries made before ``cutoff``; how many went."""
        result = await self._session.execute(delete(AuditLogModel).where(AuditLogModel.created_at < cutoff))
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    @staticmethod
    def _to_domain(model: AuditLogModel) -> AuditEntry:
        return AuditEntry(
            id=model.id,
            created_at=model.created_at,
            action=model.action,
            entity_type=model.entity_type,
            entity_id=model.entity_id,
            entity_name=model.entity_name,
            project_id=model.project_id,
            actor_id=model.actor_id,
            actor=model.actor,
            request_id=model.request_id,
            changes=dict(model.changes or {}),
            details=dict(model.details or {}),
        )


# The columns of ``system_settings`` the domain model carries, read as plain values so no loaded row goes stale.
_SETTINGS_COLUMNS = (SystemSettingsModel.audit_retention_days, SystemSettingsModel.license_key)


class SystemSettingsRepository:
    """The single ``system_settings`` row (see ``SystemSettingsModel``)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> SystemSettings:
        """The settings as stored, or the defaults while no admin has saved any."""
        row = (await self._session.execute(select(*_SETTINGS_COLUMNS).where(SystemSettingsModel.id == 1))).one_or_none()
        return SystemSettings() if row is None else self._to_domain(row)

    async def set_audit_retention(self, days: int) -> SystemSettings:
        """Store how many days audit entries are kept, the license key left as it is; the settings as stored now."""
        return await self._upsert(audit_retention_days=days)

    async def set_license_key(self, key: str | None) -> SystemSettings:
        """Store the license key, the retention left as it is; the settings as stored now."""
        return await self._upsert(license_key=key)

    async def _upsert(self, **values: Any) -> SystemSettings:
        """Write ``values`` alone into the row, creating it with the defaults for the rest when there is none."""
        statement = pg_insert(SystemSettingsModel).values(id=1, **values)
        statement = statement.on_conflict_do_update(index_elements=[SystemSettingsModel.id], set_=values)
        row = (await self._session.execute(statement.returning(*_SETTINGS_COLUMNS))).one()
        return self._to_domain(row)

    @staticmethod
    def _to_domain(row: Row[int, str | None]) -> SystemSettings:
        return SystemSettings(audit_retention_days=row.audit_retention_days, license_key=row.license_key)
