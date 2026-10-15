# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the metrics flusher: Redis counters into Postgres history, nothing lost on the way.

The instances keep adding to the Redis hashes while the leader's flush
transaction runs. What they add after the flusher's read must survive into
the next flush, so each test injects a peer's publish in the middle of a
flush and checks both sides.
"""

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.metrics_flusher import MetricsFlusher
from api.core.stats import MetricDelta
from api.db.models import FailureMetricsModel, ProjectMetricsModel
from api.db.redis import RedisClient, failure_metrics_key
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
        redis_client: RedisClient,
        test_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project = await _project(db_session_factory)
        failure_key = (project.id, None, "no_proxy_available", None)
        await redis_client.flush_metric_deltas({}, {project.id: _requests(3)})
        await redis_client.append_failures([], {failure_key: 4}, max_entries=10, retention_seconds=60)

        original = MetricsRepository.save_project_metrics_snapshot

        async def save_while_a_peer_publishes(self: MetricsRepository, **kwargs: Any) -> bool:
            await redis_client.flush_metric_deltas({}, {project.id: _requests(2)})
            await redis_client.append_failures([], {failure_key: 1}, max_entries=10, retention_seconds=60)
            return await original(self, **kwargs)

        monkeypatch.setattr(MetricsRepository, "save_project_metrics_snapshot", save_while_a_peer_publishes)
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
