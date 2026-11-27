# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The install's settings an admin sets on their own pages: how long the install's resolved alerts and the audit log's
entries are kept, each save recorded on System settings with only the column it wrote."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from api.db.audit_repository import SystemSettingsRepository

if TYPE_CHECKING:
    from api.core.audit import Actor
    from api.db.session import SessionFactory
    from api.models.system import SystemSettings

logger = structlog.get_logger()


class SystemSettingsService:
    """Reads the install's settings, and saves how long the install's alerts and the audit log's entries are kept."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def get(self) -> SystemSettings:
        """The settings as stored, or the defaults while no admin has saved any."""
        async with self._session_factory() as session:
            return await SystemSettingsRepository(session).get()

    async def set_install_alert_retention(self, days: int, actor: Actor) -> SystemSettings:
        """Keep the install's resolved alerts ``days`` days (0 forever), recorded on System settings; the hourly
        compaction applies it."""
        async with self._session_factory() as session:
            repository = SystemSettingsRepository(session)
            before = await repository.get()
            after = await repository.set_install_alert_retention(days)
            # Only the column this save wrote: the row it returns can carry another request's save.
            changed = before.model_copy(
                update={"install_alert_retention_days": after.install_alert_retention_days}
            )
            await actor.updated(session, before, changed)
            await session.commit()
        logger.info("Install alert retention changed", days=days, admin=actor.name)
        return after

    async def set_audit_retention(self, days: int, actor: Actor) -> SystemSettings:
        """Keep audit entries ``days`` days (0 forever), itself recorded; the hourly compaction applies it."""
        async with self._session_factory() as session:
            repository = SystemSettingsRepository(session)
            before = await repository.get()
            after = await repository.set_audit_retention(days)
            # Only the column this save wrote: the row it returns can carry another request's license save.
            changed = before.model_copy(update={"audit_retention_days": after.audit_retention_days})
            await actor.updated(session, before, changed)
            await session.commit()
        logger.info("Audit retention changed", days=days, admin=actor.name)
        return after
