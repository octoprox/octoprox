# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reputation in the workers' metrics jobs: the flush applies the requests and reports the site windows counted
(and a connector's with IPs requests from its host windows) to the connectors' records, in its transaction, and signals
once it committed a dynamic connector's factor that moved; compaction deletes records past their retention by the day
they were last seen.

Windows are written as gateways write them, and the jobs run on the real Postgres and Redis test containers.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.stats import RequestCount
from api.db.redis import RUNTIME_STREAM_KEY, MetricWindows, RedisClient
from api.db.repository import ProjectRepository
from api.db.reputation_repository import ExitReputationRepository
from api.db.streams import RuntimeSignal
from api.geo.runtime import GeoStack
from api.jobs.metrics import CompactionJob, MetricsFlushJob
from api.models.connector import Connector
from api.models.project import ExitReputationMode, Project
from api.reputation.record import (
    ConnectorSiteKey,
    ConnectorSiteReputation,
    ExitIpKey,
    ExitIpReputation,
    ReportCounts,
)
from tests.jobs.conftest import Rows
from tests.jobs.test_metrics import FIRST, SECOND, delivery, live, meter, recorded, requests

# A compaction run's one entry.
COMPACTION = delivery("1760000000000-0")


@pytest.fixture
def flush(sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient) -> MetricsFlushJob:
    return MetricsFlushJob(sessions, redis_client)


@pytest.fixture
async def project(rows: Rows) -> Project:
    return await rows.project(exit_reputation=ExitReputationMode.AVOID)


@pytest.fixture
async def dynamic(rows: Rows, project: Project) -> Connector:
    return await rows.connector(project)


@pytest.fixture
async def static(rows: Rows, project: Project) -> Connector:
    return await rows.connector(project)


def key(connector: Connector, place: str = "", site: str = "google.com") -> ConnectorSiteKey:
    return ConnectorSiteKey(connector.project_id, connector.id, site, place)


async def reported(sessions: async_sessionmaker[AsyncSession], at: datetime, ips: dict[ExitIpKey, ReportCounts]) -> None:
    """Reports applied to the exit IP records as the ingest applies them, at ``at``, committed."""
    async with sessions() as session:
        await ExitReputationRepository(session).apply_reports(ips, at)
        await session.commit()


async def flushed(
    sessions: async_sessionmaker[AsyncSession], at: datetime, evidence: dict[ConnectorSiteKey, RequestCount]
) -> None:
    """Evidence applied to the connector records as an earlier flush applied it, at ``at``, committed."""
    async with sessions() as session:
        await ExitReputationRepository(session).apply_evidence(evidence, (), at)
        await session.commit()


async def connector_record(sessions: async_sessionmaker[AsyncSession], k: ConnectorSiteKey) -> ConnectorSiteReputation | None:
    async with sessions() as session:
        return await ExitReputationRepository(session).connector_record(k)


async def ip_record(sessions: async_sessionmaker[AsyncSession], k: ExitIpKey) -> ExitIpReputation | None:
    async with sessions() as session:
        return await ExitReputationRepository(session).ip_record(k)


async def runtime(redis_client: RedisClient) -> list[dict[str, str]]:
    return [fields for _id, fields in await redis_client.client.xrange(RUNTIME_STREAM_KEY)]


class TestFlush:
    async def test_a_dynamic_connectors_requests_and_reports_reach_every_level_of_their_place(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        dynamic: Connector,
    ) -> None:
        """A report makes a record at every level of its place; the place's coarser levels sum its finer ones."""
        project_id, connector_id = dynamic.project_id, dynamic.id
        await meter(
            redis_client,
            MetricWindows(
                site_requests={
                    (project_id, connector_id, "google.com", "US/NY/new_york"): RequestCount(20, blocked=1),
                    (project_id, connector_id, "google.com", "US/NY"): RequestCount(5),
                    # No report made a record at the city, so its requests count at the country alone.
                    (project_id, connector_id, "google.com", "US//austin"): RequestCount(10),
                    (project_id, connector_id, "google.com", ""): RequestCount(30, blocked=1),
                    # No report made a record here: nothing is kept for it.
                    (project_id, connector_id, "bing.com", "US"): RequestCount(100),
                },
                # The host windows count the same requests whatever their place: a dynamic connector takes none.
                hosts={(project_id, connector_id, "google.com"): requests(80)},
            ),
        )

        await flush.run(delivery(FIRST))

        counted = {}
        for place in ("US/NY/new_york", "US/NY", "US", ""):
            stored = await connector_record(sessions, key(dynamic, place))
            assert stored is not None
            counted[place] = (stored.requests, stored.blocked_reports, stored.factor)
        assert counted == {
            "US/NY/new_york": (20, 1, 0.95),
            "US/NY": (25, 1, 0.95),
            "US": (35, 1, 0.95),
            "": (30, 1, 0.95),
        }
        for place, site in (("US//austin", "google.com"), ("US", "bing.com")):
            assert await connector_record(sessions, key(dynamic, place, site)) is None
        assert FIRST in await recorded(sessions)
        # Four factors moved in a project that avoids: one entry naming it, after the commit.
        assert await runtime(redis_client) == [{"kind": RuntimeSignal.REPUTATION.value, "ref": dynamic.project_id}]

    async def test_a_window_with_reports_alone_flushes(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        dynamic: Connector,
    ) -> None:
        """Reports the ingest added to a window no request reached this minute."""
        await redis_client.add_site_reports({key(dynamic, "GB"): RequestCount(blocked=2, good=1)})

        await flush.run(delivery(FIRST))

        stored = await connector_record(sessions, key(dynamic, "GB"))
        assert stored is not None
        assert (stored.blocked_reports, stored.good_reports, stored.requests, stored.factor) == (2, 1, 0, None)
        assert FIRST in await recorded(sessions)

    async def test_a_connector_with_ips_takes_its_requests_from_the_host_windows_at_no_place(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        static: Connector,
    ) -> None:
        """Its reports come through the site windows at no place, like any connector's; its factor routes nothing."""
        await meter(
            redis_client,
            MetricWindows(
                site_requests={(static.project_id, static.id, "google.com", ""): RequestCount(blocked=2)},
                hosts={
                    (static.project_id, static.id, "google.com"): requests(40),
                    # The hosts past the busiest, folded together: no site.
                    (static.project_id, static.id, "(other)"): requests(500),
                },
            ),
        )

        await flush.run(delivery(FIRST))

        stored = await connector_record(sessions, key(static))
        assert stored is not None and (stored.requests, stored.blocked_reports, stored.factor) == (40, 2, 0.95)
        assert await runtime(redis_client) == []

    async def test_a_dynamic_connector_takes_nothing_from_the_host_windows_in_a_minute_without_site_requests(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        rows: Rows,
        project: Project,
    ) -> None:
        """Its tunnel devices' requests, which no site window counts, reach the host windows alone, and its reports a
        window of no request: the flush tells the connector is dynamic by its rows, not by that minute's windows."""
        dynamic = await rows.connector(project)
        await rows.proxy(dynamic, metadata={"dynamic_sessions": "true"})
        await flushed(sessions, utc_now(), {key(dynamic): RequestCount(blocked=1)})
        await meter(
            redis_client,
            MetricWindows(
                site_requests={(dynamic.project_id, dynamic.id, "google.com", ""): RequestCount(blocked=1)},
                hosts={(dynamic.project_id, dynamic.id, "google.com"): requests(80)},
            ),
        )

        await flush.run(delivery(FIRST))

        stored = await connector_record(sessions, key(dynamic))
        assert stored is not None and (stored.requests, stored.blocked_reports) == (0, 2)
        assert FIRST in await recorded(sessions)

    async def test_a_project_that_records_nothing_adds_nothing(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        rows: Rows,
    ) -> None:
        project = await rows.project(exit_reputation=ExitReputationMode.RECORD)
        connector = await rows.connector(project)
        await flushed(sessions, utc_now(), {key(connector): RequestCount(blocked=1)})
        project.exit_reputation = ExitReputationMode.OFF
        async with sessions() as session:
            await ProjectRepository(session).update(project)
            await session.commit()
        await meter(
            redis_client,
            MetricWindows(site_requests={(project.id, connector.id, "google.com", ""): RequestCount(50, blocked=1)}),
        )

        await flush.run(delivery(FIRST))

        stored = await connector_record(sessions, key(connector))
        assert stored is not None and (stored.requests, stored.blocked_reports) == (0, 1)
        assert await runtime(redis_client) == []

    async def test_a_factor_that_stays_sends_no_signal(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        dynamic: Connector,
    ) -> None:
        await flushed(sessions, utc_now(), {key(dynamic): RequestCount(blocked=1)})
        windows = MetricWindows(site_requests={(dynamic.project_id, dynamic.id, "google.com", ""): RequestCount(100)})
        await meter(redis_client, windows)
        await flush.run(delivery(FIRST))
        await redis_client.client.delete(RUNTIME_STREAM_KEY)

        await meter(redis_client, windows)
        await flush.run(delivery(SECOND))

        stored = await connector_record(sessions, key(dynamic))
        assert stored is not None and (stored.requests, stored.factor) == (200, 1.0)
        assert await runtime(redis_client) == []

    async def test_only_dynamic_connectors_signal_one_entry_per_project(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        rows: Rows,
    ) -> None:
        """A connector with IPs is routed by its usable IPs: its factor moving moves no routing."""
        first, second, third = [await rows.project(exit_reputation=ExitReputationMode.AVOID) for _ in range(3)]
        dynamic = [await rows.connector(project) for project in (first, second)]
        for connector in dynamic:
            await rows.proxy(connector, metadata={"dynamic_sessions": "true"})
        static = await rows.connector(third)
        await rows.proxy(static)
        half = RequestCount(20, blocked=10)
        await meter(
            redis_client,
            MetricWindows(
                site_requests={
                    **{(c.project_id, c.id, "google.com", "US"): half for c in dynamic},
                    (static.project_id, static.id, "google.com", ""): RequestCount(blocked=10),
                },
                hosts={(static.project_id, static.id, "google.com"): requests(20)},
            ),
        )

        await flush.run(delivery(FIRST))

        stored = await connector_record(sessions, key(static))
        assert stored is not None and stored.factor == 0.5
        assert sorted(entry["ref"] for entry in await runtime(redis_client)) == sorted([first.id, second.id])

    async def test_gateways_and_reports_add_up_in_redis_before_the_flush(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        dynamic: Connector,
    ) -> None:
        """Gateways counting the same connector, site and place, and the reports taken there, write one window: one
        record update per flush."""
        entity = (dynamic.project_id, dynamic.id, "google.com", "GB")
        for count in (12, 18):
            await meter(redis_client, MetricWindows(site_requests={entity: RequestCount(count)}))
        await redis_client.add_site_reports({entity: RequestCount(blocked=1)})

        assert (await live(redis_client)).site_requests == {entity: RequestCount(30, blocked=1)}
        await flush.run(delivery(FIRST))
        stored = await connector_record(sessions, key(dynamic, "GB"))
        assert stored is not None and (stored.requests, stored.blocked_reports) == (30, 1)


class TestRetention:
    @pytest.fixture
    def compaction(self, sessions: async_sessionmaker[AsyncSession], geo: GeoStack) -> Any:
        def build(days: int) -> CompactionJob:
            return CompactionJob(sessions, geo.geo_service, reputation_retention_days=days)

        return build

    async def test_records_seen_before_the_retention_go_unless_an_avoid_is_ahead(
        self,
        compaction: Any,
        sessions: async_sessionmaker[AsyncSession],
        project: Project,
        static: Connector,
        rows: Rows,
    ) -> None:
        now = utc_now()
        stale, fresh = (ExitIpKey(project.id, f"203.0.113.{n}", "google.com") for n in (1, 2))
        await reported(sessions, now - timedelta(days=3), {stale: ReportCounts(bad=1)})
        await flushed(sessions, now - timedelta(days=3), {key(static): RequestCount(blocked=1)})
        await reported(sessions, now - timedelta(hours=1), {fresh: ReportCounts(good=1)})
        # A week-long avoid begun three days ago stays.
        lasting_project = await rows.project(exit_reputation=ExitReputationMode.AVOID, exit_reputation_avoid_minutes=10_080)
        lasting = ExitIpKey(lasting_project.id, "203.0.113.3", "google.com")
        await reported(sessions, now - timedelta(days=3), {lasting: ReportCounts(bad=1)})

        with structlog.testing.capture_logs() as logs:
            await compaction(1).run(COMPACTION)

        assert await ip_record(sessions, stale) is None and await connector_record(sessions, key(static)) is None
        assert await ip_record(sessions, fresh) is not None and await ip_record(sessions, lasting) is not None
        done = [log for log in logs if log["event"] == "Metrics compaction complete"]
        assert [(log["exit_ip_reputation_deleted"], log["connector_reputation_deleted"]) for log in done] == [(1, 1)]

    async def test_a_retention_of_zero_days_keeps_them_all(
        self, compaction: Any, sessions: async_sessionmaker[AsyncSession], project: Project
    ) -> None:
        old = ExitIpKey(project.id, "203.0.113.7", "google.com")
        await reported(sessions, utc_now() - timedelta(days=400), {old: ReportCounts(good=1)})

        await compaction(0).run(COMPACTION)

        assert await ip_record(sessions, old) is not None
