# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the api's project map: every project in memory, kept current from the changes stream.

Projects are written to the test Postgres and their change entries to the test Redis, as the project writes record
them; a :class:`Harness` runs the map's loop as a task and notes what each read asked Postgres for.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import AbstractAsyncContextManager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.job_stats import job_stats
from api.core.workers import WorkerName
from api.db.redis import CHANGES_STREAM_KEY, RedisClient
from api.db.repository import ProjectRepository
from api.db.streams import ChangeKind, ChangeLog
from api.models.project import ExitReputationMode, Project
from api.roles.refresh import ProjectMap
from tests.jobs.conftest import Rows
from tests.waits import until


class Flaky:
    """A session factory that fails like an unreachable Postgres while ``down``."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self.down = False

    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        if self.down:
            raise OSError("connection refused")
        return self._sessions()


class Harness:
    """A project map over the test Postgres, its loop run as a task; ``reads`` notes each read, whole ("all") or the
    project ids it asked for."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        monkeypatch: pytest.MonkeyPatch,
        *,
        reconcile_interval: float = 3600.0,
    ) -> None:
        self.flaky = Flaky(sessions)
        self.projects = ProjectMap(self.flaky, redis_client=redis_client, reconcile_interval=reconcile_interval)
        self.reads: list[str | list[str]] = []
        get_all, get_many = ProjectRepository.get_all, ProjectRepository.get_many

        async def reading_all(repository: ProjectRepository) -> list[Project]:
            self.reads.append("all")
            return await get_all(repository)

        async def reading_some(repository: ProjectRepository, project_ids: Iterable[str]) -> list[Project]:
            wanted = sorted(project_ids)
            self.reads.append(wanted)
            return await get_many(repository, wanted)

        monkeypatch.setattr(ProjectRepository, "get_all", reading_all)
        monkeypatch.setattr(ProjectRepository, "get_many", reading_some)
        self.task: asyncio.Task[None] | None = None

    def follow(self) -> None:
        """Run the loop."""
        self.task = asyncio.create_task(self.projects.run(), name=WorkerName.PROJECT_MAP)

    async def close(self) -> None:
        await self.projects.stop()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    @property
    def entries(self) -> int:
        """Stream entries the loop has read so far."""
        return self.projects.streams()["changes"].entries


@pytest.fixture(autouse=True)
def _counters() -> None:
    """The loop counts into the process's job stats: each test starts from none."""
    job_stats.reset()


@pytest.fixture
async def harness(
    sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Harness]:
    built = Harness(sessions, redis_client, monkeypatch)
    yield built
    await built.close()


async def _saved(sessions: async_sessionmaker[AsyncSession], project: Project) -> None:
    """``project`` written over its stored row, as an edit writes it."""
    async with sessions() as session:
        await ProjectRepository(session).update(project)
        await session.commit()


async def _deleted(sessions: async_sessionmaker[AsyncSession], project: Project) -> None:
    async with sessions() as session:
        await ProjectRepository(session).delete(project.id)
        await session.commit()


class TestStart:
    async def test_start_reads_every_project_from_the_streams_position(self, harness: Harness, rows: Rows) -> None:
        first = await rows.project(exit_reputation=ExitReputationMode.RECORD)
        second = await rows.project()

        await harness.projects.start()

        assert _mode(harness, first.id) is ExitReputationMode.RECORD
        assert _mode(harness, second.id) is ExitReputationMode.OFF
        assert harness.projects.get("no-such-project") is None
        assert harness.reads == ["all"]
        state = harness.projects.streams()["changes"]
        assert (state.position, state.full_reads) == ("0-0", {"start": 1})

    async def test_a_start_that_cannot_read_postgres_raises(self, harness: Harness) -> None:
        harness.flaky.down = True

        with pytest.raises(OSError):
            await harness.projects.start()


class TestFollowing:
    async def test_a_project_entry_reads_that_project_alone(
        self, harness: Harness, rows: Rows, sessions: async_sessionmaker[AsyncSession], change_log: ChangeLog
    ) -> None:
        project = await rows.project(exit_reputation=ExitReputationMode.RECORD)
        other = await rows.project(exit_reputation=ExitReputationMode.RECORD)
        await harness.projects.start()
        harness.follow()

        await _saved(sessions, project.model_copy(update={"exit_reputation": ExitReputationMode.OFF}))
        await change_log.record(ChangeKind.PROJECT, project.id)

        # Within a second, not at the next reconcile an hour away.
        await until(lambda: _mode(harness, project.id) is ExitReputationMode.OFF, timeout=1.0)
        assert harness.reads == ["all", [project.id]]
        assert _mode(harness, other.id) is ExitReputationMode.RECORD

    async def test_a_new_project_enters_the_map_and_a_deleted_one_leaves_it(
        self, harness: Harness, rows: Rows, sessions: async_sessionmaker[AsyncSession], change_log: ChangeLog
    ) -> None:
        await harness.projects.start()
        harness.follow()

        project = await rows.project()
        await change_log.record(ChangeKind.PROJECT, project.id)
        await until(lambda: harness.projects.get(project.id) is not None)

        await _deleted(sessions, project)
        await change_log.record(ChangeKind.PROJECT, project.id)
        await until(lambda: harness.projects.get(project.id) is None)
        assert harness.reads == ["all", [project.id], [project.id]]

    @pytest.mark.parametrize("kind", [ChangeKind.PROJECT, ChangeKind.ALL])
    async def test_an_entry_naming_several_projects_or_none_or_anything_reads_every_project(
        self, harness: Harness, rows: Rows, change_log: ChangeLog, kind: ChangeKind
    ) -> None:
        await harness.projects.start()
        harness.follow()

        project = await rows.project()
        await change_log.record(kind)

        await until(lambda: harness.projects.get(project.id) is not None)
        assert harness.reads == ["all", "all"]
        assert harness.projects.streams()["changes"].full_reads == {"start": 1}

    async def test_other_kinds_read_nothing(self, harness: Harness, change_log: ChangeLog) -> None:
        await harness.projects.start()
        harness.follow()

        kinds = [kind for kind in ChangeKind if kind not in (ChangeKind.PROJECT, ChangeKind.ALL)]
        for kind in kinds:
            await change_log.record(kind, "row-1")
        await until(lambda: harness.entries == len(kinds))
        await asyncio.sleep(0.05)

        assert harness.reads == ["all"]
        stats = job_stats.get(WorkerName.PROJECT_MAP)
        assert stats is not None and stats.runs == stats.idle_runs >= 1

    async def test_projects_named_together_are_read_in_one_statement(
        self, harness: Harness, rows: Rows, change_log: ChangeLog
    ) -> None:
        await harness.projects.start()
        made = [await rows.project() for _ in range(3)]
        for project in made:
            await change_log.record(ChangeKind.PROJECT, project.id)

        harness.follow()

        await until(lambda: all(harness.projects.get(project.id) is not None for project in made))
        assert harness.reads == ["all", sorted(project.id for project in made)]

    async def test_a_gap_in_the_stream_reads_in_full(
        self,
        harness: Harness,
        rows: Rows,
        change_log: ChangeLog,
        redis_client: RedisClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The stream wiped after the position: whatever it held may have named a project."""
        monkeypatch.setattr("api.db.streams.STREAM_BLOCK_SECONDS", 0.1)
        await harness.projects.start()
        harness.follow()
        await change_log.record(ChangeKind.CONNECTOR, "c-1")
        await until(lambda: harness.entries == 1)
        project = await rows.project()

        await redis_client.client.delete(CHANGES_STREAM_KEY)

        await until(lambda: harness.projects.get(project.id) is not None)
        assert harness.projects.streams()["changes"].full_reads == {"start": 1, "gap": 1}

    async def test_the_reconcile_reads_what_announced_nothing(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        rows: Rows,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A write whose entry was lost (its process died after the commit), or one made by hand in SQL."""
        built = Harness(sessions, redis_client, monkeypatch, reconcile_interval=0.3)
        try:
            await built.projects.start()
            built.follow()

            project = await rows.project()

            await until(lambda: built.projects.get(project.id) is not None)
            assert built.projects.streams()["changes"].full_reads["reconcile"] >= 1
        finally:
            await built.close()


class TestFailures:
    async def test_a_project_read_that_fails_is_made_again_until_postgres_answers(
        self,
        harness: Harness,
        rows: Rows,
        sessions: async_sessionmaker[AsyncSession],
        change_log: ChangeLog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No later entry may come to name it again: the loop keeps it named, and reads it after each back-off."""
        monkeypatch.setattr("api.db.streams.RETRY_FIRST_SECONDS", 0.02)
        monkeypatch.setattr("api.db.streams.RETRY_MAX_SECONDS", 0.05)
        project = await rows.project(exit_reputation=ExitReputationMode.RECORD)
        await harness.projects.start()
        harness.follow()

        harness.flaky.down = True
        await _saved(sessions, project.model_copy(update={"exit_reputation": ExitReputationMode.AVOID}))
        await change_log.record(ChangeKind.PROJECT, project.id)
        await until(lambda: harness.entries == 1)
        await until(lambda: _failures() >= 2)
        # What was read stands meanwhile.
        assert _mode(harness, project.id) is ExitReputationMode.RECORD

        harness.flaky.down = False

        await until(lambda: _mode(harness, project.id) is ExitReputationMode.AVOID)
        assert harness.reads[-1] == [project.id]


def _failures() -> int:
    """The map's loop's failed cycles so far."""
    stats = job_stats.get(WorkerName.PROJECT_MAP)
    return stats.failures if stats is not None else 0


def _mode(harness: Harness, project_id: str) -> ExitReputationMode | None:
    """The exit reputation mode the map holds for the project; None when it holds no such project."""
    project = harness.projects.get(project_id)
    return project.exit_reputation if project is not None else None
