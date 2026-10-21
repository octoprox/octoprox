# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the router beyond the location rules.

Local exclusions and feed quarantines in eligibility, the answers a request
that found nothing gets (429, the traffic limit's status, or 502), strategy
objects across snapshot swaps, the counters strategies route on, domain
filters, and sticky sessions held to a quarantined proxy, the latter against
the real Redis container because the binding is read from Redis.
"""

import asyncio
from datetime import timedelta

import pytest

from api.core import utc_now
from api.db.redis import RedisClient
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.routing.exclusions import LocalExclusions
from api.routing.inflight import InFlight
from api.routing.outcomes import LocalOutcomes
from api.routing.router import Router
from api.strategies.round_robin import RoundRobinStrategy
from api.strategies.sticky import StickySessionStrategy
from tests.routing.builder import FeedBuilder, healthy

STICKY_RATE_LIMIT = {
    "max_requests": 5,
    "window_seconds": 60,
    "quarantine_seconds_min": 300,
    "quarantine_seconds_max": 300,
    "sticky_quarantine": True,
}


def _feed(strategy: str = "round_robin", **connector_fields: object) -> FeedBuilder:
    """One project with one static connector ``conn`` and two healthy proxies, ``p1`` and ``p2``."""
    return FeedBuilder().add(
        Project(id="p", name="P", username="client", password="pw", routing_strategy=strategy),
        Credential(id="k", name="K", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="p"),
        Connector(
            id="conn", name="Conn", credential_id="k", credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id="p", **connector_fields,  # type: ignore[arg-type]
        ),
        healthy(Proxy(id="p1", host="h1", port=1, connector_id="conn")),
        healthy(Proxy(id="p2", host="h2", port=1, connector_id="conn")),
    )


def _add_connector(feed: FeedBuilder, connector_id: str, **fields: object) -> None:
    """A second static connector of the project with one healthy proxy, ``<connector_id>-0``."""
    feed.add(
        Connector(
            id=connector_id, name=connector_id, credential_id="k",
            credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="p", **fields,  # type: ignore[arg-type]
        ),
        healthy(Proxy(id=f"{connector_id}-0", host=connector_id, port=1, connector_id=connector_id)),
    )


def _ids(router: Router, target_host: str | None = None) -> set[str]:
    return {proxy.id for proxy in router.get_routable_proxies_for_project("p", target_host=target_host)}


class TestLookups:
    def test_projects_by_id_and_username(self) -> None:
        router = _feed().router()
        project = router.get_project("p")
        assert project is not None and router.get_project_by_username("client") is project
        assert router.get_project_by_username("client-cc-us") is None

    def test_declared_countries(self) -> None:
        router = _feed(config={"countries": ["gb", "IE"]}).router()
        assert router.declared_countries("conn") == ["GB", "IE"]
        assert router.declared_countries("gone") == []


class TestExclusions:
    def test_a_retired_slot_is_gone_for_lookups_and_routing(self) -> None:
        exclusions = LocalExclusions()
        router = _feed().router(exclusions=exclusions)
        exclusions.retire("p1")
        assert router.get_proxy("p1") is None and router.get_proxy("p2") is not None
        assert _ids(router) == {"p2"}
        # The feed still has the row until the worker's delete reaches it.
        assert router.snapshot.proxy("p1") is not None

    async def test_a_local_trip_keeps_the_proxy_out_until_it_ends(self) -> None:
        exclusions = LocalExclusions()
        router = _feed().router(exclusions=exclusions)
        exclusions.quarantine("p1", utc_now() + timedelta(minutes=5))
        exclusions.quarantine("p2", utc_now() - timedelta(seconds=1))
        assert _ids(router) == {"p2"}
        # A quarantined proxy is still the proxy: lookups see it.
        assert router.get_proxy("p1") is not None

    async def test_what_a_gateway_excludes_applies_to_every_request_on_a_snapshot(self) -> None:
        """A snapshot works out once which rows can serve; retirements and trips after that still count."""
        exclusions = LocalExclusions()
        router = _feed().router(exclusions=exclusions)
        assert _ids(router) == {"p1", "p2"}
        exclusions.retire("p1")
        assert _ids(router) == {"p2"}
        exclusions.quarantine("p2", utc_now() + timedelta(seconds=5))
        assert _ids(router) == set()
        assert await router.are_all_proxies_quarantined("p") is True

    def test_a_quarantine_in_the_feed_keeps_the_proxy_out(self) -> None:
        feed = _feed()
        feed.quarantined["p1"] = utc_now() + timedelta(minutes=5)
        feed.quarantined["p2"] = utc_now() - timedelta(seconds=1)
        assert _ids(feed.router()) == {"p2"}

    async def test_exclude_drops_proxies_before_the_strategy(self) -> None:
        router = _feed("random").router()
        for _ in range(10):
            selected = await router.select_proxy_for_project("p", exclude=frozenset({"p1"}))
            assert selected is not None and selected.id == "p2"


class TestEligibility:
    @pytest.mark.parametrize("status", [s for s in ProxyStatus if s != ProxyStatus.HEALTHY])
    def test_only_healthy_rows_are_routed(self, status: ProxyStatus) -> None:
        feed = _feed()
        feed.proxies["p1"].status = status
        assert _ids(feed.router()) == {"p2"}

    def test_disabled_and_deleting_connectors_serve_nothing(self) -> None:
        feed = _feed()
        _add_connector(feed, "off", enabled=False)
        _add_connector(feed, "going", pending_deletion=True)
        assert _ids(feed.router()) == {"p1", "p2"}

    def test_a_traffic_blocked_connector_is_left_out(self) -> None:
        feed = _feed()
        _add_connector(feed, "over")
        feed.blocked_until["over"] = utc_now() + timedelta(days=1)
        router = feed.router()
        assert _ids(router) == {"p1", "p2"}
        assert router.is_traffic_blocked("over") and not router.is_traffic_blocked("conn")


class TestRefusalAnswers:
    async def test_every_serving_row_quarantined_is_a_429(self) -> None:
        exclusions = LocalExclusions()
        feed = _feed()
        feed.quarantined["p2"] = utc_now() + timedelta(minutes=5)
        router = feed.router(exclusions=exclusions)
        exclusions.quarantine("p1", utc_now() + timedelta(minutes=5))
        assert await router.select_proxy_for_project("p") is None
        assert await router.are_all_proxies_quarantined("p") is True

    async def test_a_retired_slot_is_gone_not_resting(self) -> None:
        exclusions = LocalExclusions()
        router = _feed().router(exclusions=exclusions)
        exclusions.retire("p1")
        exclusions.quarantine("p2", utc_now() + timedelta(minutes=5))
        assert await router.are_all_proxies_quarantined("p") is True
        exclusions.retire("p2")
        # Nothing left at all: a 502, not a 429.
        assert await router.are_all_proxies_quarantined("p") is False

    async def test_nothing_configured_is_no_quarantine_and_no_limit(self) -> None:
        router = _feed().router()
        assert await router.are_all_proxies_quarantined("other-project") is False
        assert router.traffic_limit_status("other-project") is None

    def test_a_blocked_connector_answers_with_its_limit_status(self) -> None:
        feed = _feed(traffic_config={"limit_bytes": 1000, "action": "block", "limit_status": 429})
        feed.blocked_until["conn"] = utc_now() + timedelta(days=1)
        assert feed.router().traffic_limit_status("p") == 429

    def test_blocked_connectors_disagreeing_answer_509(self) -> None:
        feed = _feed(traffic_config={"limit_bytes": 1000, "action": "block", "limit_status": 429})
        _add_connector(feed, "other", traffic_config={"limit_bytes": 1000, "action": "block"})
        for connector_id in ("conn", "other"):
            feed.blocked_until[connector_id] = utc_now() + timedelta(days=1)
        assert feed.router().traffic_limit_status("p") == 509

    def test_no_limit_status_while_something_still_serves(self) -> None:
        feed = _feed()
        _add_connector(feed, "over", traffic_config={"limit_bytes": 1000, "action": "block"})
        feed.blocked_until["over"] = utc_now() + timedelta(days=1)
        assert feed.router().traffic_limit_status("p") is None


class TestDomainFilter:
    def test_whitelist(self) -> None:
        router = _feed(routing_config={"domain_whitelist": ["example.com"]}).router()
        assert _ids(router, "example.com") == {"p1", "p2"}
        assert _ids(router, "www.example.com") == {"p1", "p2"}
        assert _ids(router, "other.com") == set()

    def test_blacklist(self) -> None:
        router = _feed(routing_config={"domain_blacklist": ["ads.example.com"]}).router()
        assert _ids(router, "example.com") == {"p1", "p2"}
        assert _ids(router, "ads.example.com") == set()
        assert _ids(router, "sub.ads.example.com") == set()

    def test_no_routing_config_allows_every_domain(self) -> None:
        assert _ids(_feed().router(), "anything.com") == {"p1", "p2"}

    def test_connectors_filter_independently(self) -> None:
        feed = _feed(routing_config={"domain_whitelist": ["google.com"]})
        _add_connector(feed, "open")
        router = feed.router()
        assert _ids(router, "google.com") == {"p1", "p2", "open-0"}
        assert _ids(router, "bing.com") == {"open-0"}

    async def test_select_respects_the_destination(self) -> None:
        router = _feed(routing_config={"domain_whitelist": ["allowed.com"]}).router()
        assert await router.select_proxy_for_project("p", target_host="allowed.com") is not None
        assert await router.select_proxy_for_project("p", target_host="blocked.com") is None


class TestStrategies:
    async def test_a_project_keeps_its_strategy_state_across_swaps(self) -> None:
        feed = _feed("round_robin")
        router = feed.router()
        strategy = router.strategy_for_project("p")
        first = await router.select_proxy_for_project("p")
        feed.add(healthy(Proxy(id="p3", host="h3", port=1, connector_id="conn")))
        router.swap(feed.snapshot(previous=router.snapshot))
        second = await router.select_proxy_for_project("p")
        assert router.strategy_for_project("p") is strategy
        # The cursor moved on instead of starting over at the first row.
        assert first is not None and second is not None and (first.id, second.id) == ("p1", "p2")

    def test_a_changed_routing_strategy_builds_a_new_one(self) -> None:
        feed = _feed("round_robin")
        router = feed.router()
        assert isinstance(router.strategy_for_project("p"), RoundRobinStrategy)
        feed.projects["p"].routing_strategy = "sticky"
        router.swap(feed.snapshot(previous=router.snapshot))
        assert isinstance(router.strategy_for_project("p"), StickySessionStrategy)

    async def test_a_removed_connector_drops_its_strategy_state(self) -> None:
        feed = _feed("round_robin")
        _add_connector(feed, "gone")
        router = feed.router()
        for _ in range(4):
            await router.select_proxy_for_project("p")
        strategy = router.strategy_for_project("p")
        assert isinstance(strategy, RoundRobinStrategy) and "gone" in strategy._current
        del feed.connectors["gone"]
        router.swap(feed.snapshot(previous=router.snapshot))
        assert "gone" not in strategy._current and "gone" not in strategy._indices
        assert "conn" in strategy._indices

    async def test_a_removed_project_drops_its_strategy(self) -> None:
        feed = _feed("sticky")
        router = feed.router()
        await router.select_proxy_for_project("p", session_id="s")
        sticky = router.strategy_for_project("p")
        del feed.projects["p"]
        router.swap(feed.snapshot(previous=router.snapshot))
        feed.add(Project(id="p", name="P", username="client", password="pw", routing_strategy="sticky"))
        router.swap(feed.snapshot(previous=router.snapshot))
        assert router.strategy_for_project("p") is not sticky

    def test_a_strategy_no_router_has_is_not_routed_with_the_gateways_default(self) -> None:
        """The compiler serves a stored name no router has as the install's default, so a feed names none."""
        router = _feed("fastest").router(default_strategy="random")
        with pytest.raises(ValueError, match="Unknown strategy: fastest"):
            router.strategy_for_project("p")

    def test_an_unknown_project_gets_the_default(self) -> None:
        assert _feed().router(default_strategy="sticky").strategy_for_project("gone").name == "sticky"

    async def test_least_used_routes_on_this_gateways_open_transfers(self) -> None:
        feed = _feed("least_used")
        feed.add(healthy(Proxy(id="p3", host="h3", port=1, connector_id="conn")))
        inflight = InFlight()
        router = feed.router(inflight=inflight)
        inflight.begin("p1", "conn")
        inflight.begin("p2", "conn")
        for _ in range(10):
            selected = await router.select_proxy_for_project("p")
            assert selected is not None and selected.id == "p3"

    async def test_health_based_routes_on_this_gateways_outcomes(self) -> None:
        outcomes = LocalOutcomes()
        for request in range(100):
            outcomes.record("p1", True, 50.0)
            outcomes.record("p2", request < 10, 900.0)
        router = _feed("health_based").router(outcomes=outcomes)
        picks = set()
        for _ in range(30):
            selected = await router.select_proxy_for_project("p")
            assert selected is not None
            picks.add(selected.id)
        assert picks == {"p1"}

    async def test_settle_waits_for_every_strategy_it_holds_and_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each strategy says what it has to wait for; the router asks them all, whatever their kind."""
        router = _feed("round_robin").router(default_strategy="random")
        held, default = router.strategy_for_project("p"), router.strategy_for_project("unknown")
        settled: list[str] = []
        for strategy in (held, default):

            async def settle(name: str = strategy.name) -> None:
                settled.append(name)

            monkeypatch.setattr(strategy, "settle", settle)

        await router.settle()

        assert settled == ["round_robin", "random"]


class BrokenRedis(RedisClient):
    """A Redis that cannot be reached: every sticky binding call fails."""

    def __init__(self) -> None:
        # Never connected: only the two calls below are made.
        super().__init__("redis://redis.invalid")

    async def get_sticky_binding(self, project_id: str, session_id: str) -> str | None:
        raise ConnectionError("Redis is down")

    async def set_sticky_binding(self, project_id: str, session_id: str, proxy_id: str, ttl_seconds: int = 300) -> None:
        raise ConnectionError("Redis is down")


class SlowRedis(RedisClient):
    """Sticky bindings in memory, with every read counted and every write held until ``written`` is set."""

    def __init__(self) -> None:
        # Never connected: only the two calls below are made.
        super().__init__("redis://redis.invalid")
        self.bindings: dict[tuple[str, str], str] = {}
        self.reads = 0
        self.written = asyncio.Event()

    async def get_sticky_binding(self, project_id: str, session_id: str) -> str | None:
        self.reads += 1
        return self.bindings.get((project_id, session_id))

    async def set_sticky_binding(self, project_id: str, session_id: str, proxy_id: str, ttl_seconds: int = 300) -> None:
        await self.written.wait()
        self.bindings[(project_id, session_id)] = proxy_id


class TestStickyQuarantine:
    async def test_a_session_is_held_to_its_quarantined_proxy(self, redis_client: RedisClient) -> None:
        exclusions = LocalExclusions()
        router = _feed("sticky", rate_limit_config=STICKY_RATE_LIMIT).router(
            redis_client=redis_client, exclusions=exclusions
        )
        first = await router.select_proxy_for_project("p", session_id="session-1")
        assert first is not None
        exclusions.quarantine(first.id, utc_now() + timedelta(minutes=5))

        # sticky_quarantine: the same session gets nothing (429) rather than another exit.
        assert await router.select_proxy_for_project("p", session_id="session-1") is None
        assert await router.are_all_proxies_quarantined("p", session_id="session-1") is True
        # Another session still gets the proxy that is not quarantined.
        other = await router.select_proxy_for_project("p", session_id="session-2")
        assert other is not None and other.id != first.id

        # A gateway that knows the binding only through Redis (another gateway
        # bound it, or this one dropped its local copy) holds the session too.
        await router.settle()
        router.strategy_for_project("p").reset()
        assert await router.select_proxy_for_project("p", session_id="session-1") is None
        assert await router.are_all_proxies_quarantined("p", session_id="session-1") is True
        # The Redis hit was remembered locally.
        strategy = router.strategy_for_project("p")
        assert isinstance(strategy, StickySessionStrategy) and strategy.bound_proxy_id("session-1") == first.id

    async def test_a_quarantine_from_the_feed_holds_the_session_too(self, redis_client: RedisClient) -> None:
        feed = _feed("sticky", rate_limit_config=STICKY_RATE_LIMIT)
        router = feed.router(redis_client=redis_client)
        first = await router.select_proxy_for_project("p", session_id="session-1")
        assert first is not None
        feed.quarantined[first.id] = utc_now() + timedelta(minutes=5)
        router.swap(feed.snapshot(previous=router.snapshot))
        assert await router.select_proxy_for_project("p", session_id="session-1") is None

    async def test_without_sticky_quarantine_the_session_moves(self, redis_client: RedisClient) -> None:
        exclusions = LocalExclusions()
        rate_limit = {**STICKY_RATE_LIMIT, "sticky_quarantine": False}
        router = _feed("sticky", rate_limit_config=rate_limit).router(redis_client=redis_client, exclusions=exclusions)
        first = await router.select_proxy_for_project("p", session_id="sess-1")
        assert first is not None
        exclusions.quarantine(first.id, utc_now() + timedelta(minutes=5))
        moved = await router.select_proxy_for_project("p", session_id="sess-1")
        assert moved is not None and moved.id != first.id
        assert await router.are_all_proxies_quarantined("p", session_id="sess-1") is False

    async def test_a_strategy_that_holds_no_session_moves_it(self, redis_client: RedisClient) -> None:
        """``sticky_quarantine`` keeps only a session the project's strategy holds: under round robin it moves."""
        exclusions = LocalExclusions()
        router = _feed("round_robin", rate_limit_config=STICKY_RATE_LIMIT).router(
            redis_client=redis_client, exclusions=exclusions
        )
        first = await router.select_proxy_for_project("p", session_id="session-1")
        assert first is not None
        exclusions.quarantine(first.id, utc_now() + timedelta(minutes=5))
        moved = await router.select_proxy_for_project("p", session_id="session-1")
        assert moved is not None and moved.id != first.id
        assert await router.are_all_proxies_quarantined("p", session_id="session-1") is False

    async def test_a_retired_slot_holds_no_session(self, redis_client: RedisClient) -> None:
        exclusions = LocalExclusions()
        router = _feed("sticky", rate_limit_config=STICKY_RATE_LIMIT).router(
            redis_client=redis_client, exclusions=exclusions
        )
        first = await router.select_proxy_for_project("p", session_id="session-1")
        assert first is not None
        exclusions.quarantine(first.id, utc_now() + timedelta(minutes=5))
        exclusions.retire(first.id)
        moved = await router.select_proxy_for_project("p", session_id="session-1")
        assert moved is not None and moved.id != first.id

    async def test_redis_failing_never_fails_a_request(self) -> None:
        router = _feed("sticky", rate_limit_config=STICKY_RATE_LIMIT).router(
            redis_client=BrokenRedis()
        )
        first = await router.select_proxy_for_project("p", session_id="session-1")
        again = await router.select_proxy_for_project("p", session_id="session-1")
        # Bound locally and placed by hashing, as a new session would be.
        assert first is not None and again is not None and first.id == again.id
        assert await router.are_all_proxies_quarantined("p", session_id="session-9") is False


class TestStickyRedisCalls:
    """A request waits on Redis for the one binding read it needs, never for the write."""

    @pytest.mark.parametrize("sticky_quarantine", [True, False])
    async def test_a_session_bound_elsewhere_is_read_once(self, sticky_quarantine: bool) -> None:
        redis = SlowRedis()
        redis.bindings[("p", "session-1")] = "p2"
        rate_limit = {**STICKY_RATE_LIMIT, "sticky_quarantine": sticky_quarantine}
        router = _feed("sticky", rate_limit_config=rate_limit).router(redis_client=redis)

        selected = await router.select_proxy_for_project("p", session_id="session-1")

        assert selected is not None and selected.id == "p2"
        assert redis.reads == 1

    async def test_a_project_holding_no_session_reads_a_new_one_once(self) -> None:
        redis = SlowRedis()
        rate_limit = {**STICKY_RATE_LIMIT, "sticky_quarantine": False}
        router = _feed("sticky", rate_limit_config=rate_limit).router(redis_client=redis)
        assert await router.select_proxy_for_project("p", session_id="session-1") is not None
        assert redis.reads == 1

    async def test_a_new_binding_is_written_without_holding_the_request(self) -> None:
        redis = SlowRedis()
        router = _feed("sticky", rate_limit_config=STICKY_RATE_LIMIT).router(redis_client=redis)

        selected = await asyncio.wait_for(router.select_proxy_for_project("p", session_id="session-1"), 1.0)

        assert selected is not None and redis.bindings == {}
        # The write lands once Redis takes it.
        redis.written.set()
        await router.settle()
        assert redis.bindings == {("p", "session-1"): selected.id}
