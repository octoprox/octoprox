# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What the views read back of the records: where an exit IP's record stands at a moment, and the repository's pages,
IPs and summaries, and what the traffic split and the connectors view read for sites and places.

The status filters run in SQL and the statuses shown are decided in Python; the two must agree on every record at every
moment, so one test asks both about the same records.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.stats import RequestCount
from api.db import reputation_repository
from api.db.reputation_repository import ExitReputationRepository, ReputationSort
from api.models.exit_reputation import ReputationSummary
from api.models.project import ExitReputationMode, Project
from api.reputation.record import (
    ConnectorSiteKey,
    ExitIpKey,
    ExitIpReputation,
    ReportCounts,
    ReputationStatus,
)
from tests.reputation.world import make_connector, make_project

T0 = datetime(2026, 11, 15, 12, 0, 0)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


@pytest.fixture
async def project(db_session: AsyncSession) -> Project:
    return await make_project(db_session, "acme")


async def _apply(session: AsyncSession, when: datetime, batch: dict[ExitIpKey, ReportCounts]) -> None:
    await ExitReputationRepository(session).apply_reports(batch, when)
    await session.commit()


# The records the page tests read: avoided now, for a while, never, and long ago.
BAD = ("203.0.113.1", "google.com")
GOOD = ("203.0.113.2", "google.com")
OLD = ("203.0.113.6", "bing.com")
MIXED = ("203.0.113.7", "bing.com")
SIX = ("2001:db8::1", "google.com")


async def _the_records(session: AsyncSession, project: Project) -> None:
    def k(record: tuple[str, str]) -> ExitIpKey:
        return ExitIpKey(project.id, *record)

    await _apply(session, at(-24 * 30), {k(OLD): ReportCounts(bad=1, reason="captcha")})
    await _apply(
        session,
        T0,
        {
            k(BAD): ReportCounts(bad=1, reason="403"),
            k(GOOD): ReportCounts(good=1),
            k(MIXED): ReportCounts(good=3, bad=1),
            k(SIX): ReportCounts(bad=2),
        },
    )


def _named(records: list[ExitIpReputation]) -> set[tuple[str, str]]:
    return {(record.ip, record.site) for record in records}


class TestPage:
    async def test_each_status_filter_agrees_with_the_status_shown(
        self, db_session: AsyncSession, project: Project
    ) -> None:
        """At every moment, a filter selects exactly the records whose own status says it holds."""
        await _the_records(db_session, project)
        repository = ExitReputationRepository(db_session)

        for now in (at(0.5), at(1), at(30), at(24 * 40)):
            everything, total, exact = await repository.page(project.id, now)
            assert (total, exact) == (5, True)
            for status in ReputationStatus:
                found, _, _ = await repository.page(project.id, now, status=status)
                expected = {(r.ip, r.site) for r in everything if r.status(now) is status}
                assert _named(found) == expected, (status, now)

    async def test_what_holds_half_an_hour_in(self, db_session: AsyncSession, project: Project) -> None:
        await _the_records(db_session, project)
        repository = ExitReputationRepository(db_session)

        async def named(now: datetime, status: ReputationStatus) -> set[tuple[str, str]]:
            return _named((await repository.page(project.id, now, status=status))[0])

        assert await named(at(0.5), ReputationStatus.AVOIDED) == {BAD, SIX}
        assert await named(at(0.5), ReputationStatus.NONE) == {GOOD, MIXED, OLD}
        assert await named(at(1), ReputationStatus.AVOIDED) == set()

    async def test_an_ip_filter_is_a_whole_ip_or_the_start_of_one(
        self, db_session: AsyncSession, project: Project
    ) -> None:
        await _the_records(db_session, project)
        await _apply(db_session, T0, {ExitIpKey(project.id, "203.0.113.10", "google.com"): ReportCounts(bad=1)})
        repository = ExitReputationRepository(db_session)

        async def named(ip: str) -> set[str]:
            return {r.ip for r in (await repository.page(project.id, at(1), ip=ip))[0]}

        assert await named("203.0.113.1") == {"203.0.113.1"}
        assert await named(" 203.0.113.1") == {"203.0.113.1"}
        assert await named("203.0.113.") == {f"203.0.113.{n}" for n in (1, 2, 6, 7, 10)}
        assert await named("203.0.113.1_") == set()  # no wildcards
        # A whole IPv6 address matches its canonical text however it is written.
        assert await named("2001:0DB8:0:0::0001") == {"2001:db8::1"}
        assert await named("2001:DB8") == {"2001:db8::1"}

    async def test_a_site_filter_matches_any_part_of_the_site(self, db_session: AsyncSession, project: Project) -> None:
        await _the_records(db_session, project)
        await _apply(
            db_session,
            T0,
            {
                ExitIpKey(project.id, "203.0.113.1", "google.co.uk"): ReportCounts(bad=1),
                ExitIpKey(project.id, "203.0.113.1", "xn--bcher-kva.de"): ReportCounts(bad=1),
            },
        )
        repository = ExitReputationRepository(db_session)

        async def sites(site: str) -> set[str]:
            return {r.site for r in (await repository.page(project.id, at(1), site=site))[0]}

        assert await sites("GOOGLE") == {"google.com", "google.co.uk"}
        assert await sites("co.uk") == {"google.co.uk"}
        assert await sites("bücher") == {"xn--bcher-kva.de"}
        assert await sites("g%e") == set()
        assert await sites("bing") == {"bing.com"}

    async def test_the_sort_orders_and_ties_keep_pages_apart(self, db_session: AsyncSession, project: Project) -> None:
        def k(n: int) -> ExitIpKey:
            return ExitIpKey(project.id, f"198.51.100.{n}", "google.com")

        await _apply(db_session, T0, {k(1): ReportCounts(good=1, bad=1), k(2): ReportCounts(good=1)})
        await _apply(db_session, at(1), {k(3): ReportCounts(bad=5), k(4): ReportCounts(bad=1)})
        repository = ExitReputationRepository(db_session)

        async def ips(sort: ReputationSort) -> list[str]:
            return [r.ip for r in (await repository.page(project.id, at(2), sort=sort))[0]]

        assert await ips(ReputationSort.LAST_SEEN) == ["198.51.100.3", "198.51.100.4", "198.51.100.1", "198.51.100.2"]
        assert await ips(ReputationSort.REPORTS) == ["198.51.100.3", "198.51.100.1", "198.51.100.4", "198.51.100.2"]
        paged = [
            r.ip
            for offset in (0, 1, 2, 3)
            for r in (await repository.page(project.id, at(2), sort=ReputationSort.REPORTS, limit=1, offset=offset))[0]
        ]
        assert paged == await ips(ReputationSort.REPORTS)

    async def test_the_total_is_counted_only_so_far_past_the_page(
        self, db_session: AsyncSession, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reputation_repository, "COUNT_CAP", 3)
        await _apply(
            db_session, T0, {ExitIpKey(project.id, f"198.51.100.{n}", "google.com"): ReportCounts(bad=1) for n in range(8)}
        )
        repository = ExitReputationRepository(db_session)

        async def counted(limit: int, offset: int) -> tuple[int, int, bool]:
            found, total, exact = await repository.page(project.id, at(1), limit=limit, offset=offset)
            return len(found), total, exact

        assert await counted(2, 0) == (2, 3, False)
        assert await counted(2, 4) == (2, 7, False)
        assert await counted(2, 6) == (2, 8, True)
        assert await counted(5, 6) == (2, 8, True)
        # A page past the end is counted from the start.
        assert await counted(2, 20) == (0, 8, True)

    async def test_another_projects_records_never_show(self, db_session: AsyncSession, project: Project) -> None:
        other = await make_project(db_session, "other")
        await _apply(
            db_session,
            T0,
            {
                ExitIpKey(project.id, "203.0.113.1", "google.com"): ReportCounts(bad=1),
                ExitIpKey(other.id, "203.0.113.1", "google.com"): ReportCounts(good=1),
                ExitIpKey(other.id, "203.0.113.2", "google.com"): ReportCounts(good=1),
            },
        )

        found, total, _ = await ExitReputationRepository(db_session).page(project.id, at(1))

        assert [(r.project_id, r.ip, r.bad_reports) for r in found] == [(project.id, "203.0.113.1", 1)]
        assert total == 1


class TestOfIp:
    async def test_the_ips_sites_in_the_project_avoided_first(self, db_session: AsyncSession, project: Project) -> None:
        """Avoided now, then the rest, each the one seen last first; a wide crawl's IP is cut at the limit."""
        other = await make_project(db_session, "other", ExitReputationMode.AVOID)
        ip = "203.0.113.1"
        await _apply(
            db_session,
            T0,
            {ExitIpKey(project.id, ip, "google.com"): ReportCounts(bad=1), ExitIpKey(project.id, ip, "yahoo.com"): ReportCounts(good=1)},
        )
        await _apply(
            db_session,
            at(0.5),
            {
                ExitIpKey(project.id, ip, "bing.com"): ReportCounts(good=1),
                ExitIpKey(project.id, "203.0.113.2", "bing.com"): ReportCounts(good=1),
                ExitIpKey(other.id, ip, "duckduckgo.com"): ReportCounts(bad=1),
            },
        )
        repository = ExitReputationRepository(db_session)

        async def sites(now: datetime, limit: int = 100) -> list[str]:
            return [record.site for record in await repository.of_ip(project.id, ip, now, limit=limit)]

        assert await sites(at(0.75)) == ["google.com", "bing.com", "yahoo.com"]
        assert await sites(at(0.75), limit=1) == ["google.com"]
        assert await sites(at(2)) == ["bing.com", "google.com", "yahoo.com"]
        assert await repository.of_ip(project.id, "198.51.100.1", at(2), limit=100) == []


class TestSummaries:
    async def test_each_ip_summed_over_its_sites(self, db_session: AsyncSession, project: Project) -> None:
        other = await make_project(db_session, "other")
        first, second = "203.0.113.1", "203.0.113.2"
        await _apply(
            db_session,
            T0,
            {
                ExitIpKey(project.id, first, "google.com"): ReportCounts(bad=2, good=1),
                ExitIpKey(project.id, first, "bing.com"): ReportCounts(bad=1),
                ExitIpKey(project.id, first, "yahoo.com"): ReportCounts(good=4),
                ExitIpKey(project.id, second, "google.com"): ReportCounts(good=5),
                ExitIpKey(other.id, first, "google.com"): ReportCounts(bad=9),
            },
        )

        summaries = await ExitReputationRepository(db_session).summaries(
            project.id, {first, second, "198.51.100.1"}, at(0.5)
        )

        # google.com and bing.com avoided for an hour each; yahoo.com never.
        assert summaries == {
            first: ReputationSummary(first, 5, 3, 3, 2, at(1)),
            second: ReputationSummary(second, 5, 0, 1, 0, None),
        }
        later = await ExitReputationRepository(db_session).summaries(project.id, [first], at(2))
        assert later == {first: ReputationSummary(first, 5, 3, 3, 0, None)}
        assert await ExitReputationRepository(db_session).summaries(project.id, [], at(1)) == {}

    async def test_any_number_of_ips_is_one_statement(self, db_session: AsyncSession, project: Project) -> None:
        """A statement per batch size would leave a plan each in every pooled connection's backend."""
        repository = ExitReputationRepository(db_session)
        for size in (1, 2, 500):
            await repository.summaries(project.id, [f"10.9.{n // 250}.{n % 250}" for n in range(size)], T0)
        prepared = await db_session.execute(
            text(
                "SELECT count(DISTINCT statement) FROM pg_prepared_statements "
                "WHERE statement LIKE 'SELECT exit_ip_reputation.ip, sum(%'"
            )
        )

        assert prepared.scalar_one() == 1


class TestSitesAndPlaces:
    """The reads behind the traffic split's site and place pickers and the connectors view."""

    async def _reported(self, session: AsyncSession, project: Project) -> tuple[str, str]:
        """Reports through two connectors: per report, a record at every level of its place."""
        pool = await make_connector(session, project, "pool")
        dynamic = await make_connector(session, project, "dynamic")

        def levels(connector_id: str, site: str, place: str, counts: RequestCount) -> dict[ConnectorSiteKey, RequestCount]:
            return dict.fromkeys(ConnectorSiteKey(project.id, connector_id, site, place).levels(), counts)

        repository = ExitReputationRepository(session)
        await repository.apply_reports({ExitIpKey(project.id, "203.0.113.1", "google.com"): ReportCounts(bad=3)}, T0)
        await repository.apply_evidence(
            {
                **levels(pool.id, "google.com", "", RequestCount(blocked=3)),
                **levels(dynamic.id, "google.com", "US/NY/new_york", RequestCount(blocked=2, good=2)),
                **levels(dynamic.id, "bing.com", "GB", RequestCount(blocked=1)),
            },
            [dynamic.id],
            T0,
            timedelta(minutes=1),
        )
        await session.commit()
        return pool.id, dynamic.id

    async def test_sites_by_their_reports_each_counted_once(self, db_session: AsyncSession, project: Project) -> None:
        await self._reported(db_session, project)
        repository = ExitReputationRepository(db_session)

        # google.com: 3 through the pool, 4 through the dynamic connector, each once whatever its place's depth.
        assert await repository.busiest_sites(project.id, limit=20) == [("google.com", 7), ("bing.com", 1)]
        assert await repository.busiest_sites(project.id, limit=1) == [("google.com", 7)]

    async def test_places_by_their_reports_never_the_one_naming_none(
        self, db_session: AsyncSession, project: Project
    ) -> None:
        await self._reported(db_session, project)
        repository = ExitReputationRepository(db_session)

        assert await repository.busiest_places(project.id, "google.com", limit=20) == [
            ("US", 4), ("US/NY", 4), ("US/NY/new_york", 4),
        ]
        assert await repository.busiest_places(project.id, None, limit=20) == [
            ("US", 4), ("US/NY", 4), ("US/NY/new_york", 4), ("GB", 1),
        ]

    async def test_the_records_and_avoids_of_the_sites_asked_about(
        self, db_session: AsyncSession, project: Project
    ) -> None:
        pool, dynamic = await self._reported(db_session, project)
        repository = ExitReputationRepository(db_session)

        records = await repository.connector_records(project.id, ["google.com"], ["US/NY", "US", ""])
        assert [(record.connector_id, record.place, record.blocked_reports) for record in records] == sorted(
            [(pool, "", 3), (dynamic, "US", 2), (dynamic, "US/NY", 2)]
        )
        assert await repository.avoided_ips(project.id, ["google.com", "bing.com"], at(0.5)) == {
            "google.com": frozenset({"203.0.113.1"})
        }
        # Three bad reports avoid for "Avoid for", an hour at the default.
        assert await repository.avoided_ips(project.id, ["google.com"], at(2)) == {}

    async def test_every_place_of_a_site_for_the_connectors_named(self, db_session: AsyncSession, project: Project) -> None:
        pool, dynamic = await self._reported(db_session, project)
        other = await make_project(db_session, "globex")
        await self._reported(db_session, other)
        repository = ExitReputationRepository(db_session)

        records = await repository.connector_records(project.id, ["google.com"], None, connector_ids=[pool, dynamic])
        assert [(record.connector_id, record.site, record.place) for record in records] == sorted(
            [
                (pool, "google.com", ""),
                (dynamic, "google.com", "US"),
                (dynamic, "google.com", "US/NY"),
                (dynamic, "google.com", "US/NY/new_york"),
            ]
        )
        # Only the connectors named, and none of another project's.
        dynamic_only = await repository.connector_records(project.id, ["google.com"], None, connector_ids=[dynamic])
        assert [record.connector_id for record in dynamic_only] == [dynamic] * 3
        assert await repository.connector_records(project.id, ["google.com"], None, connector_ids=[]) == []
        assert await repository.busiest_sites(project.id) == [("google.com", 7), ("bing.com", 1)]


class TestEveryRecordingProjectAtOnce:
    """The reads the alerts job makes for every recording project at once agree with the one-project reads."""

    async def test_each_read_holds_what_each_project_s_own_read_finds(
        self, db_session: AsyncSession, project: Project
    ) -> None:
        other = await make_project(db_session, "globex")
        await TestSitesAndPlaces()._reported(db_session, project)
        await TestSitesAndPlaces()._reported(db_session, other)
        repository = ExitReputationRepository(db_session)
        since = at(-1)
        ids = [project.id, other.id]

        sites = await repository.recent_sites_of(ids, since, limit=8)
        assert sites == {owner: await repository.recent_sites(owner, since, limit=8) for owner in ids}
        assert sites[project.id] == ["google.com", "bing.com"]
        # The limit holds for each project.
        assert await repository.recent_sites_of(ids, since, limit=1) == {owner: ["google.com"] for owner in ids}
        assert await repository.recent_sites_of(ids, at(1), limit=8) == {}

        pairs = [(owner, site) for owner in ids for site in sites[owner]]
        assert await repository.avoided_ips_of(pairs, at(0.5)) == {
            (owner, site): ips
            for owner in ids
            for site, ips in (await repository.avoided_ips(owner, sites[owner], at(0.5))).items()
        }
        assert await repository.connector_records_of(pairs, ["US", ""]) == sorted(
            [
                record
                for owner in ids
                for record in await repository.connector_records(owner, sites[owner], ["US", ""])
            ],
            key=lambda record: record.key,
        )
        assert await repository.coarse_records_of(pairs, since) == sorted(
            [record for owner in ids for record in await repository.coarse_records(owner, sites[owner], since)],
            key=lambda record: record.key,
        )
        # Only the pairs asked about.
        assert await repository.connector_records_of([(project.id, "bing.com")], [""]) == []
        assert await repository.avoided_ips_of([], at(0.5)) == {}
