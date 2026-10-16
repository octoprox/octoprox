# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the metrics flusher: Redis counters into Postgres history, nothing lost on the way.

The instances keep adding to the Redis hashes while the leader's flush
transaction runs. What they add after the flusher's read must survive into
the next flush, so each test injects a peer's publish in the middle of a
flush and checks both sides.
"""

import asyncio
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import metrics_flusher
from api.core.config import Settings
from api.core.job_stats import job_stats
from api.core.leadership import Lease
from api.core.metrics_flusher import MetricsFlusher
from api.core.stats import MetricDelta
from api.core.workers import WorkerName
from api.db.models import FailureMetricsModel, ProjectMetricsModel
from api.db.redis import LEASE_KEY, RedisClient, failure_metrics_key
from api.db.repository import MetricsRepository, ProjectRepository
from api.models.project import Project


def _requests(n: int) -> MetricDelta:
    delta = MetricDelta()
    for _ in range(n):
        delta.add_request(True, 100.0, 10, 20)
    return delta


async def _project(session_factory: async_sessionmaker[AsyncSession]) -> Project:
    project = Project(name="Flushed", username="flushed_user", password="pass")
    async with session_factory() as session:
        await ProjectRepository(session).create(project)
        await session.commit()
    return project


async def _sum(session_factory: async_sessionmaker[AsyncSession], column: Any, model: Any, project_id: str) -> int:
    async with session_factory() as session:
        result = await session.execute(select(func.sum(column)).where(model.project_id == project_id))
        return int(result.scalar() or 0)


class TestFlushKeepsWhatArrivesMeanwhile:
    async def test_project_and_failure_counts(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        redis_client: RedisClient,
        test_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = await _project(db_session_factory)
        failure_key = (project.id, None, "no_proxy_available", None)
        await redis_client.flush_metric_deltas({}, {project.id: _requests(3)})
        await redis_client.append_failures([], {failure_key: 4}, max_entries=10, retention_seconds=60)

        original = MetricsRepository.save_project_metrics_snapshots

        async def save_while_a_peer_publishes(self: MetricsRepository, batch: dict[str, MetricDelta]) -> list[str]:
            await redis_client.flush_metric_deltas({}, {project.id: _requests(2)})
            await redis_client.append_failures([], {failure_key: 1}, max_entries=10, retention_seconds=60)
            return await original(self, batch)

        monkeypatch.setattr(MetricsRepository, "save_project_metrics_snapshots", save_while_a_peer_publishes)
        flusher = MetricsFlusher(db_session_factory, redis_client, test_settings)
        await flusher._flush_metrics()

        # This flush wrote what it read; the peer's publish waits in Redis.
        assert await _sum(db_session_factory, ProjectMetricsModel.request_count, ProjectMetricsModel, project.id) == 3
        assert await _sum(db_session_factory, FailureMetricsModel.failure_count, FailureMetricsModel, project.id) == 4
        left = await redis_client.get_project_metrics(project.id)
        assert left is not None and left.request_count == 2
        assert await redis_client.get_all_failure_metrics() == {failure_key: 1}

        # The next flush takes the rest, and leaves nothing behind.
        monkeypatch.undo()
        await flusher._flush_metrics()
        assert await _sum(db_session_factory, ProjectMetricsModel.request_count, ProjectMetricsModel, project.id) == 5
        assert await _sum(db_session_factory, FailureMetricsModel.failure_count, FailureMetricsModel, project.id) == 5
        assert await redis_client.get_project_metrics(project.id) is None
        assert not await redis_client.client.exists(failure_metrics_key(failure_key))

    async def test_counts_of_a_gone_project_are_dropped_and_cleared(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        test_settings: Settings,
    ) -> None:
        await redis_client.flush_metric_deltas({}, {"gone": _requests(2)})
        await redis_client.append_failures(
            [], {("gone", None, "no_proxy_available", None): 3}, max_entries=10, retention_seconds=60
        )

        await MetricsFlusher(db_session_factory, redis_client, test_settings)._flush_metrics()

        assert await redis_client.get_project_metrics("gone") is None
        assert await redis_client.get_all_failure_metrics() == {}


class TestFlushThatLostItsLease:
    async def test_writes_nothing_and_leaves_the_counts_for_the_new_holder(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        redis_client: RedisClient,
        test_settings: Settings,
    ) -> None:
        project = await _project(db_session_factory)
        await redis_client.flush_metric_deltas({}, {project.id: _requests(3)})
        # Its own name: the test app's flusher holds the real one in this Redis.
        lease = Lease(redis_client, name="flusher_under_test", owner_id="stalled")
        assert await lease.try_acquire()
        try:
            # The key expired before the stalled holder's next refresh, and a peer took it.
            await redis_client.client.set(LEASE_KEY.format(name="flusher_under_test"), "peer")
            assert await MetricsFlusher(db_session_factory, redis_client, test_settings)._flush_metrics(lease) is False
        finally:
            await lease.release()

        assert await _sum(db_session_factory, ProjectMetricsModel.request_count, ProjectMetricsModel, project.id) == 0
        left = await redis_client.get_project_metrics(project.id)
        assert left is not None and left.request_count == 3
        assert not lease.is_held


class ScriptedLease:
    """Stands in for the flusher's lease: ``is_held`` answers from a script, then False."""

    def __init__(self, held: list[bool], confirm: bool, on_retry: Any) -> None:
        self._held = held
        self._confirm = confirm
        self._on_retry = on_retry

    @property
    def is_held(self) -> bool:
        return self._held.pop(0) if self._held else False

    async def try_acquire(self) -> bool:
        self._on_retry()
        return False

    async def confirm(self) -> bool:
        return self._confirm

    async def release(self) -> None:
        pass


class TestCyclesThatWroteNothing:
    """A flush that did not happen is a gap in the history; the system view counts it."""

    @pytest.mark.parametrize(
        ("held", "confirm", "error"),
        [
            # Lost while the loop slept: no flush is even attempted.
            ([True, False], True, "Skipped: the lease was lost before the flush"),
            # Lost by the time of the commit: the flush is rolled back.
            ([True, True], False, "Rolled back: the lease could not be confirmed before the commit"),
        ],
    )
    async def test_is_counted_as_a_failed_cycle(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        db_session: AsyncSession,
        redis_client: RedisClient,
        test_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        held: list[bool],
        confirm: bool,
        error: str,
    ) -> None:
        project = await _project(db_session_factory)
        await redis_client.flush_metric_deltas({}, {project.id: _requests(1)})
        flusher = MetricsFlusher(
            db_session_factory, redis_client, test_settings.model_copy(update={"metrics_flush_interval": 0})
        )
        monkeypatch.setattr(metrics_flusher, "_LEASE_RETRY_SECONDS", 0)
        monkeypatch.setattr(
            metrics_flusher, "Lease", lambda *args, **kwargs: ScriptedLease(list(held), confirm, flusher.stop)
        )
        job_stats.reset()
        try:
            await asyncio.wait_for(flusher.run(), 5)
            stats = job_stats.get(WorkerName.METRICS_FLUSHER)
            assert stats is not None
            assert (stats.runs, stats.failures, stats.last_error) == (1, 1, error)
            # Standing by afterwards: counted, but not shown as failing now.
            assert stats.consecutive_failures == 0
        finally:
            job_stats.reset()
            await redis_client.reset_project_metrics(project.id)
