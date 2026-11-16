# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The workers' exit report ingest: a batch's reports checked against the project's rows, its exit IP reports folded
to one count per record and applied in one transaction, its connector reports added to the site windows the metrics
flush applies, and a runtime signal after the commit when an avoid moved; on the test Postgres and Redis.

A malformed entry is skipped with its batch. A report whose project is gone or off, or whose exit headers name no exit
of the project's rows, records nothing and is counted dropped, by reason.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.job_stats import job_stats
from api.core.stats import RequestCount, SitePlaceKey
from api.db.redis import EXIT_REPORT_STREAM_KEY, RUNTIME_STREAM_KEY, RedisClient
from api.db.repository import ProxyRepository
from api.db.reputation_repository import ExitReputationRepository
from api.db.streams import RuntimeSignal, StreamEntry
from api.jobs.messages import ExitReportMessage, NotedExit
from api.models.connector import Connector
from api.models.exit_reports import DropReason, ExitVerdict
from api.models.project import ExitReputationMode, Project
from api.models.proxy import Proxy, ProxyProtocol
from api.reputation.ingest import ExitReportCount, ExitReportCounts, ExitReportIngest
from api.reputation.record import ConnectorSiteKey, ExitIpKey, ReportCounts
from tests.jobs.conftest import counted_selects
from tests.reputation.world import make_connector, make_project
from tests.waits import running, until

T0 = datetime(2026, 11, 15, 12, 0, 0)


@pytest.fixture(autouse=True)
def _clean_job_stats() -> Iterator[None]:
    job_stats.reset()
    yield
    job_stats.reset()


def _report(project: Project, verdict: ExitVerdict = ExitVerdict.BAD, **fields: Any) -> ExitReportMessage:
    """A report as the api appends it, named by a tag's note unless ``proxy_id=None``: by its exit headers."""
    values: dict[str, Any] = {
        "project_id": project.id,
        "ip": "203.0.113.7",
        "site": "google.com",
        "verdict": verdict,
        "proxy_id": "row-1",
        "reported_at": T0,
    }
    values.update(fields)
    return ExitReportMessage(**values)


def _by_headers(
    project: Project, connector_id: str, verdict: ExitVerdict = ExitVerdict.BAD, **fields: Any
) -> ExitReportMessage:
    """A report named by its exit headers, as sent: no IP unless one is given, at the place the username names."""
    return _report(project, verdict, connector_id=connector_id, proxy_id=None, **{"ip": None, **fields})


def _dropped(**reasons: int) -> list[ExitReportCount]:
    """The bad reports counted dropped, by reason, as a snapshot lists them."""
    return [ExitReportCount("", "", "bad", count, dropped=reason) for reason, count in sorted(reasons.items())]


async def _row(session: AsyncSession, connector: Connector, **metadata: str) -> None:
    """A proxy row of the connector, with ``metadata`` such as where it was last seen exiting, committed."""
    await ProxyRepository(session).create(
        Proxy(host="203.0.113.250", port=8080, protocol=ProxyProtocol.HTTP, connector_id=connector.id, metadata=metadata)
    )
    await session.commit()


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
        # No proxy row named: its exit headers named it, for the workers to check.
        assert _by_headers(project, "c").by_headers and not message.by_headers


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
        assert ingest.counts.snapshot() == [
            ExitReportCount(connector.id, "Residential", "bad", 1),
            *_dropped(connector=2, project=1),
        ]

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

    async def test_a_project_off_or_gone_records_nothing_and_its_reports_are_dropped(
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
        assert ingest.counts.snapshot() == _dropped(project=2)
        assert await _runtime(redis_client) == []


class TestExitHeaders:
    """A report its exit headers named is checked against the project's rows in the batch's transaction: a dynamic
    connector counts it alone, at the place asked for, and a connector with IPs for the exit IP one of its rows was last
    seen exiting from, and at no place; anything else is dropped, counted and logged by reason."""

    async def test_a_dynamic_connector_counts_it_at_the_place_asked_for_whatever_ip_was_sent(
        self,
        ingest: ExitReportIngest,
        project: Project,
        connector: Connector,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        await _row(db_session, connector, dynamic_sessions="true", discovered_ip="203.0.113.50")

        await ingest._handle(_entries(_by_headers(project, connector.id, ip="203.0.113.50", place="US/NY")))

        # Its exits are per request: the IP it went out through is no record of its own.
        assert await _ip(db_session_factory, ExitIpKey(project.id, "203.0.113.50", "google.com")) is None
        assert await _windows(redis_client) == {
            (project.id, connector.id, "google.com", "US/NY"): RequestCount(blocked=1)
        }
        assert ingest.counts.snapshot() == [ExitReportCount(connector.id, "Residential", "bad", 1)]

    async def test_a_connector_with_ips_counts_it_for_the_exit_ip_its_rows_were_seen_at_and_at_no_place(
        self,
        ingest: ExitReportIngest,
        project: Project,
        connector: Connector,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        await _row(db_session, connector, discovered_ip="203.0.113.7")
        # A row's exit as an echo answered it, in its IPv4-mapped form.
        await _row(db_session, connector, discovered_ip="::ffff:203.0.113.8")
        await _row(db_session, connector)

        await ingest._handle(
            _entries(
                _by_headers(project, connector.id, ip="203.0.113.7", place="US"),
                _by_headers(project, connector.id, ExitVerdict.GOOD, ip="203.0.113.8", place="US/NY"),
            )
        )

        assert (await _ip(db_session_factory, ExitIpKey(project.id, "203.0.113.7", "google.com"))).bad_reports == 1
        assert (await _ip(db_session_factory, ExitIpKey(project.id, "203.0.113.8", "google.com"))).good_reports == 1
        # Its rows already depend on the place asked for.
        assert await _windows(redis_client) == {
            (project.id, connector.id, "google.com", ""): RequestCount(blocked=1, good=1)
        }

    async def test_exit_headers_that_name_no_exit_of_the_project_are_dropped_counted_and_logged_once(
        self,
        ingest: ExitReportIngest,
        project: Project,
        connector: Connector,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        await _row(db_session, connector, discovered_ip="203.0.113.7")
        off = await make_project(db_session, "switched-off", ExitReputationMode.OFF)
        off_connector = await make_connector(db_session, off, "Off")
        other = await make_project(db_session, "other")
        theirs = await make_connector(db_session, other, "Theirs")
        await _row(db_session, theirs, discovered_ip="203.0.113.7")

        with structlog.testing.capture_logs() as logs:
            await ingest._handle(
                _entries(
                    _by_headers(off, off_connector.id, ip="203.0.113.7"),
                    _by_headers(project, theirs.id, ip="203.0.113.7"),
                    _by_headers(project, "no-such-connector"),
                    _by_headers(project, connector.id),
                    _by_headers(project, connector.id, ip="198.51.100.99"),
                    _by_headers(project, connector.id, ExitVerdict.GOOD, ip="203.0.113.7"),
                )
            )

        assert await _ip(db_session_factory, ExitIpKey(project.id, "198.51.100.99", "google.com")) is None
        assert await _windows(redis_client) == {
            (project.id, connector.id, "google.com", ""): RequestCount(good=1)
        }
        assert ingest.counts.snapshot() == [
            ExitReportCount(connector.id, "Residential", "good", 1),
            *_dropped(connector=2, ip_needed=1, ip_not_seen=1, project=1),
        ]
        dropped = [log for log in logs if log["event"] == "Exit reports dropped"]
        assert [(log["log_level"], log["count"], log["reasons"]) for log in dropped] == [
            ("warning", 5, {"connector": 2, "ip_needed": 1, "ip_not_seen": 1, "project": 1})
        ]
        assert set(DropReason) - {DropReason.GIVEN_UP} == {DropReason(reason) for reason in dropped[0]["reasons"]}

    async def test_exit_headers_that_name_no_exit_fall_back_on_the_tags_note(
        self,
        ingest: ExitReportIngest,
        project: Project,
        connector: Connector,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        """A connector with IPs named without its IP or with one none of its rows was seen at, a connector that is not
        the project's: the note that came with the report names its exit, as for a report without exit headers."""
        await _row(db_session, connector, discovered_ip="203.0.113.7")
        dynamic = await make_connector(db_session, project, "Dynamic")
        await _row(db_session, dynamic, dynamic_sessions="true")
        noted = NotedExit(connector_id=connector.id, proxy_id="row-1", ip="198.51.100.5")

        await ingest._handle(
            _entries(
                _by_headers(project, connector.id, place="US", noted=noted),
                _by_headers(project, connector.id, ip="198.51.100.99", noted=noted),
                _by_headers(project, "no-such-connector", noted=noted),
                # A dynamic gateway's note counts for its connector at the place asked for.
                _by_headers(project, connector.id, place="US/NY", noted=NotedExit(connector_id=dynamic.id, proxy_id="gw")),
            )
        )

        assert (await _ip(db_session_factory, ExitIpKey(project.id, "198.51.100.5", "google.com"))).bad_reports == 3
        assert await _ip(db_session_factory, ExitIpKey(project.id, "198.51.100.99", "google.com")) is None
        assert await _windows(redis_client) == {
            (project.id, connector.id, "google.com", ""): RequestCount(blocked=3),
            (project.id, dynamic.id, "google.com", "US/NY"): RequestCount(blocked=1),
        }
        assert set(ingest.counts.snapshot()) == {
            ExitReportCount(connector.id, "Residential", "bad", 3),
            ExitReportCount(dynamic.id, "Dynamic", "bad", 1),
        }

    async def test_exit_headers_that_name_an_exit_win_over_the_tags_note(
        self,
        ingest: ExitReportIngest,
        project: Project,
        connector: Connector,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _row(db_session, connector, discovered_ip="203.0.113.7")
        noted = NotedExit(connector_id=connector.id, proxy_id="row-1", ip="198.51.100.5")

        await ingest._handle(_entries(_by_headers(project, connector.id, ip="203.0.113.7", noted=noted)))

        assert (await _ip(db_session_factory, ExitIpKey(project.id, "203.0.113.7", "google.com"))).bad_reports == 1
        assert await _ip(db_session_factory, ExitIpKey(project.id, "198.51.100.5", "google.com")) is None

    async def test_a_report_neither_its_headers_nor_its_note_name_is_dropped_for_the_headers_reason(
        self, ingest: ExitReportIngest, project: Project, connector: Connector, db_session: AsyncSession
    ) -> None:
        await _row(db_session, connector, discovered_ip="203.0.113.7")
        gone = NotedExit(connector_id="gone", proxy_id="gw")

        await ingest._handle(
            _entries(
                _by_headers(project, connector.id, ip="198.51.100.99", noted=gone),
                _by_headers(project, "no-such-connector", noted=gone),
            )
        )

        assert ingest.counts.snapshot() == _dropped(connector=1, ip_not_seen=1)

    async def test_a_tags_note_is_not_checked_against_the_rows(
        self,
        ingest: ExitReportIngest,
        project: Project,
        connector: Connector,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        """A gateway noted the exit it routed: no row need hold its IP now."""
        await ingest._handle(_entries(_report(project, connector_id=connector.id, ip="198.51.100.99")))

        assert (await _ip(db_session_factory, ExitIpKey(project.id, "198.51.100.99", "google.com"))).bad_reports == 1
        assert await _windows(redis_client) == {(project.id, connector.id, "google.com", ""): RequestCount(blocked=1)}

    async def test_the_rows_are_read_in_two_statements_whatever_the_batch(
        self,
        ingest: ExitReportIngest,
        project: Project,
        db_session: AsyncSession,
        db_engine: AsyncEngine,
    ) -> None:
        """One for which connectors are dynamic and one for the exit IPs named, for every report of the batch; none
        without a report its exit headers named."""
        connectors = [await make_connector(db_session, project, f"pool-{n}") for n in range(3)]
        for n, connector in enumerate(connectors):
            await _row(db_session, connector, discovered_ip=f"203.0.113.{n}")

        async def proxy_reads(*reports: ExitReportMessage) -> int:
            with counted_selects(db_engine) as selects:
                await ingest._handle(_entries(*reports))
            return sum("proxies" in statement for statement in selects)

        assert await proxy_reads(_by_headers(project, connectors[0].id, ip="203.0.113.0")) == 2
        many = [
            _by_headers(project, connector.id, ip=f"203.0.113.{n}", site=f"site-{m}.example.com")
            for n, connector in enumerate(connectors)
            for m in range(10)
        ]
        assert await proxy_reads(*many) == 2
        assert await proxy_reads(_report(project), _report(project, connector_id=connectors[0].id)) == 0


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

        # By connector id, then verdict; the connector's id is random, so it may sort either side of "deleted". Then the
        # dropped ones, by reason.
        assert ingest.counts.snapshot() == [
            *sorted(
                [
                    ExitReportCount("", "", "bad", 1),
                    ExitReportCount(connector.id, "Residential", "bad", 1),
                    ExitReportCount(connector.id, "Residential", "good", 2),
                    ExitReportCount("deleted", "", "bad", 1),
                ],
                key=lambda count: (count.connector_id, count.verdict),
            ),
            *_dropped(connector=1),
        ]

    def test_the_drops_by_reason_add_up_their_verdicts(self) -> None:
        """What the heartbeat carries for the System page: one number per reason, the reports taken left out."""
        counts = ExitReportCounts()
        assert counts.dropped_by_reason() == {}

        counts.drop(DropReason.IP_NOT_SEEN, ExitVerdict.BAD)
        counts.drop(DropReason.IP_NOT_SEEN, ExitVerdict.GOOD)
        counts.drop(DropReason.PROJECT, ExitVerdict.BAD)
        counts.add("c-1", "Residential", ExitVerdict.BAD)

        assert counts.dropped_by_reason() == {"ip_not_seen": 2, "project": 1}


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
