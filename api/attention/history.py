# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Alert history: a project's alerts, or the install's, open and resolved, a page at a time and one by one."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from api.attention.mutes import MuteBook
from api.core import as_naive_utc, utc_now
from api.db.alert_repository import AlertFilters, AlertRepository
from api.db.repository import ProjectRepository
from api.models.alerts import AlertEntry, AlertPage
from api.services.errors import NotFoundError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.session import SessionFactory

# A page when the caller does not say, and the most one may ask for.
DEFAULT_ALERT_LIMIT = 50
MAX_ALERT_LIMIT = 200


class AlertHistory:
    """A project's alerts, or the install's (no project), as the Alerts pages list them."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def page(
        self,
        project_id: str | None,
        filters: AlertFilters,
        *,
        limit: int,
        before: str | None = None,
        until: datetime | None = None,
    ) -> AlertPage:
        """A page of alerts newest first, opened at or before ``until`` (now when None; a time with its zone reads as
        UTC), each open project alert with its mute; three statements for a project, two for the install.

        Raises NotFoundError when there is no such project.
        """
        now = utc_now()
        latest = as_naive_utc(until) or now
        async with self._session_factory() as session:
            rows = await AlertRepository(session).page(project_id, filters, until=latest, limit=limit, before=before)
            book = await self._book(session, project_id, now)
        return AlertPage(
            alerts=[alert.entry(book.mute_of(alert) if book is not None else None) for alert in rows.alerts],
            more=rows.more,
            open=rows.open,
            resolved=rows.resolved,
            until=latest,
            newer=rows.newer,
        )

    async def one(self, project_id: str | None, alert_id: str) -> AlertEntry:
        """One alert of the project (the install's for None), with its mute while it is open.

        Raises NotFoundError when there is no such project or alert.
        """
        now = utc_now()
        async with self._session_factory() as session:
            alert = await AlertRepository(session).one(project_id, alert_id)
            book = await self._book(session, project_id, now)
        if alert is None:
            raise NotFoundError("Alert not found")
        return alert.entry(book.mute_of(alert) if book is not None else None)

    @staticmethod
    async def _book(session: AsyncSession, project_id: str | None, now: datetime) -> MuteBook | None:
        """The project's mutes, None for the install; raises NotFoundError when there is no such project."""
        if project_id is None:
            return None
        mutes = await ProjectRepository(session).attention_mutes([project_id])
        if project_id not in mutes:
            raise NotFoundError("Project not found")
        return MuteBook(mutes[project_id], now)
