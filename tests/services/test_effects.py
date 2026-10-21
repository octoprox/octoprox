# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for what follows a committed write: a Redis that fails or stalls then never fails the write.

Redis fails right after the commit, or stops answering (``CLIENT PAUSE``): the write must return its saved model with
each step after the commit cut at the request deadline, stay committed and log what was lost, and its change entry lands
once Redis answers.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.roles import JobKind
from api.db.redis import TRAFFIC_PENDING_KEY, RedisClient
from api.db.repository import ConnectorRepository, ProjectRepository, ProxyRepository
from api.db.streams import ChangeKind, ChangeLog
from api.jobs.queue import JobQueue
from api.models.connector import Connector
from api.models.project import Project
from api.models.proxy import Proxy, ProxyCreate
from api.providers.registry import ProviderRegistry
from api.services.connectors import ConnectorService
from api.services.projects import ProjectService
from api.services.proxies import ProxyService
from tests.conftest import queued_job_keys
from tests.services.conftest import OXYLABS_POOL, Connect, announced, forget_announcements

# The request deadline of the stalling tests' client, in seconds: short, so a
# test waits a fraction of a second where the default would wait two.
DEADLINE = 0.25
# How long the stalling tests keep Redis from writing, in milliseconds: well
# past every deadline a save meets, and short enough to wait out.
PAUSE_MS = 2500
# What a stalled save may take beyond its steps' deadlines: the Postgres work.
MARGIN = 0.5


def failed_requests(logs: list[dict[str, Any]]) -> list[str]:
    """The kinds of the job requests a write's logs say were lost, in order."""
    return [log["kind"] for log in logs if log["event"] == "Redis step after a write failed" and log["step"] == "request jobs"]


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch, jobs: JobQueue, redis_client: RedisClient) -> list[str]:
    """Redis refuses every job request, key deletion and MULTI from now on; the kinds asked for, keys deleted and "MULTI"s, in order."""
    calls: list[str] = []
    open_pipeline = redis_client.client.pipeline

    async def refused(*args: Any, **kwargs: Any) -> None:
        calls.append(str(args[0]) if args else "")
        raise ConnectionError("Redis is down")

    async def refused_multi(*args: Any, **kwargs: Any) -> None:
        calls.append("MULTI")
        raise ConnectionError("Redis is down")

    def pipeline(transaction: bool = True, shard_hint: str | None = None) -> Any:
        pipe = open_pipeline(transaction=transaction, shard_hint=shard_hint)
        if transaction:
            pipe.execute = refused_multi
        return pipe

    monkeypatch.setattr(jobs, "publish", refused)
    monkeypatch.setattr(redis_client.client, "delete", refused)
    monkeypatch.setattr(redis_client.client, "pipeline", pipeline)
    return calls


@pytest.fixture
async def stalling(test_redis_url: str) -> AsyncIterator[RedisClient]:
    """A client as a node builds one, with a request deadline of ``DEADLINE``, connected before the stall."""
    client = RedisClient(test_redis_url, request_timeout=DEADLINE)
    await client.connect()
    await client.client.ping()
    yield client
    await client.close()


@pytest.fixture
async def stall(redis_client: RedisClient) -> AsyncIterator[Callable[[], Awaitable[object]]]:
    """Starts a stall: Redis takes no write (``CLIENT PAUSE ... WRITE``) for ``PAUSE_MS``, or until the test ends."""
    yield lambda: redis_client.client.client_pause(PAUSE_MS, all=False)
    await redis_client.client.client_unpause()


class TestCommittedWritesStand:
    async def test_a_proxy_added_by_hand_is_saved_without_its_job_requests(
        self,
        proxy_service: ProxyService,
        project: Project,
        static_connector: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_down: list[str],
    ) -> None:
        with structlog.testing.capture_logs() as logs:
            proxy = await proxy_service.create(
                project.id, ProxyCreate(host="10.0.0.1", port=8080, connector_id=static_connector.id)
            )

        async with sessions() as session:
            assert await ProxyRepository(session).get_by_id(proxy.id) == proxy
        assert failed_requests(logs) == ["health", "enrich"]
        # One attempt each, not one wait per key.
        assert redis_down == ["health", "enrich"]

    async def test_a_connector_that_syncs_is_saved_without_its_first_sync(
        self, connect: Connect, project: Project, sessions: async_sessionmaker[AsyncSession], redis_down: list[str]
    ) -> None:
        with structlog.testing.capture_logs() as logs:
            connector = await connect(project, "Pool", OXYLABS_POOL)

        async with sessions() as session:
            assert await ConnectorRepository(session).get_by_id(connector.id) is not None
        assert failed_requests(logs) == ["sync"]

    async def test_a_traffic_reset_stands_without_its_traffic_decision(
        self,
        connector_service: ConnectorService,
        project: Project,
        static_connector: Connector,
        redis_client: RedisClient,
        redis_down: list[str],
    ) -> None:
        """The decision's run-now entry and the pending mark go in one MULTI: one try, and neither lands."""
        with structlog.testing.capture_logs() as logs:
            saved = await connector_service.reset_traffic(project.id, static_connector.id)

        assert saved.traffic_reset_at is not None
        assert redis_down == ["MULTI"]
        assert [
            (log["step"], log["connector_id"]) for log in logs if log["event"] == "Redis step after a write failed"
        ] == [("ask for a traffic decision", static_connector.id)]
        assert await redis_client.client.exists(TRAFFIC_PENDING_KEY) == 0
        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == []

    async def test_a_deleted_connector_and_project_stay_deleted_when_their_keys_cannot_go(
        self,
        connector_service: ConnectorService,
        project_service: ProjectService,
        project: Project,
        static_connector: Connector,
        sessions: async_sessionmaker[AsyncSession],
        redis_down: list[str],
    ) -> None:
        with structlog.testing.capture_logs() as logs:
            await connector_service.remove(project.id, static_connector.id)
            await project_service.delete(project.id)

        async with sessions() as session:
            assert await ConnectorRepository(session).get_by_id(static_connector.id) is None
            assert await ProjectRepository(session).get_by_id(project.id) is None
        steps = [log["step"] for log in logs if log["event"] == "Redis step after a write failed"]
        assert steps == ["delete the connector's scale state", "delete the project's keys"]

    async def test_a_save_on_a_stalled_redis_stands_and_is_announced_once_redis_answers(
        self,
        stalling: RedisClient,
        redis_client: RedisClient,
        stall: Callable[[], Awaitable[object]],
        sessions: async_sessionmaker[AsyncSession],
        registry: ProviderRegistry,
        project: Project,
        static_connector: Connector,
    ) -> None:
        """Each step after the commit waits its deadline, no more; the change entry waits for Redis, and lands."""
        log = ChangeLog(stalling)
        service = ProxyService(sessions, JobQueue(stalling), stalling, registry, changes=log)
        await forget_announcements(redis_client)
        try:
            await stall()
            started = time.monotonic()
            with structlog.testing.capture_logs() as logs:
                proxy = await service.create(
                    project.id, ProxyCreate(host="10.0.0.1", port=8080, connector_id=static_connector.id)
                )
            # Three steps (the entry, the health and enrich requests), each cut at its deadline.
            assert time.monotonic() - started < DEADLINE * 3 + MARGIN
            async with sessions() as session:
                assert await ProxyRepository(session).get_by_id(proxy.id) == proxy
            assert failed_requests(logs) == ["health", "enrich"]
            assert log.pending == {ChangeKind.PROXY}
            await redis_client.client.client_unpause()
        finally:
            # What is pending is sent once more: Redis answers by now.
            await log.close()

        assert await announced(redis_client) == [("proxy", proxy.id)]

    async def test_a_batch_delete_on_a_stalled_redis_stands_with_its_sync_given_up_at_the_deadline(
        self,
        stalling: RedisClient,
        redis_client: RedisClient,
        stall: Callable[[], Awaitable[object]],
        sessions: async_sessionmaker[AsyncSession],
        registry: ProviderRegistry,
        connect: Connect,
        project: Project,
    ) -> None:
        """Two connectors that sync, one request for both: the entry and that request each wait the deadline once."""
        pools = [await connect(project, name, OXYLABS_POOL) for name in ("Pool A", "Pool B")]
        rows = [Proxy(host="pr.oxylabs.io", port=7777, connector_id=pool.id) for pool in pools for _ in range(3)]
        async with sessions() as session:
            await ProxyRepository(session).bulk_create(rows)
            await session.commit()
        log = ChangeLog(stalling)
        service = ProxyService(sessions, JobQueue(stalling), stalling, registry, changes=log)
        try:
            await stall()
            started = time.monotonic()
            with structlog.testing.capture_logs() as logs:
                deleted = await service.delete_many(row.id for row in rows)
            assert time.monotonic() - started < DEADLINE * 2 + MARGIN
            assert deleted == sorted(row.id for row in rows)
            async with sessions() as session:
                assert await ProxyRepository(session).get_many(deleted) == []
            assert failed_requests(logs) == ["sync"]
            assert log.pending == {ChangeKind.PROXY}
            await redis_client.client.client_unpause()
        finally:
            await log.close()
