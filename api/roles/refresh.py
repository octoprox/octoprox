# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The followers of the changes stream: how an api or worker process keeps what it holds in memory current.

The definitions refresher reads the descriptors, attribution settings and IP database list again when the changes
stream names one, or may have missed one; the api's project map reads a project again when an entry names it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from api.core.consumers import BroadcastFollower
from api.core.logging import error_text
from api.core.workers import WorkerName
from api.db.redis import CHANGES_STREAM_KEY
from api.db.repository import ProjectRepository
from api.db.streams import REF_FIELD, ChangeKind, FullRead

if TYPE_CHECKING:
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import StreamEntry
    from api.geo.settings import GeoSettingsStore
    from api.geo.store import GeoDatabaseStore
    from api.models.project import Project
    from api.providers.store import ProviderStore

logger = structlog.get_logger()

# The change kinds that name what an api or worker process keeps in memory, or anything at all. The others name what
# the controller compiles, which such a process reads from Postgres whenever it needs it.
READ_ON = frozenset({ChangeKind.DESCRIPTOR, ChangeKind.GEO_SETTINGS, ChangeKind.GEO_DATABASE, ChangeKind.ALL})


class ChangesFollower(BroadcastFollower):
    """Follows the changes stream from the position :meth:`start` takes, reading in full whenever it may have missed an
    entry (:class:`BroadcastFollower`).

    Args:
        reconcile_interval: seconds from one read in full to the next, made whatever the stream says.
    """

    def __init__(self, redis_client: RedisClient, *, name: WorkerName, reconcile_interval: float) -> None:
        super().__init__(redis_client, [CHANGES_STREAM_KEY], name=name, reconcile_interval=reconcile_interval)

    async def start(self) -> None:
        """Take the stream's position, then read in full: the read the process serves with.

        Raises when Postgres cannot be read; without Redis it reads all the same, and the loop reads in full once Redis answers.
        """
        try:
            async with self._redis.request_deadline():
                await self._reader.start(FullRead.START)
        except Exception as exc:
            logger.debug("Changes stream not followed yet", loop=self.name, error=error_text(exc))
        await self._full_read()


class DefinitionsRefresher(ChangesFollower):
    """Reads the provider descriptors, attribution settings and IP database list again when the changes stream names one.

    :meth:`start` makes the read the process serves with, from the stream's position on, and the loop follows from there.
    """

    def __init__(
        self,
        provider_store: ProviderStore,
        geo_settings_store: GeoSettingsStore,
        geo_database_store: GeoDatabaseStore,
        *,
        redis_client: RedisClient,
        reconcile_interval: float,
    ) -> None:
        super().__init__(redis_client, name=WorkerName.DEFINITIONS_REFRESHER, reconcile_interval=reconcile_interval)
        self._provider_store = provider_store
        self._geo_settings_store = geo_settings_store
        self._geo_database_store = geo_database_store
        # One read at a time: the loop's, or a sync job's that meets a credential type its registry lacks.
        self._reading = asyncio.Lock()
        # Whether a read has worked yet: the process serves only once one has.
        self._loaded = False

    async def read(self) -> None:
        """Read the descriptors, attribution settings and IP database list again; each store applies only what changed.

        Raises when Postgres cannot be read, keeping what is in memory; a read asked for while another runs waits for it.
        """
        async with self._reading:
            await self._provider_store.sync_all()
            await self._geo_settings_store.load()
            await self._geo_database_store.sync_all()
            self._loaded = True

    def readiness(self) -> str | None:
        """None once the definitions have been read, else why not."""
        return None if self._loaded else "definitions not loaded yet"

    def _on_entries(self, key: str, entries: list[StreamEntry]) -> bool:
        """Whether any entry names what this process keeps (``READ_ON``)."""
        return any(entry.kind in READ_ON for entry in entries)

    async def _full_read(self) -> None:
        """Read the definitions again."""
        await self.read()


class ProjectMap(ChangesFollower):
    """Every project as Postgres last held it, by id, in an api process: the exit reports route takes its project from
    here instead of reading Postgres per call.

    A project change entry reads the project it names again, and one that names several or none, or a change to
    anything (``all``), reads every project, as the start, a gap and the reconcile do; a project gone leaves the map.
    """

    def __init__(self, session_factory: SessionFactory, *, redis_client: RedisClient, reconcile_interval: float) -> None:
        super().__init__(redis_client, name=WorkerName.PROJECT_MAP, reconcile_interval=reconcile_interval)
        self._session_factory = session_factory
        self._projects: dict[str, Project] = {}
        # The projects entries named since the last read, each to be read again.
        self._named: set[str] = set()

    def get(self, project_id: str) -> Project | None:
        """The project as last read; None for one Postgres did not hold then."""
        return self._projects.get(project_id)

    def _on_entries(self, key: str, entries: list[StreamEntry]) -> bool:
        """Note each project an entry names; True for an entry that names several or none, or a change to anything."""
        for entry in entries:
            if entry.kind == ChangeKind.ALL:
                return True
            if entry.kind == ChangeKind.PROJECT:
                project_id = entry.fields.get(REF_FIELD, "")
                if not project_id:
                    return True
                self._named.add(project_id)
        return False

    def _next_duty(self) -> float:
        """0 while a project named waits for its read, as after a read that failed; else until the reconcile."""
        return 0.0 if self._named else super()._next_duty()

    async def _act(self) -> bool:
        """The full read when one is owed, else a read of the projects named since, one statement; whether either ran.

        Raising keeps the projects named, read again after the back-off.
        """
        if self._owed or not self._named:
            return await super()._act()
        named = sorted(self._named)
        async with self._session_factory() as session:
            found = {project.id: project for project in await ProjectRepository(session).get_many(named)}
        for project_id in named:
            project = found.get(project_id)
            if project is None:
                self._projects.pop(project_id, None)
            else:
                self._projects[project_id] = project
        self._named.difference_update(named)
        return True

    async def _full_read(self) -> None:
        """Read every project again: the map then holds what Postgres holds, and covers every project named before."""
        async with self._session_factory() as session:
            projects = await ProjectRepository(session).get_all()
        self._projects = {project.id: project for project in projects}
        self._named.clear()
