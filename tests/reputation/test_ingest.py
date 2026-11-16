# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The workers' exit report ingest: a batch's exit IP reports folded to one count per record and applied in one
transaction, its connector reports added to the site windows the metrics flush applies, and a runtime signal after the
commit when an avoid moved; on the test Postgres and Redis.

A malformed entry is skipped with its batch, and a project that is gone or off records nothing and counts nothing.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.job_stats import job_stats
from api.core.stats import RequestCount, SitePlaceKey
from api.db.redis import EXIT_REPORT_STREAM_KEY, RUNTIME_STREAM_KEY, RedisClient
from api.db.reputation_repository import ExitReputationRepository
from api.db.streams import RuntimeSignal, StreamEntry
from api.jobs.messages import ExitReportMessage
from api.models.connector import Connector
from api.models.exit_reports import ExitVerdict
from api.models.project import ExitReputationMode, Project
from api.reputation.ingest import ExitReportCount, ExitReportIngest
from api.reputation.record import ConnectorSiteKey, ExitIpKey, ReportCounts
from tests.reputation.world import make_connector, make_project
from tests.waits import running, until

T0 = datetime(2026, 11, 15, 12, 0, 0)


@pytest.fixture(autouse=True)
def _clean_job_stats() -> Iterator[None]:
    job_stats.reset()
    yield
    job_stats.reset()


def _report(project: Project, verdict: ExitVerdict = ExitVerdict.BAD, **fields: Any) -> ExitReportMessage:
    values: dict[str, Any] = {
        "project_id": project.id,
        "ip": "203.0.113.7",
        "site": "google.com",
        "verdict": verdict,
        "reported_at": T0,
    }
    values.update(fields)
    return ExitReportMessage(**values)


def _entries(*items: ExitReportMessage | str) -> list[StreamEntry]:
    """Stream entries carrying ``items`` (a str as it is), as a read of the reports stream hands them over."""
    return [
        StreamEntry(f"{n + 1}-0", {"item": item if isinstance(item, str) else item.model_dump_json()})
        for n, item in enumerate(items)
    ]


@pytest.fixture
def ingest(redis_client: RedisClient, db_session_factory: async_sessionmaker[AsyncSession]) -> ExitReportIngest:
    return ExitReportIngest(redis_client, db_session_factory)


@pytest.fixture
async def project(db_session: AsyncSession) -> Project:
    return await make_project(db_session, "acme", ExitReputationMode.AVOID)


@pytest.fixture
async def connector(db_session: AsyncSession, project: Project) -> Connector:
    return await make_connector(db_session, project, "Residential")


async def _ip(session_factory: async_sessionmaker[AsyncSession], key: ExitIpKey) -> Any:
    async with session_factory() as session:
        return await ExitReputationRepository(session).ip_record(key)


async def _connector(session_factory: async_sessionmaker[AsyncSession], key: ConnectorSiteKey) -> Any:
    async with session_factory() as session:
        return await ExitReputationRepository(session).connector_record(key)


async def _runtime(redis_client: RedisClient) -> list[dict[str, str]]:
    return [fields for _id, fields in await redis_client.client.xrange(RUNTIME_STREAM_KEY)]


async def _windows(redis_client: RedisClient) -> dict[SitePlaceKey, RequestCount]:
    """The site windows the next metrics flush would take."""
    return (await redis_client.take_metric_windows("check")).site_requests


class TestMessage:
    def test_it_round_trips_and_names_what_it_counts_for(self) -> None:
        project = Project(id="p", name="acme", username="acme", password="pw")
        message = _report(project, ExitVerdict.GOOD, reason="ok", connector_id="c", place="US/NY/new_york", token_id="t")

        assert ExitReportMessage.model_validate_json(message.model_dump_json()) == message
        assert message.ip_key == ExitIpKey("p", "203.0.113.7", "google.com")
        assert message.connector_key == ConnectorSiteKey("p", "c", "google.com", "US/NY/new_york")
        assert message.counts == ReportCounts(good=1, reason="ok")
        assert _report(project).counts == ReportCounts(bad=1)
        # A dynamic connector's report names no IP; one naming no connector counts for the IP alone.
        assert _report(project, ip=None, connector_id="c").ip_key is None
        assert _report(project).connector_key is None


class TestApply:
    async def test_a_batch_folds_to_one_count_per_record(
        self, ingest: ExitReportIngest, project: Project, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await ingest._handle(
            _entries(
                # Out of order: the record keeps the reason of the latest report made.
                _report(project, reason="banned", reported_at=T0 + timedelta(seconds=2)),
                _report(project, reason="captcha", reported_at=T0),
                _report(project, ExitVerdict.GOOD, ip="203.0.113.8"),
                _report(project, site="bing.com"),
            )
        )

        google = await _ip(db_session_factory, ExitIpKey(project.id, "203.0.113.7", "google.com"))
        assert (google.bad_reports, google.good_reports, google.last_reason) == (2, 0, "banned")
        assert google.avoid_until is not None and google.avoid_until > utc_now()
        good = await _ip(db_session_factory, ExitIpKey(project.id, "203.0.113.8", "google.com"))
        assert (good.good_reports, good.bad_reports, good.avoid_until) == (1, 0, None)
        bing = await _ip(db_session_factory, ExitIpKey(project.id, "203.0.113.7", "bing.com"))
        assert bing.bad_reports == 1

    async def test_a_connectors_reports_go_to_its_site_window_at_the_finest_level_of_its_place(
        self,
        ingest: ExitReportIngest,
        project: Project,
        connector: Connector,
        redis_client: RedisClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A dynamic connector's report names no IP: the metrics flush applies it to the connector's records, at every
        level of its place, with the minute's requests."""
        await ingest._handle(
            _entries(
                _report(project, ip=None, connector_id=connector.id, place="US/NY/new_york"),
                _report(project, ip=None, connector_id=connector.id, place="US"),
                _report(project, ip=None, connector_id=connector.id, place="US"),
                _report(project, ExitVerdict.GOOD, ip=None, connector_id=connector.id, place=""),
            )
        )

        assert await _windows(redis_client) == {
            (project.id, connector.id, "google.com", "US/NY/new_york"): RequestCount(blocked=1),
            (project.id, connector.id, "google.com", "US"): RequestCount(blocked=2),
            (project.id, connector.id, "google.com", ""): RequestCount(good=1),
        }
        # Nothing reaches Postgres before the flush.
        assert await _connector(db_session_factory, ConnectorSiteKey(project.id, connector.id, "google.com", "US")) is None
        assert await _ip(db_session_factory, ExitIpKey(project.id, "203.0.113.7", "google.com")) is None
        assert await _runtime(redis_client) == []

    async def test_a_report_with_an_ip_and_a_connector_counts_for_both(
        self,
        ingest: ExitReportIngest,
        project: Project,
        connector: Connector,
        redis_client: RedisClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await ingest._handle(_entries(_report(project, connector_id=connector.id)))

        assert (await _ip(db_session_factory, ExitIpKey(project.id, "203.0.113.7", "google.com"))).bad_reports == 1
        assert await _windows(redis_client) == {
            (project.id, connector.id, "google.com", ""): RequestCount(blocked=1)
        }

    async def test_a_connector_report_is_taken_only_where_the_project_records_and_owns_the_connector(
        self,
        ingest: ExitReportIngest,
        project: Project,
        connector: Connector,
        db_session: AsyncSession,
        redis_client: RedisClient,
    ) -> None:
        off = await make_project(db_session, "switched-off", ExitReputationMode.OFF)
        off_connector = await make_connector(db_session, off, "Off")
        other = await make_project(db_session, "other")
        elsewhere = await make_connector(db_session, other, "Elsewhere")

        await ingest._handle(
            _entries(
                _report(off, ip=None, connector_id=off_connector.id),
                _report(project, ip=None, connector_id=elsewhere.id),
                _report(project, ip=None, connector_id="deleted"),
                _report(project, ip=None, connector_id=connector.id),
            )
        )

        assert await _windows(redis_client) == {(project.id, connector.id, "google.com", ""): RequestCount(blocked=1)}
        assert ingest.counts.snapshot() == [ExitReportCount(connector.id, "Residential", "bad", 1)]

    async def test_the_windows_follow_the_commit_and_the_signal_follows_them(
        self,
        ingest: ExitReportIngest,
        project: Project,
        connector: Connector,
        redis_client: RedisClient,
        db_session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: list[Any] = []
        add, send = ingest._redis.add_site_reports, ingest._signal.send

        async def adding(counts: Any) -> None:
            seen.append(("windows", await _ip(db_session_factory, ExitIpKey(project.id, "203.0.113.7", "google.com"))))
            await add(counts)

        async def sending(changes: Any) -> None:
            seen.append(("signal", len(await _windows(redis_client))))
            await send(changes)

        monkeypatch.setattr(ingest._redis, "add_site_reports", adding)
        monkeypatch.setattr(ingest._signal, "send", sending)

        await ingest._handle(_entries(_report(project, connector_id=connector.id)))

        assert [step for step, _seen in seen] == ["windows", "signal"]
        assert seen[0][1] is not None and seen[0][1].bad_reports == 1
        assert seen[1][1] == 1
        assert await _runtime(redis_client) == [{"kind": RuntimeSignal.REPUTATION.value, "ref": project.id}]

    async def test_windows_redis_refuses_leave_the_batch_pending_and_the_avoid_signalled(
        self,
        ingest: ExitReportIngest,
        project: Project,
        connector: Connector,
        redis_client: RedisClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The batch is delivered again, its exit IP reports counted twice; the controller hears of the avoid now."""

        async def refused(_counts: Any) -> None:
            raise ConnectionError("Redis went away")

        monkeypatch.setattr(ingest._redis, "add_site_reports", refused)

        with pytest.raises(ConnectionError):
            await ingest._handle(_entries(_report(project, connector_id=connector.id)))

        assert await _runtime(redis_client) == [{"kind": RuntimeSignal.REPUTATION.value, "ref": project.id}]
        assert ingest.counts.snapshot() == []

    async def test_an_avoid_begun_is_signalled_after_the_commit(
        self,
        ingest: ExitReportIngest,
        project: Project,
        redis_client: RedisClient,
        db_session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The controller reads only what committed, so the signal follows the commit."""
        key = ExitIpKey(project.id, "203.0.113.7", "google.com")
        seen: list[Any] = []
        send = ingest._signal.send

        async def send_seeing(changes: Any) -> None:
            seen.append(await _ip(db_session_factory, key))
            await send(changes)

        monkeypatch.setattr(ingest._signal, "send", send_seeing)

        await ingest._handle(_entries(_report(project)))

        assert seen[0] is not None and seen[0].bad_reports == 1
        assert await _runtime(redis_client) == [{"kind": RuntimeSignal.REPUTATION.value, "ref": project.id}]

    async def test_no_signal_when_nothing_routing_uses_moved(
        self, ingest: ExitReportIngest, db_session: AsyncSession, redis_client: RedisClient
    ) -> None:
        """Ten fresh bad reports to avoid an exit: one decides nothing yet; and a project that records routes on none."""
        cautious = await make_project(db_session, "cautious", ExitReputationMode.AVOID, exit_reputation_bad_reports=10)
        recording = await make_project(db_session, "recording", ExitReputationMode.RECORD)

        await ingest._handle(_entries(_report(cautious), _report(recording)))

        assert (await ExitReputationRepository(db_session).ip_record(
            ExitIpKey(recording.id, "203.0.113.7", "google.com")
        )).avoid_until is not None
        assert await _runtime(redis_client) == []

    async def test_a_signal_redis_refuses_fails_nothing(
        self,
        ingest: ExitReportIngest,
        project: Project,
        db_session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The records committed; raising would apply the batch again."""

        def refused(*_args: Any, **_kwargs: Any) -> Any:
            raise ConnectionError("Redis went away")

        monkeypatch.setattr(ingest._signal._redis.client, "pipeline", refused)

        await ingest._handle(_entries(_report(project)))

        assert (await _ip(db_session_factory, ExitIpKey(project.id, "203.0.113.7", "google.com"))).bad_reports == 1

    async def test_malformed_entries_are_skipped_and_the_rest_applied(
        self, ingest: ExitReportIngest, project: Project, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await ingest._handle(_entries("not json", '{"project_id": "p"}', _report(project), ""))

        record = await _ip(db_session_factory, ExitIpKey(project.id, "203.0.113.7", "google.com"))
        assert record.bad_reports == 1

    async def test_a_project_off_or_gone_records_and_counts_nothing(
        self,
        ingest: ExitReportIngest,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        off = await make_project(db_session, "switched-off", ExitReputationMode.OFF)
        off_connector = await make_connector(db_session, off)
        gone = Project(id="gone", name="gone", username="gone", password="pw")

        await ingest._handle(
            _entries(_report(off, connector_id=off_connector.id), _report(gone, ip=None, connector_id="c"))
        )

        assert await _ip(db_session_factory, ExitIpKey(off.id, "203.0.113.7", "google.com")) is None
        assert await _windows(redis_client) == {}
        assert ingest.counts.snapshot() == []
        assert await _runtime(redis_client) == []


class TestCounts:
    async def test_applied_reports_are_counted_by_connector_and_verdict(
        self, ingest: ExitReportIngest, project: Project, connector: Connector
    ) -> None:
        await ingest._handle(
            _entries(
                _report(project, connector_id=connector.id),
                _report(project, ExitVerdict.GOOD, ip="203.0.113.8", connector_id=connector.id),
                _report(project, ExitVerdict.GOOD, ip=None, connector_id=connector.id, place="US"),
                _report(project, ip="203.0.113.10"),
                # A connector deleted since the report was taken: its IP's record counts it, under the id alone.
                _report(project, ip="203.0.113.11", connector_id="deleted"),
                # Nothing left to count it for.
                _report(project, ip=None, connector_id="deleted"),
            )
        )

        # By connector id, then verdict; the connector's id is random, so it may sort either side of "deleted".
        assert ingest.counts.snapshot() == sorted(
            [
                ExitReportCount("", "", "bad", 1),
                ExitReportCount(connector.id, "Residential", "bad", 1),
                ExitReportCount(connector.id, "Residential", "good", 2),
                ExitReportCount("deleted", "", "bad", 1),
            ],
            key=lambda count: (count.connector_id, count.verdict),
        )


class TestStream:
    async def test_reports_appended_are_applied_and_the_entries_completed(
        self,
        ingest: ExitReportIngest,
        project: Project,
        redis_client: RedisClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        for report in (_report(project), _report(project, ip="203.0.113.8")):
            await redis_client.client.xadd(EXIT_REPORT_STREAM_KEY, {"item": report.model_dump_json()})

        async with running(ingest):
            await until(lambda: ingest.consumed == 2)

        assert await redis_client.client.xlen(EXIT_REPORT_STREAM_KEY) == 0
        for ip in ("203.0.113.7", "203.0.113.8"):
            record = await _ip(db_session_factory, ExitIpKey(project.id, ip, "google.com"))
            assert record is not None and record.bad_reports == 1
