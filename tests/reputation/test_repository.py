# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The records in Postgres, on the test container: a batch of reports applied to the exit IP records, a metrics flush's
requests and reports to the connector records, new records inserted with their final values and stored ones locked in
key order, the reads the feed takes, and the retention by day.

Writers with overlapping batches (two report ingests, two metrics flushes) must neither deadlock nor lose each other's
evidence; the concurrency tests hold real transactions open against each other.
"""

import asyncio
import random
from collections import Counter
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.stats import RequestCount
from api.db.repository import ConnectorRepository, ProjectRepository
from api.db.reputation_repository import ExitReputationRepository
from api.models.connector import Connector
from api.models.project import ExitReputationMode, Project
from api.reputation.record import (
    ConnectorChange,
    ConnectorSiteKey,
    ConnectorSiteReputation,
    ExitIpKey,
    ExitIpReputation,
    IpChange,
    ReportCounts,
)
from tests.reputation.world import make_connector, make_project

T0 = datetime(2026, 11, 15, 12, 0, 0)
BAD = ReportCounts(bad=1)
GOOD = ReportCounts(good=1)
# A flush's one blocked report through a connector.
BLOCKED = RequestCount(blocked=1)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


@pytest.fixture
async def project(db_session: AsyncSession) -> Project:
    return await make_project(db_session, "acme", ExitReputationMode.AVOID)


@pytest.fixture
async def connector(db_session: AsyncSession, project: Project) -> Connector:
    return await make_connector(db_session, project)


def ip(project: Project, n: int = 1, site: str = "google.com") -> ExitIpKey:
    return ExitIpKey(project.id, f"203.0.113.{n}", site)


def place(connector: Connector, level: str = "", site: str = "google.com") -> ConnectorSiteKey:
    return ConnectorSiteKey(connector.project_id, connector.id, site, level)


async def _apply(session: AsyncSession, when: datetime, ips: dict[ExitIpKey, ReportCounts]) -> Any:
    changes = await ExitReputationRepository(session).apply_reports(ips, when)
    await session.commit()
    return changes


async def _flush(
    session: AsyncSession,
    when: datetime,
    evidence: dict[ConnectorSiteKey, RequestCount],
    dynamic: Collection[str] = (),
) -> Any:
    changes = await ExitReputationRepository(session).apply_evidence(evidence, dynamic, when)
    await session.commit()
    return changes


class TestApplyReports:
    async def test_a_new_record_is_inserted_with_its_final_values(self, db_session: AsyncSession, project: Project) -> None:
        changes = await _apply(db_session, T0, {ip(project): ReportCounts(bad=1, reason="captcha")})

        assert changes.ips == (IpChange(ip(project), T0, None, at(1)),)
        assert changes.connectors == ()
        assert changes.moved_projects == [project.id]
        assert await ExitReputationRepository(db_session).ip_record(ip(project)) == ExitIpReputation(
            project_id=project.id,
            ip="203.0.113.1",
            site="google.com",
            bad_reports=1,
            weighted_bad=1.0,
            weighted_avoids=1.0,
            weighted_at=T0,
            avoid_until=at(1),
            first_seen_at=T0,
            last_seen_at=T0,
            last_seen_day=T0.date(),
            last_report_at=T0,
            last_reason="captcha",
        )
        # Inserted once, never updated: the live row is the first version written to the fresh table.
        assert (await db_session.execute(text("SELECT ctid::text FROM exit_ip_reputation"))).scalars().all() == ["(0,1)"]

    async def test_evidence_adds_up_and_decays_between_batches(self, db_session: AsyncSession, project: Project) -> None:
        await _apply(db_session, T0, {ip(project): BAD})
        changes = await _apply(db_session, at(24), {ip(project): ReportCounts(good=1)})

        stored = await ExitReputationRepository(db_session).ip_record(ip(project))
        assert stored is not None
        assert (stored.good_reports, stored.bad_reports, stored.weighted_good, stored.weighted_bad) == (1, 1, 1.0, 0.5)
        assert (stored.weighted_avoids, stored.weighted_at, stored.last_seen_day) == (0.5, at(24), at(24).date())
        # The avoid had ended long before: nothing moved.
        assert changes.ips == (IpChange(ip(project), at(24), at(1), at(1)),) and not changes.moved_projects

    async def test_the_changes_name_each_record_in_key_order(self, db_session: AsyncSession, project: Project) -> None:
        ips = {ip(project, 2): BAD, ip(project, 1, "bing.com"): GOOD, ip(project, 1): BAD}

        changes = await _apply(db_session, T0, ips)

        assert [change.key for change in changes.ips] == sorted(ips)
        # The good report alone begins nothing.
        assert [change.moved for change in changes.ips] == [False, True, True]

    async def test_a_project_off_or_gone_is_skipped(self, db_session: AsyncSession, project: Project) -> None:
        """Turned off while its reports were queued: nothing is recorded, as the mode says."""
        off = await make_project(db_session, "off", ExitReputationMode.OFF)
        gone = ExitIpKey("no-such-project", "203.0.113.1", "google.com")

        changes = await _apply(db_session, T0, {ip(off): BAD, gone: BAD, ip(project): BAD})

        assert [change.key for change in changes.ips] == [ip(project)]
        assert (await db_session.execute(text("SELECT count(*) FROM exit_ip_reputation"))).scalar_one() == 1

    async def test_a_project_that_records_moves_nobodys_routing(self, db_session: AsyncSession) -> None:
        recording = await make_project(db_session, "recording", ExitReputationMode.RECORD)
        changes = await _apply(db_session, T0, {ip(recording): BAD})
        assert changes.ips[0].moved and changes.moved_projects == []

    async def test_the_projects_settings_decide(self, db_session: AsyncSession) -> None:
        patient = await make_project(
            db_session,
            "patient",
            ExitReputationMode.AVOID,
            exit_reputation_avoid_minutes=15,
            exit_reputation_bad_reports=2,
        )
        await _apply(db_session, T0, {ip(patient): BAD})
        stored = await ExitReputationRepository(db_session).ip_record(ip(patient))
        assert stored is not None and stored.avoid_until is None
        await _apply(db_session, T0, {ip(patient): BAD})
        stored = await ExitReputationRepository(db_session).ip_record(ip(patient))
        assert stored is not None and stored.avoid_until == T0 + timedelta(minutes=15)

    async def test_records_go_with_their_project_or_connector(
        self, db_session: AsyncSession, project: Project, connector: Connector
    ) -> None:
        other = await make_connector(db_session, project, "other")
        await _apply(db_session, T0, {ip(project): BAD})
        await _flush(db_session, T0, {place(connector): BLOCKED, place(other): BLOCKED})
        repository = ExitReputationRepository(db_session)

        await ConnectorRepository(db_session).delete(connector.id)
        await db_session.commit()
        assert await repository.connector_record(place(connector)) is None
        assert await repository.connector_record(place(other)) is not None

        await ProjectRepository(db_session).delete(project.id)
        await db_session.commit()
        assert await repository.ip_record(ip(project)) is None
        assert await repository.connector_record(place(other)) is None

    async def test_batches_of_any_size_are_the_same_statements(
        self, db_session: AsyncSession, project: Project, connector: Connector
    ) -> None:
        """A statement per batch size would leave a plan each in every pooled connection's backend."""
        repository = ExitReputationRepository(db_session)
        for size in (1, 2, 2500):
            ips = {ExitIpKey(project.id, f"10.9.{n // 250}.{n % 250}", "google.com"): BAD for n in range(size)}
            changes = await repository.apply_reports(ips, T0)
            assert len(changes.ips) == size
            # Reports making records, then requests alone locking them.
            sites = [place(connector, site=f"site-{n}.example") for n in range(size)]
            made = await repository.apply_evidence(dict.fromkeys(sites, BLOCKED), [connector.id], T0)
            locked = await repository.apply_evidence(dict.fromkeys(sites, RequestCount(requests=3)), [connector.id], T0)
            assert len(made.connectors) == len(locked.connectors) == size
        # Anchored at the start, so this query, prepared too, does not count itself.
        prepared = await db_session.execute(
            text(
                "SELECT count(DISTINCT statement) FILTER (WHERE statement LIKE 'SELECT projects.id, projects.exit_reputation%'), "
                "count(DISTINCT statement) FILTER (WHERE statement LIKE 'INSERT INTO exit_ip_reputation%'), "
                "count(DISTINCT statement) FILTER (WHERE statement LIKE 'UPDATE exit_ip_reputation%'), "
                "count(DISTINCT statement) FILTER (WHERE statement LIKE 'INSERT INTO connector_site_reputation%'), "
                "count(DISTINCT statement) FILTER (WHERE statement LIKE 'UPDATE connector_site_reputation%'), "
                "count(DISTINCT statement) FILTER (WHERE statement LIKE 'SELECT connector_site_reputation.project_id%') "
                "FROM pg_prepared_statements"
            )
        )
        counts = tuple(prepared.one())
        await db_session.commit()

        # The connector records' reads: the keys stored, and the records locked.
        assert counts == (1, 1, 1, 1, 1, 2)
        first = await repository.ip_record(ExitIpKey(project.id, "10.9.0.0", "google.com"))
        assert first is not None and first.bad_reports == 3
        site = await repository.connector_record(place(connector, site="site-0.example"))
        assert site is not None and (site.blocked_reports, site.requests) == (3, 9)


class TestApplyEvidence:
    """A metrics flush's requests and reports: a report makes a record, requests add only to one that exists."""

    async def test_a_report_makes_a_record_inserted_with_its_final_values(
        self, db_session: AsyncSession, project: Project, connector: Connector
    ) -> None:
        changes = await _flush(
            db_session, T0, {place(connector, "US"): RequestCount(requests=20, blocked=5)}, [connector.id]
        )

        assert changes.connectors == (ConnectorChange(place(connector, "US"), None, 0.75),)
        assert await ExitReputationRepository(db_session).connector_record(place(connector, "US")) == (
            ConnectorSiteReputation(
                project_id=project.id,
                connector_id=connector.id,
                site="google.com",
                place="US",
                blocked_reports=5,
                requests=20,
                weighted_blocked=5.0,
                weighted_requests=20.0,
                factor=0.75,
                weighted_at=T0,
                first_seen_at=T0,
                last_seen_at=T0,
                last_seen_day=T0.date(),
            )
        )
        # Inserted once, never updated.
        assert (await db_session.execute(text("SELECT ctid::text FROM connector_site_reputation"))).scalars().all() == [
            "(0,1)"
        ]

    async def test_requests_add_to_the_records_that_exist_and_make_none(
        self, db_session: AsyncSession, project: Project, connector: Connector
    ) -> None:
        await _flush(db_session, T0, {place(connector, "US"): RequestCount(blocked=2)})

        changes = await _flush(
            db_session,
            at(1),
            {place(connector, "US"): RequestCount(requests=40, blocked=2), place(connector, "GB"): RequestCount(500)},
            [connector.id],
        )

        assert changes.connectors == (ConnectorChange(place(connector, "US"), None, 0.95),)
        assert changes.moved_projects == [project.id]
        repository = ExitReputationRepository(db_session)
        stored = await repository.connector_record(place(connector, "US"))
        assert stored is not None
        # The first two count about 0.38 each now: half for the hour, and about three quarters of that for the 40 requests.
        assert stored.weighted_blocked == pytest.approx(2 * 0.5 * 2 ** -0.4 + 2)
        assert (stored.blocked_reports, stored.requests, stored.weighted_requests, stored.factor) == (4, 40, 40.0, 0.95)
        assert (stored.last_seen_at, stored.weighted_at) == (at(1), at(1))
        assert await repository.connector_record(place(connector, "GB")) is None

    async def test_a_good_report_makes_a_record_too(
        self, db_session: AsyncSession, project: Project, connector: Connector
    ) -> None:
        await _flush(db_session, T0, {place(connector): RequestCount(requests=30, good=1)})

        stored = await ExitReputationRepository(db_session).connector_record(place(connector))
        assert stored is not None and (stored.good_reports, stored.requests, stored.factor) == (1, 30, 1.0)

    async def test_only_the_dynamic_connectors_changes_are_told(
        self, db_session: AsyncSession, project: Project, connector: Connector
    ) -> None:
        """A connector with IPs is weighed by its usable IPs, never by a factor: its factor moving moves no routing."""
        static = await make_connector(db_session, project, "static")
        half = RequestCount(requests=20, blocked=10)

        changes = await _flush(db_session, T0, {place(connector): half, place(static): half}, [connector.id])
        assert changes.connectors == (ConnectorChange(place(connector), None, 0.5),)
        assert changes.moved_projects == [project.id]

        changes = await _flush(db_session, T0, {place(static, "US"): half}, [connector.id])
        assert changes.connectors == () and changes.moved_projects == []
        stored = await ExitReputationRepository(db_session).connector_record(place(static, "US"))
        assert stored is not None and stored.factor == 0.5

    async def test_a_project_off_or_gone_and_a_connector_gone_or_elsewhere_are_skipped(
        self, db_session: AsyncSession, project: Project, connector: Connector
    ) -> None:
        off = await make_project(db_session, "off", ExitReputationMode.OFF)
        off_connector = await make_connector(db_session, off, "off")
        other = await make_project(db_session, "other")
        elsewhere = await make_connector(db_session, other, "elsewhere")
        deleted = await make_connector(db_session, project, "deleted")
        await ConnectorRepository(db_session).delete(deleted.id)
        await db_session.commit()

        changes = await _flush(
            db_session,
            T0,
            {
                place(off_connector): BLOCKED,
                ConnectorSiteKey("no-such-project", connector.id, "google.com", ""): BLOCKED,
                place(deleted): BLOCKED,
                ConnectorSiteKey(project.id, elsewhere.id, "google.com", ""): BLOCKED,
                place(connector): BLOCKED,
            },
            [off_connector.id, connector.id, deleted.id, elsewhere.id],
        )

        assert changes.connectors == (ConnectorChange(place(connector), None, None),)
        assert (await db_session.execute(text("SELECT count(*) FROM connector_site_reputation"))).scalar_one() == 1

    async def test_a_project_that_records_nothing_adds_nothing(self, db_session: AsyncSession) -> None:
        project = await make_project(db_session, "switching", ExitReputationMode.RECORD)
        connector = await make_connector(db_session, project)
        await _flush(db_session, T0, {place(connector): BLOCKED})
        project.exit_reputation = ExitReputationMode.OFF
        await ProjectRepository(db_session).update(project)
        await db_session.commit()

        changes = await _flush(db_session, T0, {place(connector): RequestCount(requests=100, blocked=1)}, [connector.id])

        assert changes.connectors == ()
        stored = await ExitReputationRepository(db_session).connector_record(place(connector))
        assert stored is not None and (stored.requests, stored.blocked_reports) == (0, 1)

    async def test_the_projects_connector_memory_decides(self, db_session: AsyncSession) -> None:
        brief = await make_project(
            db_session, "brief", ExitReputationMode.AVOID, exit_reputation_connector_half_life_requests=20
        )
        connector = await make_connector(db_session, brief)
        await _flush(db_session, T0, {place(connector): RequestCount(requests=40, blocked=40)})

        await _flush(db_session, T0, {place(connector): RequestCount(requests=20)})

        stored = await ExitReputationRepository(db_session).connector_record(place(connector))
        assert stored is not None
        assert (stored.weighted_requests, stored.weighted_blocked) == (pytest.approx(40.0), pytest.approx(20.0))


class TestConcurrentWriters:
    async def test_a_writer_of_records_another_is_making_waits_for_it(
        self, db_session_factory: async_sessionmaker[AsyncSession], project: Project
    ) -> None:
        """Both make the same new records: the second's insert waits on the first's, then locks and updates its rows."""
        k0, k1, k2 = ip(project, 0), ip(project, 1), ip(project, 2)
        async with db_session_factory() as first, db_session_factory() as second:
            await ExitReputationRepository(first).apply_reports({k0: BAD, k1: BAD}, T0)
            waiting = asyncio.create_task(
                ExitReputationRepository(second).apply_reports({k2: GOOD, k1: GOOD, k0: GOOD}, T0)
            )
            await asyncio.sleep(0.3)
            assert not waiting.done()
            await first.commit()
            changes = await asyncio.wait_for(waiting, timeout=10)
            await second.commit()

        assert [change.key for change in changes.ips] == [k0, k1, k2]
        async with db_session_factory() as session:
            repository = ExitReputationRepository(session)
            stored = [await repository.ip_record(k) for k in (k0, k1, k2)]
        assert [(r.good_reports, r.bad_reports) for r in stored if r] == [(1, 1), (1, 1), (1, 0)]

    async def test_a_flush_waits_for_another_holding_its_records(
        self, db_session_factory: async_sessionmaker[AsyncSession], project: Project, connector: Connector
    ) -> None:
        """The first makes one record and locks another for its requests; the second waits at the first key the two
        share, then locks both and adds to them in its turn."""
        k0, k1, k2 = (place(connector, site=f"{n}.example") for n in (0, 1, 2))
        async with db_session_factory() as session:
            await ExitReputationRepository(session).apply_evidence({k1: BLOCKED}, [connector.id], T0)
            await session.commit()
        requests = RequestCount(requests=10)
        async with db_session_factory() as first, db_session_factory() as second:
            await ExitReputationRepository(first).apply_evidence({k0: BLOCKED, k1: requests}, [connector.id], T0)
            waiting = asyncio.create_task(
                ExitReputationRepository(second).apply_evidence(
                    {k2: BLOCKED, k1: requests, k0: BLOCKED}, [connector.id], T0
                )
            )
            await asyncio.sleep(0.3)
            assert not waiting.done()
            await first.commit()
            changes = await asyncio.wait_for(waiting, timeout=10)
            await second.commit()

        assert [change.key for change in changes.connectors] == [k0, k1, k2]
        async with db_session_factory() as session:
            repository = ExitReputationRepository(session)
            stored = [await repository.connector_record(k) for k in (k0, k1, k2)]
        assert [(r.blocked_reports, r.requests) for r in stored if r] == [(2, 0), (1, 20), (1, 0)]

    async def test_many_writers_on_overlapping_records_lose_nothing_and_never_deadlock(
        self, db_session_factory: async_sessionmaker[AsyncSession], project: Project, connector: Connector
    ) -> None:
        """Report ingests on the exit IP records, and flushes on the connector records, each flush's keys a mix of
        reports, which make or lock a record, and requests alone, which lock one that is stored."""
        ips = [ip(project, n, site) for n in range(4) for site in ("google.com", "bing.com")]
        levels = [place(connector, level, site) for level in ("", "US", "US/NY") for site in ("google.com", "bing.com")]
        expected: Counter[Any] = Counter()

        async def reporter(seed: int) -> None:
            shuffled = random.Random(seed)
            for _round in range(5):
                chosen = shuffled.sample(ips, 5)
                async with db_session_factory() as session:
                    await ExitReputationRepository(session).apply_reports(dict.fromkeys(chosen, BAD), T0)
                    # Held open a moment, so the other writers queue behind its locks.
                    await asyncio.sleep(0.01)
                    await session.commit()
                expected.update(chosen)

        async def flush(seed: int) -> None:
            shuffled = random.Random(seed)
            for _round in range(5):
                chosen = shuffled.sample(levels, 4)
                reported = set(shuffled.sample(chosen, 2))
                evidence = {
                    level: RequestCount(requests=10, blocked=int(level in reported)) for level in chosen
                }
                async with db_session_factory() as session:
                    await ExitReputationRepository(session).apply_evidence(evidence, [connector.id], T0)
                    await asyncio.sleep(0.01)
                    await session.commit()
                expected.update(reported)

        await asyncio.gather(*(reporter(seed) for seed in range(5)), *(flush(seed) for seed in range(100, 105)))

        async with db_session_factory() as session:
            repository = ExitReputationRepository(session)
            for k in ips:
                stored = await repository.ip_record(k)
                assert stored is not None and stored.bad_reports == expected[k]
            for level in levels:
                record = await repository.connector_record(level)
                if expected[level]:
                    assert record is not None and record.blocked_reports == expected[level]

    async def test_a_record_a_writer_made_is_held_from_its_first_statement_so_no_sweep_takes_it(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The statement that makes or locks a record holds it until the commit: the sweep, skipping held rows, never
        deletes evidence a writer is about to add to."""
        stale = ip(project)
        async with db_session_factory() as session:
            await ExitReputationRepository(session).apply_reports({stale: GOOD}, at(-40 * 24))
            await session.commit()

        locked, go_on = asyncio.Event(), asyncio.Event()
        async with db_session_factory() as writer, db_session_factory() as sweep:
            execute = writer.execute
            statements = 0

            async def pausing(*args: Any, **kwargs: Any) -> Any:
                nonlocal statements
                result = await execute(*args, **kwargs)
                statements += 1
                # The plan setting, the projects' settings, then the statement that makes or locks the records.
                if statements == 3:
                    locked.set()
                    await go_on.wait()
                return result

            monkeypatch.setattr(writer, "execute", pausing)
            applying = asyncio.create_task(ExitReputationRepository(writer).apply_reports({stale: BAD}, T0))
            await asyncio.wait_for(locked.wait(), timeout=10)
            deleted = await ExitReputationRepository(sweep).delete_stale(at(-24).date(), T0)
            await sweep.commit()
            go_on.set()
            await asyncio.wait_for(applying, timeout=10)
            await writer.commit()

        assert deleted == (0, 0)
        async with db_session_factory() as session:
            stored = await ExitReputationRepository(session).ip_record(stale)
        assert stored is not None and (stored.good_reports, stored.bad_reports) == (1, 1)


class TestLockOrder:
    async def test_records_are_taken_in_python_s_key_order_whatever_the_collation(
        self, db_session: AsyncSession, project: Project
    ) -> None:
        """Every writer locks in one order only if each statement follows the same one."""
        sites = ["a-b.com", "ab.com", "a.b.com", "A1.com", "a_b.com", "1.1.1.1", "2001:db8::1", "zz.com"]
        keys = [ExitIpKey(project.id, "203.0.113.1", site) for site in sites]

        changes = await _apply(db_session, T0, dict.fromkeys(keys, BAD))
        rows = await db_session.execute(
            text(
                "SELECT site FROM exit_ip_reputation WHERE project_id = :p "
                'ORDER BY project_id COLLATE "C", ip COLLATE "C", site COLLATE "C"'
            ),
            {"p": project.id},
        )

        assert [change.key for change in changes.ips] == sorted(keys)
        assert [row.site for row in rows] == sorted(sites)


class TestFeedReads:
    async def test_avoids_ahead_for_the_ips_asked_about(self, db_session: AsyncSession, project: Project) -> None:
        other = await make_project(db_session, "other", ExitReputationMode.AVOID)
        await _apply(db_session, at(-2), {ip(project, 1, "old.example"): BAD})
        await _apply(db_session, T0, {ip(project, 1): BAD, ip(project, 2): BAD, ip(other, 1): BAD, ip(project, 3): GOOD})

        records = await ExitReputationRepository(db_session).ip_avoids(
            {project.id: {"203.0.113.1", "203.0.113.3"}, other.id: {"203.0.113.1"}}, at(0.5), per_project=100
        )

        assert sorted(record.key for record in records) == sorted([ip(project, 1), ip(other, 1)])

    async def test_an_ip_and_a_project_carry_their_capped_avoids_the_latest_ends_first(
        self, db_session: AsyncSession, project: Project
    ) -> None:
        sites = ("a.example", "b.example", "c.example")
        await _apply(db_session, T0, {ip(project, n, site): BAD for n in (1, 2) for site in sites})
        for hours, site in enumerate(sites, start=1):
            await db_session.execute(
                text("UPDATE exit_ip_reputation SET avoid_until = :end WHERE site = :site"),
                {"end": at(hours), "site": site},
            )
        await db_session.commit()
        repository = ExitReputationRepository(db_session)
        both = {project.id: {"203.0.113.1", "203.0.113.2"}}

        per_ip = await repository.ip_avoids(both, T0, per_ip=2, per_project=100)
        per_project = await repository.ip_avoids(both, T0, per_project=3)

        assert sorted((record.ip, record.site) for record in per_ip) == [
            ("203.0.113.1", "b.example"),
            ("203.0.113.1", "c.example"),
            ("203.0.113.2", "b.example"),
            ("203.0.113.2", "c.example"),
        ]
        assert sorted((record.ip, record.site) for record in per_project) == [
            ("203.0.113.1", "b.example"),
            ("203.0.113.1", "c.example"),
            ("203.0.113.2", "c.example"),
        ]

    async def test_factors_of_the_connectors_asked_about(
        self, db_session: AsyncSession, project: Project, connector: Connector
    ) -> None:
        other = await make_connector(db_session, project, "other")
        await _flush(
            db_session,
            T0,
            {
                place(connector, "US"): RequestCount(requests=10, blocked=1),
                place(connector): BLOCKED,
                place(other, "US"): RequestCount(requests=100, blocked=1),
            },
        )
        repository = ExitReputationRepository(db_session)

        assert await repository.connector_factors([connector.id]) == []
        await _flush(db_session, T0, {place(connector, "US"): RequestCount(requests=11)})

        factors = await repository.connector_factors([connector.id, other.id])
        assert [(record.connector_id, record.place, record.factor) for record in factors] == sorted(
            [(connector.id, "US", 0.95), (other.id, "US", 1.0)]
        )


class TestDeleteStale:
    """The retention: records last seen on a day before the cutoff go, an exit IP's only once no avoid is ahead."""

    async def test_stale_records_go_and_live_avoids_and_fresh_ones_stay(
        self, db_session: AsyncSession, project: Project, connector: Connector
    ) -> None:
        repository = ExitReputationRepository(db_session)
        await _apply(db_session, at(-40 * 24), {ip(project, 1): BAD})
        await _flush(db_session, at(-40 * 24), {place(connector, "US"): BLOCKED})
        await _apply(db_session, at(-24), {ip(project, 2): GOOD})
        await _flush(db_session, at(-24), {place(connector): BLOCKED})
        # Avoided for a week from three days ago.
        await repository.apply_reports({ip(project, 3): BAD}, at(-72))
        await db_session.execute(
            text("UPDATE exit_ip_reputation SET avoid_until = :end WHERE ip = '203.0.113.3'"), {"end": at(96)}
        )
        await db_session.commit()

        deleted = await repository.delete_stale(at(-48).date(), T0)
        await db_session.commit()

        assert deleted == (1, 1)
        assert await repository.ip_record(ip(project, 1)) is None
        assert await repository.connector_record(place(connector, "US")) is None
        assert await repository.ip_record(ip(project, 2)) is not None
        assert await repository.ip_record(ip(project, 3)) is not None
        assert await repository.connector_record(place(connector)) is not None

    async def test_a_record_a_writer_holds_is_skipped_never_waited_for(
        self, db_session_factory: async_sessionmaker[AsyncSession], project: Project
    ) -> None:
        """The sweep never waits on a writer, so the two cannot deadlock; the held record goes with a later sweep."""
        async with db_session_factory() as session:
            await ExitReputationRepository(session).apply_reports(
                dict.fromkeys((ip(project, n) for n in (1, 2, 3)), GOOD), at(-40 * 24)
            )
            await session.commit()

        async with db_session_factory() as writer, db_session_factory() as sweep:
            # The writer holds record 2 locked, as an apply does until it commits.
            await writer.execute(
                text("SELECT 1 FROM exit_ip_reputation WHERE project_id = :p AND ip = '203.0.113.2' FOR UPDATE"),
                {"p": project.id},
            )
            deleted = await asyncio.wait_for(ExitReputationRepository(sweep).delete_stale(T0.date(), T0), timeout=5)
            await sweep.commit()
            await writer.commit()

        async with db_session_factory() as session:
            repository = ExitReputationRepository(session)
            assert deleted == (2, 0)
            assert await repository.ip_record(ip(project, 2)) is not None
            # The next sweep, with the writer gone, takes it.
            assert await repository.delete_stale(T0.date(), T0) == (1, 0)
