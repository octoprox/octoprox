# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Project writes: create, edit and delete, each Postgres transaction announced with a ``project`` change entry.

An edit to how locations are judged asks for ``reattribute project:<id>``. A delete removes the project's connectors as
the connector delete does and the project once none is left, then the Redis keys only the project writes.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.exc import IntegrityError

from api.core.roles import JobKind
from api.db.redis import project_keys
from api.db.repository import ConnectorRepository, ProjectRepository
from api.db.streams import ChangeKind
from api.jobs.attribution import ReattributeScope
from api.models.project import Project, ProjectCreate, ProjectUpdate
from api.services.errors import NameConflictError, NotFoundError, WriteRejectedError, name_conflict
from api.strategies import strategy_name

if TYPE_CHECKING:
    from api.core.audit import Actor
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog
    from api.jobs.queue import JobQueue
    from api.services.connectors import ConnectorService

logger = structlog.get_logger()

# The unique constraints of the projects table, as Postgres named them when
# migration 002 created the table with unique name and username columns.
_NAME_UNIQUE = "projects_name_key"
_USERNAME_UNIQUE = "projects_username_key"


class ProjectService:
    """Creates, edits and deletes projects.

    Args:
        redis_client: Deletes a deleted project's captures and failure log, under its request deadline.
        connectors: Removes a deleted project's connectors, a cloud one once the scale job has terminated its instances.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        jobs: JobQueue,
        redis_client: RedisClient,
        *,
        changes: ChangeLog,
        connectors: ConnectorService,
    ) -> None:
        self._session_factory = session_factory
        self._jobs = jobs
        self._redis = redis_client
        self._changes = changes
        self._connectors = connectors

    async def create(self, data: ProjectCreate, actor: Actor) -> Project:
        """Store a new project; the saved project."""
        _require_strategy(data.routing_strategy)
        project = Project(**data.model_dump())
        try:
            async with self._session_factory() as session:
                await ProjectRepository(session).create(project)
                await actor.created(session, project)
                await session.commit()
        except IntegrityError as exc:
            raise _taken(exc, project) from None
        logger.info("Added project", project_id=project.id, name=project.name)
        await self._changes.record(ChangeKind.PROJECT, project.id)
        return project

    async def update(self, project_id: str, data: ProjectUpdate, actor: Actor) -> Project:
        """Apply the fields ``data`` sets to a project; the saved project.

        An empty string clears ``location_conflict_rule``, ``location_city_radius_km`` and ``observation_retention_days``,
        so the project inherits the install default again, and the fields the TLS interception mode does not use are cleared.
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
                if saved is not None:
                    await actor.updated(session, current, saved)
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

    async def delete(self, project_id: str, actor: Actor) -> bool:
        """Delete a project and, by cascade, everything it holds, its cloud instances terminated first; whether it went now.

        The project is marked pending deletion, which turns its clients away and refuses new connectors, then each
        connector is removed as the connector delete removes it. Holding no cloud connector, the project goes at once;
        otherwise the scale run that deletes its last one deletes it (``delete_if_emptied``). Asked again, it carries on.
        """
        async with self._session_factory() as session:
            projects = ProjectRepository(session)
            marked = await projects.mark_pending_deletion(project_id)
            project = await projects.get_by_id(project_id)
            if project is None:
                raise NotFoundError("Project not found")
            connectors = await ConnectorRepository(session).get_by_project(project_id)
            if marked:
                await actor.deleted(session, project)
            await session.commit()
        await self._changes.record(ChangeKind.PROJECT, project_id)
        for connector in connectors:
            # Gone already: the scale job deleted it, or another delete removed it.
            with contextlib.suppress(NotFoundError):
                await self._connectors.remove(project_id, connector.id, actor)
        if await self.delete_if_emptied(project_id):
            return True
        logger.info("Marked project for deletion", project_id=project_id)
        return False

    async def delete_if_emptied(self, project_id: str) -> bool:
        """Delete a project pending deletion once it holds no connector; whether it went.

        The scale job asks after each cloud connector it deletes, so the project goes with its last one.
        """
        async with self._session_factory() as session:
            deleted = await ProjectRepository(session).delete_if_emptied(project_id)
            await session.commit()
        if not deleted:
            return False
        logger.info("Removed project", project_id=project_id)
        await self._changes.record(ChangeKind.PROJECT, project_id)
        keys = project_keys(project_id)
        await self._redis.after_commit(
            lambda: self._redis.client.delete(*keys), "delete the project's keys", project_id=project_id
        )
        return True


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
