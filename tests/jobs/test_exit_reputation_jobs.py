# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reputation in the workers' metrics jobs: the flush applies the requests and reports the site windows counted
(and a connector's with IPs requests from its host windows) to the connectors' records of the projects that record, in
its transaction, and signals once it committed a dynamic connector's factor that moved; while no project records it
reads nothing past which. Compaction deletes records past their project's Keep data for by the day they were last
seen, and their history past the same days by when it was made.

Windows are written as gateways write them, and the jobs run on the real Postgres and Redis test containers.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import datetime, timedelta

import pytest
import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.stats import RequestCount
from api.db.models import ConnectorFactorMoveModel, ExitIpAvoidMoveModel
from api.db.redis import RUNTIME_STREAM_KEY, MetricWindows, RedisClient
from api.db.repository import ProjectRepository, ProxyRepository
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
    ReputationChanges,
)
from tests.jobs.conftest import Rows
from tests.jobs.test_metrics import (
    FIRST,
    SECOND,
    delivery,
    failing,
    live,
    meter,
    recorded,
    requests,
)

# A compaction run's one entry.
COMPACTION = delivery("1760000000000-0")


@pytest.fixture
def flush(sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient) -> MetricsFlushJob:
    return MetricsFlushJob(sessions, redis_client, timedelta(minutes=1))


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
        await ExitReputationRepository(session).apply_evidence(evidence, (), at, timedelta(minutes=1))
        await session.commit()


async def connector_record(sessions: async_sessionmaker[AsyncSession], k: ConnectorSiteKey) -> ConnectorSiteReputation | None:
    async with sessions() as session:
        return await ExitReputationRepository(session).connector_record(k)


async def ip_record(sessions: async_sessionmaker[AsyncSession], k: ExitIpKey) -> ExitIpReputation | None:
    async with sessions() as session:
        return await ExitReputationRepository(session).ip_record(k)


async def runtime(redis_client: RedisClient) -> list[dict[str, str]]:
    return [fields for _id, fields in await redis_client.client.xrange(RUNTIME_STREAM_KEY)]


class Weighed:
    """What the flush asks of the rows and records: the connectors it asks about, and the keys it weighs evidence for."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.asked: list[set[str]] = []
        self.keys: list[set[ConnectorSiteKey]] = []
        dynamic_connectors, apply_evidence = ProxyRepository.dynamic_connectors, ExitReputationRepository.apply_evidence

        async def asking(repository: ProxyRepository, connector_ids: Iterable[str]) -> set[str]:
            wanted = set(connector_ids)
            self.asked.append(wanted)
            return await dynamic_connectors(repository, wanted)

        async def weighing(
            repository: ExitReputationRepository,
            evidence: Mapping[ConnectorSiteKey, RequestCount],
            dynamic: Collection[str],
            now: datetime,
            window: timedelta,
        ) -> ReputationChanges:
            self.keys.append(set(evidence))
            return await apply_evidence(repository, evidence, dynamic, now, window)

        monkeypatch.setattr(ProxyRepository, "dynamic_connectors", asking)
        monkeypatch.setattr(ExitReputationRepository, "apply_evidence", weighing)


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

    async def test_while_no_project_records_the_flush_weighs_nothing(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        rows: Rows,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One small read of which projects record, then neither the rows nor the records."""
        project = await rows.project(exit_reputation=ExitReputationMode.OFF)
        connector = await rows.connector(project)
        weighed = Weighed(monkeypatch)
        await meter(
            redis_client,
            MetricWindows(
                site_requests={(project.id, connector.id, "google.com", ""): RequestCount(50, blocked=1)},
                hosts={(project.id, connector.id, "google.com"): requests(50)},
            ),
        )

        await flush.run(delivery(FIRST))

        assert (weighed.asked, weighed.keys) == ([], [])
        assert await connector_record(sessions, key(connector)) is None
        # The rest of the flush committed, and its windows were cleared.
        assert FIRST in await recorded(sessions)
        assert (await live(redis_client)).site_requests == {}

    async def test_only_the_keys_of_the_projects_that_record_are_weighed(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        rows: Rows,
        static: Connector,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Another project's windows are left out before the rows are asked which connectors are dynamic."""
        off = await rows.project(exit_reputation=ExitReputationMode.OFF)
        off_connector = await rows.connector(off)
        weighed = Weighed(monkeypatch)
        await meter(
            redis_client,
            MetricWindows(
                site_requests={
                    (static.project_id, static.id, "google.com", "GB"): RequestCount(blocked=1),
                    (off.id, off_connector.id, "google.com", "GB"): RequestCount(blocked=1),
                },
                hosts={
                    (static.project_id, static.id, "google.com"): requests(10),
                    (off.id, off_connector.id, "google.com"): requests(10),
                },
            ),
        )

        await flush.run(delivery(FIRST))

        assert weighed.asked == [{static.id}]
        assert weighed.keys == [{key(static, "GB"), key(static)}]
        stored = await connector_record(sessions, key(static, "GB"))
        assert stored is not None and stored.blocked_reports == 1

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
    """The records go by each project's own Keep data for, whatever the other projects keep."""

    @pytest.fixture
    def compaction(self, sessions: async_sessionmaker[AsyncSession], geo: GeoStack) -> CompactionJob:
        return CompactionJob(sessions, geo.geo_service)

    async def test_each_projects_records_go_by_its_own_days_unless_an_avoid_is_ahead(
        self, compaction: CompactionJob, sessions: async_sessionmaker[AsyncSession], rows: Rows
    ) -> None:
        """One project keeps 30 days, another 1 and a third forever (0)."""
        now = utc_now()
        month = await rows.project(exit_reputation=ExitReputationMode.AVOID)
        # Its first avoid lasts a week.
        day = await rows.project(
            exit_reputation=ExitReputationMode.AVOID, exit_reputation_retention_days=1, exit_reputation_avoid_minutes=10_080
        )
        forever = await rows.project(exit_reputation=ExitReputationMode.AVOID, exit_reputation_retention_days=0)
        of_month, of_day, of_forever = [await rows.connector(project) for project in (month, day, forever)]
        month_stale, month_fresh, day_stale, day_avoided, day_fresh, forever_old = (
            ExitIpKey(project.id, f"203.0.113.{n}", "google.com")
            for n, project in enumerate((month, month, day, day, day, forever), start=1)
        )
        await reported(sessions, now - timedelta(days=40), {month_stale: ReportCounts(good=1)})
        await flushed(sessions, now - timedelta(days=40), {key(of_month): RequestCount(blocked=1)})
        await reported(sessions, now - timedelta(days=3), {month_fresh: ReportCounts(good=1)})
        await flushed(sessions, now - timedelta(days=3), {key(of_month, "US"): RequestCount(blocked=1)})
        await reported(
            sessions, now - timedelta(days=3), {day_stale: ReportCounts(good=1), day_avoided: ReportCounts(bad=1)}
        )
        await flushed(sessions, now - timedelta(days=3), {key(of_day): RequestCount(blocked=1)})
        await reported(sessions, now - timedelta(hours=1), {day_fresh: ReportCounts(good=1)})
        await flushed(sessions, now - timedelta(hours=1), {key(of_day, "US"): RequestCount(blocked=1)})
        await reported(sessions, now - timedelta(days=400), {forever_old: ReportCounts(good=1)})
        await flushed(sessions, now - timedelta(days=400), {key(of_forever): RequestCount(blocked=1)})

        with structlog.testing.capture_logs() as logs:
            await compaction.run(COMPACTION)

        for gone in (month_stale, day_stale):
            assert await ip_record(sessions, gone) is None
        for gone_place in (key(of_month), key(of_day)):
            assert await connector_record(sessions, gone_place) is None
        for kept in (month_fresh, day_avoided, day_fresh, forever_old):
            assert await ip_record(sessions, kept) is not None
        for kept_place in (key(of_month, "US"), key(of_day, "US"), key(of_forever)):
            assert await connector_record(sessions, kept_place) is not None
        done = [log for log in logs if log["event"] == "Metrics compaction complete"]
        assert [(log["exit_ip_reputation_deleted"], log["connector_reputation_deleted"]) for log in done] == [(2, 2)]
        # The same days take the history: the avoid kept 3 days ago goes, its record staying while the avoid lasts.
        assert [(log["factor_moves_deleted"], log["avoid_moves_deleted"]) for log in done] == [(0, 1)]
        assert await avoid_moves(sessions, day, day_avoided.ip) == []

    async def test_a_changed_setting_applies_at_the_next_sweep(
        self, compaction: CompactionJob, sessions: async_sessionmaker[AsyncSession], project: Project
    ) -> None:
        old = ExitIpKey(project.id, "203.0.113.7", "google.com")
        await reported(sessions, utc_now() - timedelta(days=10), {old: ReportCounts(good=1)})
        await compaction.run(COMPACTION)
        assert await ip_record(sessions, old) is not None

        async with sessions() as session:
            await ProjectRepository(session).update(project.model_copy(update={"exit_reputation_retention_days": 7}))
            await session.commit()
        await compaction.run(COMPACTION)

        assert await ip_record(sessions, old) is None


async def moved(sessions: async_sessionmaker[AsyncSession], *keys: ConnectorSiteKey) -> None:
    """A move of each of ``keys``' dynamic factors kept now, as a flush keeps them."""
    async with sessions() as session:
        await ExitReputationRepository(session).apply_evidence(
            {k: RequestCount(20, blocked=10) for k in keys}, {k.connector_id for k in keys}, utc_now(), timedelta(minutes=1)
        )
        await session.commit()


async def factor_moves(sessions: async_sessionmaker[AsyncSession], connector: Connector, *places: str) -> list[datetime]:
    """When each move of the connector's factors for google.com at ``places`` was made."""
    async with sessions() as session:
        moves = await ExitReputationRepository(session).factor_moves(
            connector.project_id, "google.com", places, [connector.id], datetime(2000, 1, 1)
        )
    return [move.at for move in moves]


class TestFactorMoves:
    async def test_a_flush_keeps_each_move_and_a_redelivered_one_keeps_none(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        dynamic: Connector,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The moves are written in the flush's transaction, which records the flush: a delivery of it again writes
        nothing."""
        entity = (dynamic.project_id, dynamic.id, "google.com", "US")
        await meter(redis_client, MetricWindows(site_requests={entity: RequestCount(20, blocked=10)}))
        # The commit lands, and the staging hash's delete does not.
        with monkeypatch.context() as patched:
            patched.setattr(redis_client, "clear_metric_windows", failing)
            with pytest.raises(ConnectionError):
                await flush.run(delivery(FIRST))
        [first] = await factor_moves(sessions, dynamic, "US")

        await flush.run(delivery(FIRST))

        assert await factor_moves(sessions, dynamic, "US") == [first]


async def avoid_moves(sessions: async_sessionmaker[AsyncSession], project: Project, *ips: str) -> list[datetime]:
    """When each move of ``ips``' avoids for google.com was made."""
    async with sessions() as session:
        moves = await ExitReputationRepository(session).avoid_moves(project.id, "google.com", ips, datetime(2000, 1, 1))
    return [move.at for move in moves]


class TestHistoryRetention:
    """The factor moves and the avoid moves go by the same Keep data for as the records, by when they were made,
    whatever day the records were last seen."""

    @pytest.fixture
    def compaction(self, sessions: async_sessionmaker[AsyncSession], geo: GeoStack) -> CompactionJob:
        return CompactionJob(sessions, geo.geo_service)

    @staticmethod
    async def aged(sessions: async_sessionmaker[AsyncSession], project: Project, days: int) -> None:
        """Every move of the project's, factors' and avoids', made ``days`` ago."""
        async with sessions() as session:
            for model in (ConnectorFactorMoveModel, ExitIpAvoidMoveModel):
                await session.execute(
                    update(model).where(model.project_id == project.id).values(at=utc_now() - timedelta(days=days))
                )
            await session.commit()

    @staticmethod
    async def history(
        sessions: async_sessionmaker[AsyncSession], rows: Rows, days: int, age: int
    ) -> tuple[Project, Connector]:
        """A project keeping ``days`` of data with a dynamic connector's move and an exit IP's avoid move made ``age``
        days ago, their records last seen today."""
        project = await rows.project(exit_reputation=ExitReputationMode.AVOID, exit_reputation_retention_days=days)
        connector = await rows.connector(project)
        await moved(sessions, key(connector, "US"))
        await reported(sessions, utc_now(), {ExitIpKey(project.id, "203.0.113.7", "google.com"): ReportCounts(bad=1)})
        await TestHistoryRetention.aged(sessions, project, age)
        return project, connector

    async def test_each_project_keeps_its_own_days(
        self, compaction: CompactionJob, sessions: async_sessionmaker[AsyncSession], rows: Rows
    ) -> None:
        day, day_connector = await self.history(sessions, rows, 1, 2)
        month, month_connector = await self.history(sessions, rows, 30, 8)
        forever, forever_connector = await self.history(sessions, rows, 0, 400)

        with structlog.testing.capture_logs() as logs:
            await compaction.run(COMPACTION)

        assert await factor_moves(sessions, day_connector, "US") == []
        assert await avoid_moves(sessions, day, "203.0.113.7") == []
        assert len(await factor_moves(sessions, month_connector, "US")) == 1
        assert len(await avoid_moves(sessions, month, "203.0.113.7")) == 1
        # 0 days keep them all.
        assert len(await factor_moves(sessions, forever_connector, "US")) == 1
        assert len(await avoid_moves(sessions, forever, "203.0.113.7")) == 1
        # The records stay: they were last seen today.
        assert await connector_record(sessions, key(day_connector, "US")) is not None
        done = [log for log in logs if log["event"] == "Metrics compaction complete"]
        deleted = ("factor_moves_deleted", "avoid_moves_deleted", "connector_reputation_deleted")
        assert [tuple(log[name] for name in deleted) for log in done] == [(1, 1, 0)]

    async def test_a_changed_setting_applies_at_the_next_sweep(
        self, compaction: CompactionJob, sessions: async_sessionmaker[AsyncSession], rows: Rows
    ) -> None:
        project, connector = await self.history(sessions, rows, 30, 8)
        await compaction.run(COMPACTION)
        assert len(await factor_moves(sessions, connector, "US")) == 1

        async with sessions() as session:
            repository = ProjectRepository(session)
            await repository.update(project.model_copy(update={"exit_reputation_retention_days": 7}))
            await session.commit()
        await compaction.run(COMPACTION)

        assert await factor_moves(sessions, connector, "US") == []
        assert await avoid_moves(sessions, project, "203.0.113.7") == []
