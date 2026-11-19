# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A project's alert cut-offs: read with the defaults beside them, and replaced by its editors, each change recorded in
the project's History and checked at once.

They are written on the project apart from what the routing feed compiles, with ``updated_at`` kept, so a change
compiles nothing and no gateway ever sees one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from api.db.repository import ProjectRepository
from api.models.alerts import AlertSettings, AlertSettingsResponse, ProjectAlertSettings
from api.services.errors import NotFoundError

if TYPE_CHECKING:
    from api.attention.requests import AlertChecks
    from api.core.audit import Actor
    from api.db.session import SessionFactory

logger = structlog.get_logger()


class AlertSettingsService:
    """Reads and replaces a project's alert cut-offs."""

    def __init__(self, session_factory: SessionFactory, checks: AlertChecks) -> None:
        self._session_factory = session_factory
        self._checks = checks

    async def get(self, project_id: str) -> AlertSettingsResponse:
        """The project's settings in force and the defaults; raises NotFoundError when there is no such project."""
        async with self._session_factory() as session:
            settings = (await ProjectRepository(session).alert_settings([project_id])).get(project_id)
        if settings is None:
            raise NotFoundError("Project not found")
        return AlertSettingsResponse(settings=settings, defaults=AlertSettings())

    async def update(self, project_id: str, body: AlertSettings, actor: Actor) -> AlertSettingsResponse:
        """Replace the project's overrides with ``body``'s, record what changed, and ask for a check now; the settings in
        force after. Raises NotFoundError when there is no such project."""
        settings = AlertSettings.model_validate(body.model_dump())
        async with self._session_factory() as session:
            repository = ProjectRepository(session)
            before = await repository.lock_alert_settings(project_id)
            project = await repository.get_by_id(project_id) if before is not None else None
            if before is None or project is None:
                raise NotFoundError("Project not found")
            await repository.write_alert_settings(project_id, settings)
            await actor.updated(
                session,
                ProjectAlertSettings(id=project.id, name=project.name, alerts=before.model_dump(mode="json")),
                ProjectAlertSettings(id=project.id, name=project.name, alerts=settings.model_dump(mode="json")),
            )
            await session.commit()
        logger.info("Alert settings saved", project_id=project_id, overrides=sorted(settings.stored()))
        await self._checks.request(project_id)
        return AlertSettingsResponse(settings=settings, defaults=AlertSettings())
