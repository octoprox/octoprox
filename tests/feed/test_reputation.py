# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The exit reputation section the controller publishes: what a compile gives it to be read for, which projects then
owe a read, what a read carries (only the factors that change what routing takes), and the install-wide caps.

The records come from a stand-in repository, so these run without Postgres; tests/feed/test_compiler.py reads real ones.
"""

from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from itertools import product
from typing import Any

import pytest
import structlog

from api.feed import reputation as feed_reputation
from api.feed.models import ConnectorSection, ProjectReputation, ReputationSection
from api.feed.reputation import FeedReputation, ReputationScope
from api.models.connector import Connector
from api.models.location import LocationTarget
from api.models.project import ExitReputationMode, Project
from api.models.proxy import Proxy
from api.reputation.record import ConnectorSiteReputation, ExitIpReputation
from api.reputation.weight import ConnectorReputation

NOW = datetime(2026, 11, 15, 12, 0)
LATER = NOW + timedelta(hours=1)


class Records:
    """What the stand-in repository holds, and the reads made of it."""

    def __init__(self) -> None:
        self.avoids: list[ExitIpReputation] = []
        self.factors: list[ConnectorSiteReputation] = []
        self.reads: list[tuple[str, Any]] = []

    def avoid(self, project_id: str, ip: str, site: str, end: datetime = LATER) -> None:
        self.avoids.append(
            ExitIpReputation(
                project_id=project_id, ip=ip, site=site, avoid_until=end,
                weighted_at=NOW, first_seen_at=NOW, last_seen_at=NOW, last_seen_day=NOW.date(),
            )
        )

    def factor(self, project_id: str, connector_id: str, site: str, place: str, factor: float) -> None:
        self.factors.append(
            ConnectorSiteReputation(
                project_id=project_id, connector_id=connector_id, site=site, place=place, factor=factor,
                weighted_at=NOW, first_seen_at=NOW, last_seen_at=NOW, last_seen_day=NOW.date(),
            )
        )


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> Records:
    """The records the reads find, through a repository standing in for Postgres's."""
    held = Records()

    class Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def ip_avoids(
            self, exits: Mapping[str, Collection[str]], now: datetime, *, per_project: int, per_ip: int = 100
        ) -> list[ExitIpReputation]:
            held.reads.append(("ip_avoids", {project: sorted(ips) for project, ips in exits.items()}))
            return [
                record
                for record in held.avoids
                if record.ip in exits.get(record.project_id, ()) and record.avoided_until(now) is not None
            ]

        async def connector_factors(self, connector_ids: Collection[str]) -> list[ConnectorSiteReputation]:
            held.reads.append(("connector_factors", sorted(connector_ids)))
            return [record for record in held.factors if record.connector_id in connector_ids]

    monkeypatch.setattr(feed_reputation, "ExitReputationRepository", Repository)
    monkeypatch.setattr(feed_reputation, "utc_now", lambda: NOW)
    return held


@asynccontextmanager
async def _session() -> AsyncIterator[None]:
    yield None


def _reputation() -> FeedReputation:
    return FeedReputation(_session)  # type: ignore[arg-type]


def _project(project_id: str, mode: ExitReputationMode = ExitReputationMode.AVOID) -> Project:
    return Project(id=project_id, name=project_id, username=project_id, password="pw", exit_reputation=mode)


def _section(connector_id: str, project_id: str, *rows: dict[str, str]) -> ConnectorSection:
    """A connector section of the project whose rows carry ``rows`` as their metadata."""
    connector = Connector(
        id=connector_id, name=connector_id, credential_id="k", credential_type="static_proxy_provider",
        project_id=project_id,
    )
    return ConnectorSection(
        connector=connector,
        proxies=[Proxy(id=f"{connector_id}-{n}", host="h", port=1, connector_id=connector_id, metadata=meta) for n, meta in enumerate(rows)],
    )


GATEWAY = {"dynamic_sessions": "true", "discovered_ip": "198.51.100.9"}


class TestScope:
    def test_a_project_that_avoids_is_read_for_the_exit_ips_of_its_rows_and_its_dynamic_connectors(self) -> None:
        sections = [
            _section("pool", "p", {"discovered_ip": "203.0.113.1"}, {"discovered_ip": "::ffff:203.0.113.2"}, {}),
            # A pooled slot counts by its last sighting; a dynamic gateway's stored IP means nothing.
            _section("dyn", "p", GATEWAY, {"session_id": "s1", "discovered_ip": "203.0.113.3"}),
            _section("theirs", "q", {"discovered_ip": "203.0.113.9"}),
        ]

        scopes = ReputationScope.of([_project("p"), _project("q", ExitReputationMode.RECORD)], sections)

        assert scopes == {
            "p": ReputationScope(frozenset({"203.0.113.1", "203.0.113.2", "203.0.113.3"}), frozenset({"dyn"}))
        }

    def test_a_project_that_avoids_with_nothing_to_read_for_is_in_scope(self) -> None:
        """So its switch to avoid owes a read once a connector comes."""
        assert ReputationScope.of([_project("p")], []) == {"p": ReputationScope()}


class TestReads:
    async def test_a_compile_names_the_projects_whose_scope_is_new_or_moved(
        self, records: Records, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reputation = _reputation()
        scope = ReputationScope(frozenset({"203.0.113.1"}), frozenset({"dyn"}))
        builds: list[None] = []
        build = reputation._build

        def counted() -> None:
            builds.append(None)
            build()

        monkeypatch.setattr(reputation, "_build", counted)

        assert reputation.adopt({"p": scope, "q": ReputationScope()}) == {"p", "q"}
        # A compile that moved no scope builds nothing.
        assert reputation.adopt({"p": scope, "q": ReputationScope()}) == frozenset()
        assert len(builds) == 1
        assert reputation.adopt({"p": scope._replace(ips=frozenset({"203.0.113.2"})), "q": ReputationScope()}) == {"p"}
        # A project that stops avoiding owes nothing: it leaves.
        assert reputation.adopt({"q": ReputationScope()}) == frozenset()
        assert records.reads == []

    async def test_a_read_carries_the_avoids_of_the_ips_in_scope_and_the_factors_of_its_dynamic_connectors(
        self, records: Records
    ) -> None:
        records.avoid("p", "203.0.113.1", "www.google.com")
        records.avoid("p", "203.0.113.9", "www.google.com")
        records.avoid("p", "203.0.113.1", "bing.com", NOW - timedelta(minutes=1))
        records.factor("p", "dyn", "www.google.com", "US", 0.5)
        reputation = _reputation()
        reputation.adopt({"p": ReputationScope(frozenset({"203.0.113.1"}), frozenset({"dyn"}))})
        assert reputation.section() == ReputationSection()

        await reputation.read()

        assert reputation.section() == ReputationSection(
            projects={
                "p": ProjectReputation(
                    ip_avoids={"203.0.113.1": {"www.google.com": LATER}},
                    connector_factors={"dyn": {"www.google.com": {"US": 0.5}}},
                )
            }
        )
        assert records.reads == [
            ("ip_avoids", {"p": ["203.0.113.1"]}),
            ("connector_factors", ["dyn"]),
        ]

    async def test_a_read_of_one_project_leaves_the_others_as_last_read(self, records: Records) -> None:
        records.avoid("p", "203.0.113.1", "www.google.com")
        records.avoid("q", "203.0.113.2", "www.google.com")
        reputation = _reputation()
        reputation.adopt(
            {"p": ReputationScope(frozenset({"203.0.113.1"})), "q": ReputationScope(frozenset({"203.0.113.2"}))}
        )
        await reputation.read()
        records.avoids.clear()
        records.reads.clear()

        await reputation.read(["p"])

        assert records.reads == [("ip_avoids", {"p": ["203.0.113.1"]})]
        assert list(reputation.section().projects) == ["q"]

    async def test_what_a_scope_lost_leaves_the_section_at_the_compile(self, records: Records) -> None:
        records.avoid("p", "203.0.113.1", "www.google.com")
        records.avoid("p", "203.0.113.2", "www.google.com")
        records.factor("p", "dyn", "www.google.com", "", 0.5)
        reputation = _reputation()
        reputation.adopt({"p": ReputationScope(frozenset({"203.0.113.1", "203.0.113.2"}), frozenset({"dyn"}))})
        await reputation.read()

        reputation.adopt({"p": ReputationScope(frozenset({"203.0.113.2"}))})

        assert reputation.section().projects["p"] == ProjectReputation(ip_avoids={"203.0.113.2": {"www.google.com": LATER}})
        reputation.adopt({})
        assert reputation.section() == ReputationSection()

    async def test_nothing_is_read_for_projects_with_nothing_to_read_for(self, records: Records) -> None:
        reputation = _reputation()
        reputation.adopt({"p": ReputationScope()})
        await reputation.read()
        assert records.reads == []

    async def test_a_read_that_finds_the_same_keeps_the_section_object(self, records: Records) -> None:
        """The publisher re-encodes a section only when it is another object."""
        records.avoid("p", "203.0.113.1", "www.google.com")
        reputation = _reputation()
        reputation.adopt({"p": ReputationScope(frozenset({"203.0.113.1"}))})
        await reputation.read()
        section = reputation.section()

        await reputation.read()

        assert reputation.section() is section


class TestFactors:
    async def test_only_the_factors_that_change_what_routing_takes_are_carried(self, records: Records) -> None:
        stored = {
            # 1 at no place is what a request naming none gets anyway.
            "": 1.0,
            "US": 0.5,
            # A 1.0 for a state must win over its country's 0.5.
            "US/NY": 1.0,
            # The same as its state gives.
            "US/NY/new_york": 1.0,
            "US/CA": 0.5,
            "US/CA/los_angeles": 0.25,
            "GB": 1.0,
            "US//austin": 0.5,
        }
        for place, factor in stored.items():
            records.factor("p", "dyn", "www.google.com", place, factor)
        reputation = _reputation()
        reputation.adopt({"p": ReputationScope(connectors=frozenset({"dyn"}))})

        await reputation.read()

        carried = reputation.section().projects["p"].connector_factors["dyn"]["www.google.com"]
        assert carried == {"US": 0.5, "US/NY": 1.0, "US/CA/los_angeles": 0.25}
        # Every place a request may name gets the same weight from what is carried as from every stored factor.
        places = [""] + [
            LocationTarget(country=country, state=state or None, city=city or None).key
            for country, state, city in product(("US", "GB", "DE"), ("", "NY", "CA", "TX"), ("", "new_york", "austin"))
        ]
        for place in places:
            weighed = ConnectorReputation.by_block_rate(carried, place).value
            assert weighed == ConnectorReputation.by_block_rate(stored, place).value, place


class TestCaps:
    async def test_the_section_is_cut_to_the_caps_and_the_cut_is_logged_once(
        self, records: Records, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(feed_reputation, "MAX_FEED_IP_AVOIDS", 2)
        monkeypatch.setattr(feed_reputation, "MAX_FEED_FACTORS", 2)
        for n in range(1, 4):
            records.avoid("p", f"203.0.113.{n}", "www.google.com", NOW + timedelta(hours=n))
        records.factor("p", "dyn", "www.google.com", "US/NY", 0.25)
        records.factor("p", "dyn", "www.google.com", "US", 0.75)
        records.factor("p", "dyn", "www.google.com", "GB", 0.5)
        reputation = _reputation()
        reputation.adopt(
            {"p": ReputationScope(frozenset({"203.0.113.1", "203.0.113.2", "203.0.113.3"}), frozenset({"dyn"}))}
        )

        with structlog.testing.capture_logs() as logs:
            await reputation.read()
            await reputation.read()

        carried = reputation.section().projects["p"]
        # The avoids ending last, and the countries' factors before a state's, the lowest first.
        assert carried.ip_avoids == {
            "203.0.113.2": {"www.google.com": NOW + timedelta(hours=2)},
            "203.0.113.3": {"www.google.com": NOW + timedelta(hours=3)},
        }
        assert carried.connector_factors == {"dyn": {"www.google.com": {"GB": 0.5, "US": 0.75}}}
        assert [entry["event"] for entry in logs] == ["Exit reputation cut to fit the routing feed"]
