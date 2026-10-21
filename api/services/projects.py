# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Project writes: create, edit and delete, each one Postgres transaction announced with a ``project`` change entry.

An edit to how locations are judged asks for ``reattribute project:<id>``, and a delete removes the Redis keys only the
project writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy.exc import IntegrityError

from api.core.roles import JobKind
from api.db.redis import project_keys
from api.db.repository import ProjectRepository
from api.db.streams import ChangeKind
from api.jobs.attribution import ReattributeScope
from api.models.project import Project, ProjectCreate, ProjectUpdate
from api.services.errors import NameConflictError, NotFoundError, WriteRejectedError, name_conflict
from api.strategies import strategy_name

if TYPE_CHECKING:
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog
    from api.jobs.queue import JobQueue

logger = structlog.get_logger()

# The unique constraints of the projects table, as Postgres named them when
# migration 002 created the table with unique name and username columns.
_NAME_UNIQUE = "projects_name_key"
_USERNAME_UNIQUE = "projects_username_key"


class ProjectService:
    """Creates, edits and deletes projects.

    Args:
        redis_client: Deletes a deleted project's captures and failure log, under its request deadline.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        jobs: JobQueue,
        redis_client: RedisClient,
        *,
        changes: ChangeLog,
    ) -> None:
        self._session_factory = session_factory
        self._jobs = jobs
        self._redis = redis_client
        self._changes = changes

    async def create(self, data: ProjectCreate) -> Project:
        """Store a new project; the saved project."""
        _require_strategy(data.routing_strategy)
        project = Project(**data.model_dump())
        try:
            async with self._session_factory() as session:
                await ProjectRepository(session).create(project)
                await session.commit()
        except IntegrityError as exc:
            raise _taken(exc, project) from None
        logger.info("Added project", project_id=project.id, name=project.name)
        await self._changes.record(ChangeKind.PROJECT, project.id)
        return project

    async def update(self, project_id: str, data: ProjectUpdate) -> Project:
        """Apply the fields ``data`` sets to a project; the saved project.

        An empty string clears ``location_conflict_rule`` and ``location_city_radius_km``, so the project inherits the
        install default again, and the fields the TLS interception mode does not use are cleared.
        """
        async with self._session_factory() as session:
            projects = ProjectRepository(session)
            current = await projects.get_by_id(project_id)
            if current is None:
                raise NotFoundError("Project not found")
            try:
                edited = current.model_copy(update=data.changes()).mitm_checked()
            except ValueError as exc:
                raise WriteRejectedError(str(exc)) from None
            if data.routing_strategy is not None:
                _require_strategy(edited.routing_strategy)
            try:
                saved = await projects.update(edited)
                await session.commit()
            except IntegrityError as exc:
                raise _taken(exc, edited) from None
        if saved is None:
            # Deleted between the read and the write.
            raise NotFoundError("Project not found")
        logger.info("Updated project", project_id=project_id, name=saved.name)
        await self._changes.record(ChangeKind.PROJECT, project_id)
        if saved.judgement != current.judgement:
            # The rows were judged under the old rules; a worker re-judges them.
            await self._jobs.request_now(JobKind.REATTRIBUTE, [ReattributeScope(project_id=project_id).key])
        return saved

    async def delete(self, project_id: str) -> None:
        """Delete a project and, by cascade, everything it holds."""
        async with self._session_factory() as session:
            if not await ProjectRepository(session).delete(project_id):
                raise NotFoundError("Project not found")
            await session.commit()
        logger.info("Removed project", project_id=project_id)
        await self._changes.record(ChangeKind.PROJECT, project_id)
        keys = project_keys(project_id)
        await self._redis.after_commit(
            lambda: self._redis.client.delete(*keys), "delete the project's keys", project_id=project_id
        )


def _require_strategy(name: str) -> None:
    """Refuse a routing strategy Octoprox does not have."""
    try:
        strategy_name(name)
    except ValueError as exc:
        raise WriteRejectedError(str(exc)) from None


def _taken(exc: IntegrityError, project: Project) -> NameConflictError:
    """The unique name or username ``project`` collided with."""
    return name_conflict(
        exc,
        {
            _NAME_UNIQUE: f"Project with name '{project.name}' already exists",
            _USERNAME_UNIQUE: f"Project with username '{project.username}' already exists",
        },
    )
