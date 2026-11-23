# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for exit reputation in routing: rows joined to the feed's records by their exit IP, avoided rows kept off a
site, sessions moved or refused, the site a refusal names, and the effective weights the strategies are handed.

The router reads the snapshot alone; sessions are bound in the sticky strategy's memory, as without Redis.
"""

import random
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import pytest

from api.core import utc_now
from api.feed.models import (
    GLOBAL_SECTION,
    REPUTATION_SECTION,
    RUNTIME_SECTION,
    ProjectReputation,
    ReputationSection,
    RuntimeSection,
    connector_section,
)
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.location import LocationTarget
from api.models.project import ExitReputationMode, Project
from api.models.proxy import Proxy, ProxyProtocol
from api.routing import snapshot as snapshot_module
from api.routing.exclusions import LocalExclusions
from api.routing.router import AvoidedSite, Router
from api.routing.snapshot import RoutingSnapshot
from api.strategies import STRATEGY_NAMES, ProxyGroup
from api.strategies.sticky import StickySessionStrategy
from tests.routing.builder import FeedBuilder, healthy

# The rows of the feed below and the IPs they were last seen exiting from: two port slots share one IP.
EXITS = {"p1": "203.0.113.1", "p2": "203.0.113.2", "p3": "203.0.113.2", "p4": "203.0.113.4"}


def _feed(
    mode: ExitReputationMode = ExitReputationMode.AVOID, strategy: str = "round_robin", *, move: bool = False
) -> FeedBuilder:
    """Project ``p`` with one static connector ``conn`` holding the rows of ``EXITS``, each healthy."""
    return FeedBuilder().add(
        Project(
            id="p", name="P", username="client", password="pw", routing_strategy=strategy,
            exit_reputation=mode, location_session_move=move,
        ),
        Credential(id="k", name="K", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="p"),
        Connector(
            id="conn", name="Conn", credential_id="k", credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="p",
        ),
        *(
            healthy(Proxy(id=row, host=row, port=1, connector_id="conn", metadata={"discovered_ip": ip}))
            for row, ip in EXITS.items()
        ),
    )


def _mixed(
    strategy: str = "round_robin", *, mode: ExitReputationMode = ExitReputationMode.AVOID, dynamic_weight: int = 1
) -> FeedBuilder:
    """``_feed``'s project with an Oxylabs dynamic-sessions connector ``dyn`` (one gateway row, ``gw``) beside ``conn``."""
    return _feed(mode, strategy).add(
        Credential(
            id="oxy", name="Oxylabs", type="oxylabs", project_id="p",
            config={"proxy_type": "residential", "username": "alice", "password": "s3cret"},
        ),
        Connector(
            id="dyn", name="Dyn", credential_id="oxy", credential_type="oxylabs", project_id="p",
            config={"session_mode": "dynamic", "session_duration_minutes": 10},
            routing_config={"weight": dynamic_weight},
        ),
        healthy(Proxy(
            id="gw", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
            username="customer-alice-sessid-probe0probe0-sesstime-10", password="{password}", connector_id="dyn",
            metadata={
                "provider": "oxylabs", "proxy_type": "residential", "session_id": "probe0probe0", "dynamic_sessions": "true",
            },
        )),
    )


def _two_dynamic(strength: int = 1, floor: int = 10) -> FeedBuilder:
    """``_mixed``'s feed with a second Oxylabs dynamic connector ``dyn2`` (gateway row ``gw2``), the project at
    ``strength`` and lowest factor ``floor`` percent."""
    feed = _mixed()
    feed.projects["p"] = feed.projects["p"].model_copy(
        update={"exit_reputation_strength": strength, "exit_reputation_floor": floor}
    )
    return feed.add(
        Connector(
            id="dyn2", name="Dyn2", credential_id="oxy", credential_type="oxylabs", project_id="p",
            config={"session_mode": "dynamic", "session_duration_minutes": 10},
        ),
        healthy(Proxy(
            id="gw2", host="pr.oxylabs.io", port=7777, protocol=ProxyProtocol.HTTP,
            username="customer-alice-sessid-probe1probe1-sesstime-10", password="{password}", connector_id="dyn2",
            metadata={
                "provider": "oxylabs", "proxy_type": "residential", "session_id": "probe1probe1", "dynamic_sessions": "true",
            },
        )),
    )


def _later(hours: float = 1) -> datetime:
    return utc_now() + timedelta(hours=hours)


async def _weights(
    router: Router,
    monkeypatch: pytest.MonkeyPatch,
    host: str | None,
    location: LocationTarget | None = None,
    session_id: str | None = None,
) -> dict[str, float]:
    """The connector weights the project's strategy is handed for one request, by connector."""
    strategy = router.strategy_for_project("p")
    handed: list[list[ProxyGroup]] = []
    select = strategy.select_weighted

    async def spy(groups: list[ProxyGroup], *args: Any, **kwargs: Any) -> Proxy | None:
        handed.append(groups)
        return await select(groups, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(strategy, "select_weighted", spy)
        await router.select_proxy_for_project("p", session_id, host, location)
    (groups,) = handed
    return {group.key: group.weight for group in groups}


async def _picks(router: Router, host: str | None, session_id: str | None = None, draws: int = 12) -> set[str | None]:
    picked = [await router.select_proxy_for_project("p", session_id, host) for _ in range(draws)]
    return {proxy.id if proxy is not None else None for proxy in picked}


class TestSnapshot:
    def test_rows_are_joined_to_the_records_by_their_exit_ip(self) -> None:
        feed = _feed().avoid("p", "203.0.113.2", "google.com", _later())
        snapshot = feed.snapshot()

        # Both slots on the IP are avoided; the other rows and other sites are not.
        assert snapshot.avoided("p", "google.com", utc_now()) == {"p2", "p3"}
        assert snapshot.avoided("p", "bing.com", utc_now()) == frozenset()
        assert snapshot.avoided("other-project", "google.com", utc_now()) == frozenset()

    def test_an_exit_is_known_by_its_canonical_ip_and_a_dynamic_gateway_by_none(self) -> None:
        feed = _feed().add(
            # A pooled slot last seen through an IPv4-mapped address, and a dynamic gateway whose stored IP means nothing.
            healthy(Proxy(id="slot", host="h", port=1, connector_id="conn", metadata={
                "session_id": "s1", "discovered_ip": "::ffff:203.0.113.9",
            })),
            healthy(Proxy(id="gateway", host="g", port=1, connector_id="conn", metadata={
                "dynamic_sessions": "true", "discovered_ip": "203.0.113.9",
            })),
        )
        feed.avoid("p", "203.0.113.9", "google.com", _later())
        assert feed.snapshot().avoided("p", "google.com", utc_now()) == {"slot"}

    def test_the_index_is_built_as_the_feed_is_applied_and_kept_while_what_it_rests_on_stays(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never on a request: a gateway joins the records to its rows when it applies an update, and an update that
        moves neither the project's records nor its connectors (a quarantine trip) joins nothing."""
        builds: list[ProjectReputation] = []
        build = snapshot_module._ReputationIndex.build

        def counted(source: ProjectReputation, views: Any) -> Any:
            builds.append(source)
            return build(source, views)

        monkeypatch.setattr(snapshot_module._ReputationIndex, "build", counted)
        feed = _feed().avoid("p", "203.0.113.2", "google.com", _later())
        sections = feed.sections()
        first = RoutingSnapshot.from_sections(sections, registry=feed.registry)
        assert len(builds) == 1
        assert first.avoided("p", "google.com", utc_now()) == {"p2", "p3"}
        assert len(builds) == 1

        tripped = {**sections, RUNTIME_SECTION: RuntimeSection(quarantined={"p1": _later()})}
        second = RoutingSnapshot.from_sections(tripped, registry=feed.registry, previous=first)
        assert second.avoided("p", "google.com", utc_now()) == {"p2", "p3"}
        assert len(builds) == 1

        # The connector's section changed: its rows may exit elsewhere now.
        moved = {**tripped, connector_section("conn"): feed.connector_section("conn")}
        RoutingSnapshot.from_sections(moved, registry=feed.registry, previous=second)
        assert len(builds) == 2
        # The section decoded anew with the same records, as a gateway decodes it on any change: kept, holding the copy.
        decoded = ReputationSection.model_validate(sections[REPUTATION_SECTION].model_dump())
        third = RoutingSnapshot.from_sections({**tripped, REPUTATION_SECTION: decoded}, registry=feed.registry, previous=second)
        assert len(builds) == 2
        assert third._reputation["p"].source is decoded.projects["p"]
        assert third.avoided("p", "google.com", utc_now()) == {"p2", "p3"}
        # The records changed.
        feed.avoid("p", "203.0.113.1", "google.com", _later())
        changed = {**tripped, REPUTATION_SECTION: feed.sections()[REPUTATION_SECTION]}
        fourth = RoutingSnapshot.from_sections(changed, registry=feed.registry, previous=third)
        assert len(builds) == 3
        assert fourth.avoided("p", "google.com", utc_now()) == {"p1", "p2", "p3"}

    def test_a_change_in_one_projects_records_indexes_that_project_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every change of the section reaches a gateway as a whole section decoded anew: a project whose records stayed
        keeps its index."""
        builds: list[ProjectReputation] = []
        build = snapshot_module._ReputationIndex.build

        def counted(source: ProjectReputation, views: Any) -> Any:
            builds.append(source)
            return build(source, views)

        monkeypatch.setattr(snapshot_module._ReputationIndex, "build", counted)
        feed = _feed().avoid("p", "203.0.113.2", "google.com", _later())
        feed.add(
            Project(id="q", name="Q", username="other", password="pw", exit_reputation=ExitReputationMode.AVOID),
            Credential(id="kq", name="KQ", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="q"),
            Connector(
                id="cq", name="CQ", credential_id="kq", credential_type=CredentialType.STATIC_PROXY_PROVIDER,
                project_id="q",
            ),
            healthy(Proxy(id="q1", host="q1", port=1, connector_id="cq", metadata={"discovered_ip": "198.51.100.1"})),
        )
        feed.avoid("q", "198.51.100.1", "google.com", _later())
        sections = feed.sections()
        first = RoutingSnapshot.from_sections(sections, registry=feed.registry)
        assert len(builds) == 2

        feed.avoid("p", "203.0.113.4", "google.com", _later())
        changed = {**sections, REPUTATION_SECTION: feed.sections()[REPUTATION_SECTION]}
        second = RoutingSnapshot.from_sections(changed, registry=feed.registry, previous=first)

        assert [source.ip_avoids.keys() for source in builds[2:]] == [{"203.0.113.2", "203.0.113.4"}]
        assert second._reputation["q"] is not first._reputation["q"]
        assert second._reputation["q"].avoided is first._reputation["q"].avoided
        assert second.avoided("q", "google.com", utc_now()) == {"q1"}
        assert second.avoided("p", "google.com", utc_now()) == {"p2", "p3", "p4"}

    def test_an_end_counts_until_it_passes(self) -> None:
        now = utc_now()
        feed = _feed().avoid("p", "203.0.113.1", "google.com", now + timedelta(minutes=5))
        feed.avoid("p", "203.0.113.4", "google.com", now + timedelta(minutes=10))
        snapshot = feed.snapshot()

        first = snapshot.avoided("p", "google.com", now)
        assert first == {"p1", "p4"}
        # Worked out once until the first end: the same set again.
        assert snapshot.avoided("p", "google.com", now + timedelta(minutes=1)) is first
        assert snapshot.avoided("p", "google.com", now + timedelta(minutes=6)) == {"p4"}
        assert snapshot.avoided("p", "google.com", now + timedelta(minutes=11)) == frozenset()


class TestAvoid:
    async def test_avoided_rows_are_kept_off_the_site_only(self) -> None:
        router = _feed().avoid("p", "203.0.113.2", "www.google.com", _later()).router()

        # The site's requests keep off them, however the host is written; any other host's use them, google.com's too.
        assert await _picks(router, "www.google.com") == {"p1", "p4"}
        assert await _picks(router, "WWW.Google.com.:443") == {"p1", "p4"}
        for host in ("google.com", "maps.google.com", "example.com", None):
            assert await _picks(router, host) == {"p1", "p2", "p3", "p4"}, host
        ids = {proxy.id for proxy in router.get_routable_proxies_for_project("p", target_host="www.google.com")}
        assert ids == {"p1", "p4"}

    async def test_a_record_for_a_domain_leaves_its_other_hosts_alone(self) -> None:
        """A site is the exact host: an avoid for google.com says nothing about www.google.com."""
        router = _feed().avoid("p", "203.0.113.2", "google.com", _later()).router()

        assert await _picks(router, "google.com") == {"p1", "p4"}
        assert await _picks(router, "www.google.com") == {"p1", "p2", "p3", "p4"}
        assert await router.select_proxy_for_project("p", None, "www.google.com", pinned_proxy_id="p2") is not None
        assert await router.avoided_site("p", "www.google.com", pinned_proxy_id="p2") is None

    @pytest.mark.parametrize("mode", [ExitReputationMode.OFF, ExitReputationMode.RECORD])
    async def test_a_project_that_does_not_avoid_routes_as_before(self, mode: ExitReputationMode) -> None:
        feed = _feed(ExitReputationMode.AVOID).avoid("p", "203.0.113.2", "google.com", _later())
        # A mode turned off after the read: the section still carries the avoid, the project no longer applies it.
        router = feed.router()
        feed.projects["p"] = feed.projects["p"].model_copy(update={"exit_reputation": mode})
        router.swap(feed.snapshot(previous=router.snapshot))
        assert await _picks(router, "google.com") == {"p1", "p2", "p3", "p4"}

    async def test_an_avoid_that_ended_is_routed_again(self) -> None:
        router = _feed().avoid("p", "203.0.113.2", "google.com", utc_now() - timedelta(seconds=1)).router()
        assert await _picks(router, "google.com") == {"p1", "p2", "p3", "p4"}

    async def test_a_pinned_avoided_row_serves_nothing(self) -> None:
        router = _feed().avoid("p", "203.0.113.1", "google.com", _later()).router()

        assert await router.select_proxy_for_project("p", None, "google.com", pinned_proxy_id="p1") is None
        assert await router.avoided_site("p", "google.com", pinned_proxy_id="p1") == AvoidedSite("google.com")
        # Elsewhere it serves, and a row not avoided serves the site.
        assert await router.select_proxy_for_project("p", None, "bing.com", pinned_proxy_id="p1") is not None
        assert await router.avoided_site("p", "google.com", pinned_proxy_id="p4") is None


class TestRefusal:
    async def test_every_eligible_row_avoided_names_the_site(self) -> None:
        feed = _feed()
        for ip in set(EXITS.values()):
            feed.avoid("p", ip, "www.google.com", _later())
        router = feed.router()

        assert await router.select_proxy_for_project("p", None, "www.google.com") is None
        assert await router.avoided_site("p", "WWW.Google.com.") == AvoidedSite("www.google.com")
        assert await router.are_all_proxies_quarantined("p", "www.google.com") is False
        assert router.traffic_limit_status("p", "www.google.com") is None

    async def test_the_site_is_named_only_when_the_avoids_are_why(self) -> None:
        router = _feed().avoid("p", "203.0.113.1", "google.com", _later()).router()
        # Something still serves the site.
        assert await router.avoided_site("p", "google.com") is None
        # Nothing serves the place at all.
        assert await router.avoided_site("p", "google.com", location=LocationTarget(country="DE")) is None
        # No site, or a project that does not avoid.
        assert await router.avoided_site("p", None) is None

    async def test_rows_resting_are_not_counted(self) -> None:
        """One row quarantined and the others avoided: the request is refused for the site, not told to retry."""
        exclusions = LocalExclusions()
        feed = _feed()
        for ip in ("203.0.113.2", "203.0.113.4"):
            feed.avoid("p", ip, "google.com", _later())
        router = feed.router(exclusions=exclusions)
        exclusions.quarantine("p1", _later())

        assert await router.select_proxy_for_project("p", None, "google.com") is None
        assert await router.are_all_proxies_quarantined("p", "google.com") is False
        assert await router.avoided_site("p", "google.com") == AvoidedSite("google.com")

    async def test_every_row_resting_is_a_quarantine_whatever_is_avoided(self) -> None:
        exclusions = LocalExclusions()
        router = _feed().avoid("p", "203.0.113.1", "google.com", _later()).router(exclusions=exclusions)
        for row in EXITS:
            exclusions.quarantine(row, _later())
        assert await router.are_all_proxies_quarantined("p", "google.com") is True


class TestSessions:
    async def test_a_session_moves_off_its_avoided_row_when_the_project_moves_refused_sessions(self) -> None:
        feed = _feed(strategy="sticky", move=True)
        router = feed.router()
        first = await router.select_proxy_for_project("p", "session-1", "google.com")
        assert first is not None
        feed.avoid("p", EXITS[first.id], "google.com", _later())
        router.swap(feed.snapshot(previous=router.snapshot))

        moved = await router.select_proxy_for_project("p", "session-1", "google.com")

        assert moved is not None and EXITS[moved.id] != EXITS[first.id]
        assert await router.avoided_site("p", "google.com", "session-1") is None
        # The binding moved with it: the session keeps its new row for every site.
        strategy = router.strategy_for_project("p")
        assert isinstance(strategy, StickySessionStrategy) and strategy.bound_proxy_id("session-1") == moved.id

    async def test_a_session_on_an_avoided_row_is_refused_when_the_project_does_not_move_it(self) -> None:
        feed = _feed(strategy="sticky")
        router = feed.router()
        first = await router.select_proxy_for_project("p", "session-1", "google.com")
        assert first is not None
        feed.avoid("p", EXITS[first.id], "google.com", _later())
        router.swap(feed.snapshot(previous=router.snapshot))

        assert await router.select_proxy_for_project("p", "session-1", "google.com") is None
        # The refusal names the session's exit: a new session would be served.
        assert await router.avoided_site("p", "google.com", "session-1") == AvoidedSite("google.com", EXITS[first.id])
        # It keeps its row for other sites, and its binding.
        again = await router.select_proxy_for_project("p", "session-1", "bing.com")
        assert again is not None and again.id == first.id
        # Another session is placed on a row not avoided.
        other = await router.select_proxy_for_project("p", "session-2", "google.com")
        assert other is not None and EXITS[other.id] != EXITS[first.id]

    async def test_a_refused_session_stays_held_until_the_avoid_ends(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A session sending only the avoided site's requests is in use: its binding never goes idle and moves."""
        feed = _feed(strategy="sticky")
        router = feed.router()
        now = [0.0]
        monkeypatch.setattr(router.strategy_for_project("p"), "_clock", lambda: now[0])
        first = await router.select_proxy_for_project("p", "session-1", "google.com")
        assert first is not None
        feed.avoid("p", EXITS[first.id], "google.com", _later())
        router.swap(feed.snapshot(previous=router.snapshot))

        # One refused request a minute for ten minutes: twice the time a binding outlives its session's last request.
        for minute in range(1, 11):
            now[0] = minute * 60.0
            assert await router.select_proxy_for_project("p", "session-1", "google.com") is None
        again = await router.select_proxy_for_project("p", "session-1", "bing.com")
        assert again is not None and again.id == first.id

    async def test_a_session_whose_row_left_is_placed_again_not_held(self) -> None:
        """Held only to a row that would still serve it: a retired slot holds no session."""
        exclusions = LocalExclusions()
        feed = _feed(strategy="sticky")
        router = feed.router(exclusions=exclusions)
        first = await router.select_proxy_for_project("p", "session-1", "google.com")
        assert first is not None
        feed.avoid("p", EXITS[first.id], "google.com", _later())
        router.swap(feed.snapshot(previous=router.snapshot))
        exclusions.retire(first.id)

        moved = await router.select_proxy_for_project("p", "session-1", "google.com")
        assert moved is not None and EXITS[moved.id] != EXITS[first.id]

    async def test_a_strategy_that_holds_no_session_never_refuses_one(self) -> None:
        router = _feed(strategy="round_robin").avoid("p", "203.0.113.1", "google.com", _later()).router()
        assert await _picks(router, "google.com", "session-1") == {"p2", "p3", "p4"}
        assert await router.avoided_site("p", "google.com", "session-1") is None


class TestEffectiveWeights:
    """For a request with a site in a project that avoids, each connector's weight is multiplied by its reputation
    there: a dynamic connector's factor at the finest level of the requested place that has one, and for a connector
    with IPs the share of its eligible rows not avoided for the site."""

    async def test_a_dynamic_connector_takes_the_factor_of_the_finest_level_that_has_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        feed = _mixed(dynamic_weight=2)
        for place, factor in {"": 0.3, "US": 0.5, "US/NY": 1.0, "US//austin": 0.25}.items():
            feed.factor("p", "dyn", "www.google.com", place, factor)
        router = feed.router()
        cases: list[tuple[LocationTarget | None, float]] = [
            # A request naming no place uses the "" level alone.
            (None, 2 * 0.3),
            (LocationTarget(country="US"), 2 * 0.5),
            # A 1.0 for a state wins over the 0.5 of its country, for its cities too.
            (LocationTarget(country="US", state="NY"), 2.0),
            (LocationTarget(country="US", state="NY", city="new_york"), 2.0),
            # A state with no factor of its own falls back to its country.
            (LocationTarget(country="US", state="CA"), 2 * 0.5),
            (LocationTarget(country="US", city="austin"), 2 * 0.25),
            # A country with none keeps the weight: no figure for all countries stands in.
            (LocationTarget(country="DE"), 2.0),
        ]
        for location, expected in cases:
            weights = await _weights(router, monkeypatch, "www.google.com", location)
            assert weights["dyn"] == pytest.approx(expected), location
        # Another site has no factor.
        assert (await _weights(router, monkeypatch, "bing.com"))["dyn"] == 2

    async def test_a_connector_with_ips_takes_the_share_of_its_eligible_rows_not_avoided(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two of its four rows exit from the avoided IP.
        feed = _mixed().avoid("p", "203.0.113.2", "www.google.com", _later())
        router = feed.router()

        assert await _weights(router, monkeypatch, "www.google.com") == {"conn": 0.5, "dyn": 1.0}
        assert await _weights(router, monkeypatch, "example.com") == {"conn": 1.0, "dyn": 1.0}
        # Every row avoided: the connector takes no part of the site's requests, the other all of them.
        for ip in set(EXITS.values()):
            feed.avoid("p", ip, "www.google.com", _later())
        router.swap(feed.snapshot(previous=router.snapshot))
        assert await _weights(router, monkeypatch, "www.google.com") == {"dyn": 1.0}

    async def test_the_configured_weights_hold_without_a_site_or_where_the_project_does_not_avoid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        feed = _mixed().avoid("p", "203.0.113.2", "www.google.com", _later())
        feed.factor("p", "dyn", "www.google.com", "", 0.3)
        router = feed.router()
        assert await _weights(router, monkeypatch, None) == {"conn": 1, "dyn": 1}

        # Switched to record after the read: the section still carries it, and the project's mode decides.
        sections = feed.sections()
        feed.projects["p"] = feed.projects["p"].model_copy(update={"exit_reputation": ExitReputationMode.RECORD})
        sections[GLOBAL_SECTION] = feed.sections()[GLOBAL_SECTION]
        router.swap(RoutingSnapshot.from_sections(sections, registry=feed.registry, previous=router.snapshot))
        assert await _weights(router, monkeypatch, "www.google.com") == {"conn": 1, "dyn": 1}

    @pytest.mark.parametrize("strategy", STRATEGY_NAMES)
    async def test_every_strategy_is_handed_the_effective_weights(
        self, strategy: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        feed = _mixed(strategy).avoid("p", "203.0.113.2", "www.google.com", _later())
        feed.factor("p", "dyn", "www.google.com", "", 0.4)
        router = feed.router()

        weights = await _weights(router, monkeypatch, "www.google.com", session_id="s1")

        assert weights == pytest.approx({"conn": 0.5, "dyn": 0.4})

    async def test_groups_are_marked_lowered_only_where_reputation_lowered_their_weight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        feed = _mixed().avoid("p", "203.0.113.2", "www.google.com", _later())
        feed.factor("p", "dyn", "www.google.com", "", 0.4)
        feed.factor("p", "dyn", "www.bing.com", "", 1.0)
        router = feed.router()
        strategy = router.strategy_for_project("p")
        handed: list[dict[str, bool]] = []
        select = strategy.select_weighted

        async def spy(groups: list[ProxyGroup], *args: Any, **kwargs: Any) -> Proxy | None:
            handed.append({group.key: group.lowered for group in groups})
            return await select(groups, *args, **kwargs)

        monkeypatch.setattr(strategy, "select_weighted", spy)
        for host in ("www.google.com", "www.bing.com", None):
            await router.select_proxy_for_project("p", None, host)

        assert handed == [{"conn": True, "dyn": True}, {"conn": False, "dyn": False}, {"conn": False, "dyn": False}]

    async def test_at_strength_1_a_dynamic_connector_keeps_its_own_factor_never_under_the_lowest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        feed = _two_dynamic()
        feed.factor("p", "dyn", "www.google.com", "", 0.88).factor("p", "dyn2", "www.google.com", "", 0.985)
        feed.factor("p", "dyn", "www.bing.com", "", 0.05).factor("p", "dyn2", "www.bing.com", "", 1.0)
        router = feed.router()

        assert await _weights(router, monkeypatch, "www.google.com") == {"conn": 1.0, "dyn": 0.88, "dyn2": 0.985}
        assert await _weights(router, monkeypatch, "www.bing.com") == {"conn": 1.0, "dyn": 0.1, "dyn2": 1.0}

    async def test_at_strength_10_a_dynamic_connector_is_weighed_against_the_best_at_the_place(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        feed = _two_dynamic(strength=10)
        feed.factor("p", "dyn", "www.google.com", "US", 0.88).factor("p", "dyn2", "www.google.com", "US", 0.985)
        # In Germany the first is the better one; in France only it is judged.
        feed.factor("p", "dyn", "www.google.com", "DE", 0.95).factor("p", "dyn2", "www.google.com", "DE", 0.5)
        feed.factor("p", "dyn", "www.google.com", "FR", 0.88)
        router = feed.router()

        us = await _weights(router, monkeypatch, "www.google.com", LocationTarget(country="US"))
        # The pool's rows name no country: it takes none of these requests.
        assert us == pytest.approx({"dyn": 0.88 * (0.88 / 0.985) ** 9, "dyn2": 0.985})
        # The split between the two moves from 47% / 53% at strength 1 to about 24% / 76%.
        assert us["dyn"] / (us["dyn"] + us["dyn2"]) == pytest.approx(0.24, abs=0.01)
        de = await _weights(router, monkeypatch, "www.google.com", LocationTarget(country="DE"))
        # 0.5 against 0.95 to the ninth is far under the lowest factor.
        assert de == pytest.approx({"dyn": 0.95, "dyn2": 0.1})
        # Alone among the judged, a connector is the best: its own success; the other keeps its weight.
        fr = await _weights(router, monkeypatch, "www.google.com", LocationTarget(country="FR"))
        assert fr == pytest.approx({"dyn": 0.88, "dyn2": 1.0})

    async def test_round_robin_gives_a_site_its_split_whatever_the_requests_between(self) -> None:
        """Round robin's order is shared by every request of the project, so requests to a site whose weights exit
        reputation lowered are drawn by them: alternating with another site's requests, the connector lowered there
        still gets its share of the site's requests, and not every one of them."""
        random.seed(11)
        feed = _mixed("round_robin")
        feed.factor("p", "dyn", "www.google.com", "", 0.1)
        router = feed.router()
        to_google: Counter[str] = Counter()
        for _ in range(1000):
            proxy = await router.select_proxy_for_project("p", None, "www.google.com")
            assert proxy is not None
            to_google[proxy.connector_id] += 1
            assert await router.select_proxy_for_project("p", None, "example.com") is not None
        # 0.1 against 1: about one in eleven.
        assert 0.05 < to_google["dyn"] / 1000 < 0.15

    async def test_sticky_places_new_sessions_by_them_and_keeps_the_bound_ones(self) -> None:
        feed = _mixed("sticky")
        router = feed.router()
        bound: dict[str, str] = {}
        for n in range(40):
            proxy = await router.select_proxy_for_project("p", f"s{n}", "www.google.com")
            assert proxy is not None
            bound[f"s{n}"] = proxy.connector_id
        on_dynamic = [session for session, connector in bound.items() if connector == "dyn"]
        assert on_dynamic

        feed.factor("p", "dyn", "www.google.com", "", 0.1)
        router.swap(feed.snapshot(previous=router.snapshot))

        # A session bound before keeps its connector while its row is eligible, whatever the weights say.
        for session in on_dynamic:
            again = await router.select_proxy_for_project("p", session, "www.google.com")
            assert again is not None and again.connector_id == "dyn"
        # New sessions are placed by 0.1 against 1: about one in eleven.
        placed: Counter[str] = Counter()
        for n in range(400):
            proxy = await router.select_proxy_for_project("p", f"new-{n}", "www.google.com")
            assert proxy is not None
            placed[proxy.connector_id] += 1
        assert placed["dyn"] / 400 < 0.2
