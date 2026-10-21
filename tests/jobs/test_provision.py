# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the provision job: on-demand country slot groups of an "All countries" session pool.

The pool is the built-in Oxylabs descriptor, two slots per country, whose provisioning makes no vendor round trip.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.roles import JobKind
from api.db.redis import RedisClient
from api.jobs.provision import ProvisionJob, ProvisionTarget
from api.models.connector import Connector
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.providers.registry import ProviderRegistry
from api.providers.sdk.strategies import META_GEO_COUNTRY, META_SESSION_ID
from api.services.proxies import ProxyService
from tests.jobs.conftest import Rows, batch_of, waiting
from tests.providers.test_registry import fail_to_build

OXYLABS = {"proxy_type": "residential", "username": "alice", "password": "s3cret"}
POOL = {"num_proxies": 2, "session_duration_minutes": 10}


@pytest.fixture
def job(
    sessions: async_sessionmaker[AsyncSession], registry: ProviderRegistry, proxy_service: ProxyService
) -> ProvisionJob:
    return ProvisionJob(sessions, registry, proxy_service)


@pytest.fixture
async def project(rows: Rows) -> Project:
    return await rows.project()


@pytest.fixture
async def pool(rows: Rows, project: Project) -> Connector:
    return await rows.connector(project, "oxylabs", credential=OXYLABS, config=POOL)


def _group(proxies: list[Proxy], country: str) -> list[Proxy]:
    return [proxy for proxy in proxies if proxy.metadata.get(META_GEO_COUNTRY) == country]


class TestProvision:
    async def test_a_missing_group_is_added_with_fresh_sessions(
        self, job: ProvisionJob, rows: Rows, pool: Connector, redis_client: RedisClient
    ) -> None:
        assert ProvisionTarget(pool.id, " de ").key == f"{pool.id}:DE"

        await job.run(batch_of(ProvisionTarget(pool.id, "de").key))

        group = _group(await rows.of(pool), "DE")
        assert len(group) == 2 and all(slot.status == ProxyStatus.HEALTHY for slot in group)
        assert len({slot.metadata[META_SESSION_ID] for slot in group}) == 2
        # Session slots are healthy as built: nothing to check first.
        assert await waiting(redis_client, "health") == set()

    async def test_a_group_that_exists_is_left_alone_whatever_its_state(
        self, job: ProvisionJob, rows: Rows, pool: Connector
    ) -> None:
        retired = await rows.proxy(
            pool, status=ProxyStatus.UNHEALTHY, metadata={META_GEO_COUNTRY: "DE", META_SESSION_ID: "x"}
        )

        await job.run(batch_of(ProvisionTarget(pool.id, "DE").key))

        assert [proxy.id for proxy in await rows.of(pool)] == [retired.id]

    async def test_two_runs_at_once_add_one_group(
        self,
        sessions: async_sessionmaker[AsyncSession],
        registry: ProviderRegistry,
        proxy_service: ProxyService,
        rows: Rows,
        pool: Connector,
    ) -> None:
        """Two workers running the same key at once (a run-now and a redelivery) take turns on the connector's row."""
        first, second = ProvisionJob(sessions, registry, proxy_service), ProvisionJob(sessions, registry, proxy_service)

        added = await asyncio.gather(first.provision(pool.id, "FR"), second.provision(pool.id, "FR"))

        assert sorted(len(group) for group in added) == [0, 2]
        assert len(_group(await rows.of(pool), "FR")) == 2

    async def test_groups_of_different_countries_are_both_added(
        self, job: ProvisionJob, rows: Rows, pool: Connector
    ) -> None:
        await asyncio.gather(job.provision(pool.id, "FR"), job.provision(pool.id, "IT"))
        proxies = await rows.of(pool)
        assert (len(_group(proxies, "FR")), len(_group(proxies, "IT"))) == (2, 2)

    async def test_a_group_emptied_by_rotation_comes_back_with_new_sessions(
        self, job: ProvisionJob, rows: Rows, pool: Connector, proxy_service: ProxyService
    ) -> None:
        await job.provision(pool.id, "DE")
        rotated = _group(await rows.of(pool), "DE")
        for slot in rotated:
            await proxy_service.delete(slot.id)

        await job.provision(pool.id, "DE")

        fresh = _group(await rows.of(pool), "DE")
        assert len(fresh) == 2
        assert {s.id for s in fresh}.isdisjoint(s.id for s in rotated)
        assert {s.metadata[META_SESSION_ID] for s in fresh}.isdisjoint(s.metadata[META_SESSION_ID] for s in rotated)


class TestWhatIsProvisioned:
    def test_a_worker_provisions_four_groups_at_once_one_a_run(self) -> None:
        assert (ProvisionJob.kind, ProvisionJob.batch_size, ProvisionJob.concurrency) == (JobKind.PROVISION, 1, 4)

    async def test_nothing_for_a_connector_that_is_gone_off_or_not_an_all_countries_pool(
        self, job: ProvisionJob, rows: Rows, project: Project
    ) -> None:
        disabled = await rows.connector(project, "oxylabs", credential=OXYLABS, config=POOL, enabled=False)
        deleting = await rows.connector(project, "oxylabs", credential=OXYLABS, config=POOL, pending_deletion=True)
        dynamic = await rows.connector(
            project, "oxylabs", credential=OXYLABS, config={"session_mode": "dynamic", "session_duration_minutes": 10}
        )
        static = await rows.connector(project)

        for connector_id in ("gone", disabled.id, deleting.id, dynamic.id, static.id):
            assert await job.provision(connector_id, "DE") == []
        for connector in (disabled, deleting, dynamic, static):
            assert await rows.of(connector) == []

    async def test_a_pool_that_lists_its_countries_provisions_no_other(
        self, job: ProvisionJob, rows: Rows, project: Project
    ) -> None:
        listed = await rows.connector(project, "oxylabs", credential=OXYLABS, config={**POOL, "country_code": ["US"]})

        with structlog.testing.capture_logs() as logs:
            assert await job.provision(listed.id, "DE") == []

        assert await rows.of(listed) == []
        refused = "Country slot group asked of a connector that provisions none"
        assert [log["connector_id"] for log in logs if log["event"] == refused] == [listed.id]

    async def test_a_provider_that_cannot_be_built_fails_the_run(
        self, job: ProvisionJob, rows: Rows, pool: Connector, registry: ProviderRegistry
    ) -> None:
        """The entry is delivered again, rather than acknowledged with no group."""
        fail_to_build(registry, "oxylabs", RuntimeError("the vendor SDK is broken"))

        with pytest.raises(RuntimeError, match="the vendor SDK is broken"):
            await job.run(batch_of(ProvisionTarget(pool.id, "DE").key))

        assert await rows.of(pool) == []

    async def test_an_unreadable_key_is_logged_and_skipped(self, job: ProvisionJob) -> None:
        with structlog.testing.capture_logs() as logs:
            await job.run(batch_of("no-country:", "DE"))

        dropped = [(log["log_level"], log["key"]) for log in logs if log["event"] == "Unreadable provision key dropped"]
        assert dropped == [("warning", "no-country:"), ("warning", "DE")]

    async def test_a_key_whose_country_is_no_two_letter_code_adds_no_group(
        self, job: ProvisionJob, rows: Rows, pool: Connector
    ) -> None:
        """Keys come from Redis: whatever writes them can add at most one group per code, never any it likes."""
        bogus = [f"{pool.id}:X1", f"{pool.id}:GBR", f"{pool.id}:D-E", f"{pool.id}: "]

        await job.run(batch_of(*bogus, f"{pool.id}:uk"))

        stored = await rows.of(pool)
        assert {proxy.metadata.get(META_GEO_COUNTRY) for proxy in stored} == {"GB"}
