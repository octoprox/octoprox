# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for routing strategies.

``least_used`` and ``health_based`` route on what one gateway measures: the
transfers it has in flight (:class:`InFlight`) and the outcomes of the
requests it finished (:class:`LocalOutcomes`, recorded through it here).
"""

import random
from collections import Counter
from typing import cast
from unittest.mock import AsyncMock, call, patch

import pytest

from api.db.redis import RedisClient
from api.models.proxy import Proxy, ProxyStatus
from api.routing.inflight import InFlight
from api.routing.outcomes import LocalOutcomes
from api.strategies import STRATEGY_NAMES, get_strategy
from api.strategies.base import ProxyGroup
from api.strategies.health_based import HEALTH_MIN_SAMPLES, HealthBasedStrategy
from api.strategies.least_used import LeastUsedStrategy
from api.strategies.random import RandomStrategy
from api.strategies.round_robin import RoundRobinStrategy
from api.strategies.sticky import StickySessionStrategy


@pytest.fixture
def sample_proxies() -> list[Proxy]:
    """Create a list of sample proxies for testing."""
    return [
        Proxy(id="proxy-1", host="proxy1.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.HEALTHY),
        Proxy(id="proxy-2", host="proxy2.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.HEALTHY),
        Proxy(id="proxy-3", host="proxy3.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.DEGRADED),
    ]


class TestRoundRobinStrategy:
    """Tests for RoundRobinStrategy."""

    def test_name(self):
        strategy = RoundRobinStrategy()
        assert strategy.name == "round_robin"

    async def test_select_empty_list(self):
        strategy = RoundRobinStrategy()
        result = await strategy.select([])
        assert result is None

    async def test_select_cycles_through_proxies(self, sample_proxies: list[Proxy]):
        strategy = RoundRobinStrategy()

        # First cycle
        assert (await strategy.select(sample_proxies)).id == "proxy-1"
        assert (await strategy.select(sample_proxies)).id == "proxy-2"
        assert (await strategy.select(sample_proxies)).id == "proxy-3"

        # Second cycle - should wrap around
        assert (await strategy.select(sample_proxies)).id == "proxy-1"

    async def test_reset(self, sample_proxies: list[Proxy]):
        strategy = RoundRobinStrategy()

        await strategy.select(sample_proxies)  # Move to index 1
        strategy.reset()

        # Should start from beginning again
        assert (await strategy.select(sample_proxies)).id == "proxy-1"


class TestLeastUsedStrategy:
    def test_name(self) -> None:
        assert LeastUsedStrategy().name == "least_used"

    async def test_select_empty_list(self) -> None:
        assert await LeastUsedStrategy().select([]) is None

    async def test_select_returns_the_proxy_with_the_fewest_open_transfers(self, sample_proxies: list[Proxy]) -> None:
        inflight = InFlight()
        strategy = LeastUsedStrategy(inflight)
        for proxy_id, open_transfers in (("proxy-1", 2), ("proxy-2", 1), ("proxy-3", 3)):
            for _ in range(open_transfers):
                inflight.begin(proxy_id, "conn-1")
        for _ in range(10):
            selected = await strategy.select(sample_proxies)
            assert selected is not None and selected.id == "proxy-2"

    async def test_a_finished_transfer_frees_its_proxy(self, sample_proxies: list[Proxy]) -> None:
        inflight = InFlight()
        strategy = LeastUsedStrategy(inflight)
        for proxy in sample_proxies:
            inflight.begin(proxy.id, "conn-1")
        inflight.end("proxy-3", "conn-1")
        selected = await strategy.select(sample_proxies)
        assert selected is not None and selected.id == "proxy-3"

    async def test_ties_are_broken_at_random(self, sample_proxies: list[Proxy]) -> None:
        """Sequential traffic has nothing in flight between requests; a fixed tie-break would pin one proxy."""
        random.seed(5)
        strategy = LeastUsedStrategy(InFlight())
        picks = Counter([(await strategy.select(sample_proxies)).id for _ in range(300)])
        assert set(picks) == {"proxy-1", "proxy-2", "proxy-3"}
        assert min(picks.values()) > 60

    async def test_lifetime_counters_play_no_part(self, sample_proxies: list[Proxy]) -> None:
        """``request_count`` moved only when a flush landed, so it herded every request onto one proxy."""
        inflight = InFlight()
        inflight.begin("proxy-2", "conn-1")
        strategy = LeastUsedStrategy(inflight)
        picks = {(await strategy.select(sample_proxies)).id for _ in range(50)}
        assert picks == {"proxy-1", "proxy-3"}


class TestRandomStrategy:
    """Tests for RandomStrategy."""

    def test_name(self):
        strategy = RandomStrategy()
        assert strategy.name == "random"

    async def test_select_empty_list(self):
        strategy = RandomStrategy()
        result = await strategy.select([])
        assert result is None

    async def test_select_returns_valid_proxy(self, sample_proxies: list[Proxy]):
        strategy = RandomStrategy()

        for _ in range(10):
            result = await strategy.select(sample_proxies)
            assert result is not None
            assert result in sample_proxies


class TestStickySessionStrategy:
    """Tests for StickySessionStrategy, most of them in-process: with no ``redis_client`` it routes from ``_session_map``.

    The Redis binding is an ``AsyncMock`` here; the router's tests read a real one (tests/routing/test_router.py).
    """

    def test_name(self):
        strategy = StickySessionStrategy()
        assert strategy.name == "sticky"

    async def test_select_empty_list(self):
        strategy = StickySessionStrategy()
        result = await strategy.select([])
        assert result is None

    async def test_select_without_session_returns_random(self, sample_proxies: list[Proxy]):
        strategy = StickySessionStrategy()

        result = await strategy.select(sample_proxies, session_id=None)
        assert result is not None
        assert result in sample_proxies

    async def test_select_same_session_returns_same_proxy(self, sample_proxies: list[Proxy]):
        strategy = StickySessionStrategy()
        session_id = "user-session-123"

        first_result = await strategy.select(sample_proxies, session_id=session_id)

        # Same session should return same proxy
        for _ in range(5):
            result = await strategy.select(sample_proxies, session_id=session_id)
            assert result.id == first_result.id

    async def test_select_different_sessions_can_differ(self, sample_proxies: list[Proxy]):
        strategy = StickySessionStrategy()

        result1 = await strategy.select(sample_proxies, session_id="session-1")
        result2 = await strategy.select(sample_proxies, session_id="session-2")

        # Both should be valid proxies (may or may not be the same)
        assert result1 in sample_proxies
        assert result2 in sample_proxies

    async def test_binding_expires_after_ttl_without_requests(self, sample_proxies: list[Proxy]):
        now = [1000.0]
        strategy = StickySessionStrategy(ttl_seconds=300, clock=lambda: now[0])
        first = await strategy.select(sample_proxies, session_id="s")
        assert strategy.bound_proxy_id("s") == first.id
        now[0] += 299
        assert strategy.bound_proxy_id("s") == first.id
        now[0] += 2
        assert strategy.bound_proxy_id("s") is None
        assert "s" not in strategy._session_map

    async def test_requests_keep_a_binding_alive(self, sample_proxies: list[Proxy]):
        now = [0.0]
        strategy = StickySessionStrategy(ttl_seconds=300, clock=lambda: now[0])
        first = await strategy.select(sample_proxies, session_id="s")
        for _ in range(5):
            now[0] += 200
            assert (await strategy.select(sample_proxies, session_id="s")).id == first.id
        # Ten minutes have passed; each request pushed the expiry out.
        assert strategy.bound_proxy_id("s") == first.id

    async def test_abandoned_sessions_are_swept(self, sample_proxies: list[Proxy]):
        from api.strategies import sticky as sticky_module

        now = [0.0]
        strategy = StickySessionStrategy(ttl_seconds=10, clock=lambda: now[0])
        for i in range(sticky_module._SWEEP_EVERY - 1):
            await strategy.select(sample_proxies, session_id=f"old-{i}")
        now[0] += 11
        assert len(strategy._session_map) == sticky_module._SWEEP_EVERY - 1
        await strategy.select(sample_proxies, session_id="new")
        assert set(strategy._session_map) == {"new"}

    async def test_entries_warmed_from_redis_count_toward_the_sweep(self, sample_proxies: list[Proxy]):
        from unittest.mock import AsyncMock

        from api.strategies import sticky as sticky_module

        now = [0.0]
        strategy = StickySessionStrategy(ttl_seconds=10, clock=lambda: now[0])
        redis = AsyncMock(spec=RedisClient)
        redis.get_sticky_binding.return_value = sample_proxies[0].id
        # This instance never binds anything itself: every session arrives already bound by a peer.
        for i in range(sticky_module._SWEEP_EVERY - 1):
            await strategy.select(sample_proxies, session_id=f"old-{i}", redis_client=redis, project_id="p")
        now[0] += 11
        await strategy.select(sample_proxies, session_id="new", redis_client=redis, project_id="p")
        assert set(strategy._session_map) == {"new"}

    async def test_active_session_refreshes_its_redis_binding_every_half_ttl(self, sample_proxies: list[Proxy]):
        from unittest.mock import AsyncMock

        now = [0.0]
        strategy = StickySessionStrategy(ttl_seconds=300, clock=lambda: now[0])
        redis = AsyncMock(spec=RedisClient)
        redis.get_sticky_binding.return_value = None
        first = await strategy.select(sample_proxies, session_id="s", redis_client=redis, project_id="p")
        await strategy.settle()
        assert redis.set_sticky_binding.await_count == 1
        for _ in range(10):
            now[0] += 10
            await strategy.select(sample_proxies, session_id="s", redis_client=redis, project_id="p")
        # 100 seconds in: still within the first half TTL, nothing rewritten.
        await strategy.settle()
        assert redis.set_sticky_binding.await_count == 1
        now[0] += 60
        await strategy.select(sample_proxies, session_id="s", redis_client=redis, project_id="p")
        await strategy.settle()
        assert redis.set_sticky_binding.await_count == 2
        redis.set_sticky_binding.assert_awaited_with("p", "s", first.id, ttl_seconds=300)

    async def test_reset_clears_session_map(self, sample_proxies: list[Proxy]):
        strategy = StickySessionStrategy()
        session_id = "user-session-123"

        first_result = await strategy.select(sample_proxies, session_id=session_id)
        strategy.reset()

        # After reset, session map is cleared but consistent hashing should give same result
        second_result = await strategy.select(sample_proxies, session_id=session_id)
        assert second_result.id == first_result.id  # Consistent hashing

    async def test_sessid_produces_consistent_selection(self, sample_proxies: list[Proxy]):
        """Test that a session ID (as extracted from username) produces consistent results."""
        strategy = StickySessionStrategy()
        sessid = "abc123"

        first_result = await strategy.select(sample_proxies, session_id=sessid)
        assert first_result is not None

        for _ in range(5):
            result = await strategy.select(sample_proxies, session_id=sessid)
            assert result.id == first_result.id

    async def test_different_sessids_can_map_to_different_proxies(self, sample_proxies: list[Proxy]):
        """Test that different session IDs can map to different proxies."""
        strategy = StickySessionStrategy()

        results = {}
        for i in range(20):
            sessid = f"session-{i}"
            result = await strategy.select(sample_proxies, session_id=sessid)
            results[sessid] = result.id

        # With 20 different session IDs and 3 proxies, we should see more than 1 proxy used
        unique_proxies = set(results.values())
        assert len(unique_proxies) > 1

    async def test_cached_proxy_survives_list_reorder(self, sample_proxies: list[Proxy]):
        """Test that a cached session binding is stable even if the proxy list order changes."""
        strategy = StickySessionStrategy()
        sessid = "stable-session"

        first_result = await strategy.select(sample_proxies, session_id=sessid)

        # Reverse the proxy list order
        reordered = list(reversed(sample_proxies))
        result = await strategy.select(reordered, session_id=sessid)
        assert result.id == first_result.id

    async def test_removed_proxy_triggers_reassignment(self):
        """Test that removing the bound proxy causes reassignment to another proxy."""
        strategy = StickySessionStrategy()
        proxies = [
            Proxy(id="proxy-a", host="a.example.com", port=8080, connector_id="conn-1"),
            Proxy(id="proxy-b", host="b.example.com", port=8080, connector_id="conn-1"),
        ]
        sessid = "sticky-session"

        first_result = await strategy.select(proxies, session_id=sessid)

        # Remove the selected proxy from the list (simulating it becoming unhealthy)
        remaining = [p for p in proxies if p.id != first_result.id]
        result = await strategy.select(remaining, session_id=sessid)

        # Should get the other proxy
        assert result is not None
        assert result.id != first_result.id
        assert result.id in [p.id for p in remaining]

    async def test_an_unreadable_binding_places_the_session_by_hashing(self, sample_proxies: list[Proxy]) -> None:
        redis = AsyncMock(spec=RedisClient)
        redis.get_sticky_binding.side_effect = ConnectionError("Redis is down")
        strategy = StickySessionStrategy()
        selected = await strategy.select(sample_proxies, session_id="s", redis_client=redis, project_id="p")
        assert selected is not None and selected.id == StickySessionStrategy._consistent_hash_pick(sample_proxies, "s").id

    async def test_an_unwritable_binding_stays_local(self, sample_proxies: list[Proxy]) -> None:
        now = [0.0]
        redis = AsyncMock(spec=RedisClient)
        redis.get_sticky_binding.return_value = None
        redis.set_sticky_binding.side_effect = ConnectionError("Redis is down")
        strategy = StickySessionStrategy(ttl_seconds=300, clock=lambda: now[0])
        first = await strategy.select(sample_proxies, session_id="s", redis_client=redis, project_id="p")
        now[0] += 10
        again = await strategy.select(sample_proxies, session_id="s", redis_client=redis, project_id="p")
        assert first is not None and again is not None and first.id == again.id
        # The failed write is not retried on every request: the next try is half a TTL later.
        await strategy.settle()
        assert redis.set_sticky_binding.await_count == 1

    async def test_stored_binding_reads_redis_and_fails_open(self) -> None:
        redis = AsyncMock(spec=RedisClient)
        redis.get_sticky_binding.return_value = "proxy-2"
        strategy = StickySessionStrategy()
        assert await strategy.stored_binding(cast(RedisClient, redis), "p", "s") == "proxy-2"
        redis.get_sticky_binding.side_effect = ConnectionError("Redis is down")
        assert await strategy.stored_binding(cast(RedisClient, redis), "p", "s") is None

    async def test_a_binding_cut_by_the_deadline_is_logged_by_its_type(self, sample_proxies: list[Proxy]) -> None:
        """A Redis call cut by the request deadline raises a bare TimeoutError; the read's and the write's logs name it."""
        redis = AsyncMock(spec=RedisClient)
        redis.get_sticky_binding.side_effect = TimeoutError
        redis.set_sticky_binding.side_effect = TimeoutError
        strategy = StickySessionStrategy()
        with patch("api.strategies.sticky.logger") as log:
            selected = await strategy.select(sample_proxies, session_id="s", redis_client=cast(RedisClient, redis), project_id="p")
            await strategy.settle()
        assert selected is not None
        assert log.debug.call_args_list == [
            call("Sticky binding could not be read", project_id="p", error="TimeoutError"),
            call("Sticky binding could not be stored", project_id="p", error="TimeoutError"),
        ]


class TestHeldProxy:
    """The proxy a strategy holds a session to, which the router keeps the session on while it rests in quarantine."""

    async def test_a_strategy_that_binds_no_session_holds_none(self, sample_proxies: list[Proxy]) -> None:
        redis = AsyncMock(spec=RedisClient)
        for strategy in (RandomStrategy(), RoundRobinStrategy(), LeastUsedStrategy(), HealthBasedStrategy()):
            assert await strategy.select(sample_proxies, session_id="s") is not None
            assert await strategy.held_proxy("s", redis_client=cast(RedisClient, redis), project_id="p") is None
        redis.get_sticky_binding.assert_not_called()

    async def test_sticky_holds_its_own_binding_without_asking_redis(self, sample_proxies: list[Proxy]) -> None:
        redis = AsyncMock(spec=RedisClient)
        strategy = StickySessionStrategy()
        first = await strategy.select(sample_proxies, session_id="s")
        assert first is not None
        assert await strategy.held_proxy("s", redis_client=cast(RedisClient, redis), project_id="p") == first.id
        redis.get_sticky_binding.assert_not_called()

    async def test_sticky_reads_a_binding_made_elsewhere_once_and_remembers_it(self) -> None:
        redis = AsyncMock(spec=RedisClient)
        redis.get_sticky_binding.return_value = "proxy-2"
        strategy = StickySessionStrategy()
        for _ in range(2):
            assert await strategy.held_proxy("s", redis_client=cast(RedisClient, redis), project_id="p") == "proxy-2"
        assert redis.get_sticky_binding.await_count == 1
        assert strategy.bound_proxy_id("s") == "proxy-2"

    async def test_sticky_holds_none_for_a_session_bound_nowhere_or_unreadable(self) -> None:
        redis = AsyncMock(spec=RedisClient)
        redis.get_sticky_binding.return_value = None
        strategy = StickySessionStrategy()
        assert await strategy.held_proxy("s", redis_client=cast(RedisClient, redis), project_id="p") is None
        redis.get_sticky_binding.side_effect = ConnectionError("Redis is down")
        assert await strategy.held_proxy("s", redis_client=cast(RedisClient, redis), project_id="p") is None
        # Without Redis only a binding made here counts.
        assert await strategy.held_proxy("s") is None
        assert strategy.bound_proxy_id("s") is None


def _measured(**outcomes: tuple[int, int, float]) -> LocalOutcomes:
    """A gateway's outcomes with each proxy's ``(requests, successes, latency sum in ms)`` recorded, latency spread evenly.

    Keyword names spell proxy ids with ``_`` for ``-``: ``high_success`` is ``high-success``.
    """
    measured = LocalOutcomes()
    for name, (requests, successes, latency_sum_ms) in outcomes.items():
        for index in range(requests):
            measured.record(name.replace("_", "-"), index < successes, latency_sum_ms / requests)
    return measured


def _rows(*ids: str) -> list[Proxy]:
    return [Proxy(id=i, host=f"{i}.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.HEALTHY) for i in ids]


class TestHealthBasedStrategy:
    def test_name(self) -> None:
        assert HealthBasedStrategy().name == "health_based"

    async def test_select_empty_list(self) -> None:
        assert await HealthBasedStrategy().select([]) is None

    async def test_select_considers_success_rate(self) -> None:
        outcomes = _measured(high_success=(100, 95, 10000.0), low_success=(100, 50, 10000.0))
        strategy = HealthBasedStrategy(outcomes)
        results = [await strategy.select(_rows("high-success", "low-success")) for _ in range(20)]
        assert {r.id for r in results if r is not None} == {"high-success"}

    async def test_select_considers_latency(self) -> None:
        outcomes = _measured(fast=(100, 90, 5000.0), slow=(100, 90, 50000.0))
        strategy = HealthBasedStrategy(outcomes)
        results = [await strategy.select(_rows("fast", "slow")) for _ in range(20)]
        assert {r.id for r in results if r is not None} == {"fast"}

    async def test_draws_from_the_best_third(self) -> None:
        random.seed(2)
        outcomes = _measured(**{f"p{i}": (100, 100 - i * 10, 5000.0) for i in range(6)})
        strategy = HealthBasedStrategy(outcomes)
        picks = {(await strategy.select(_rows(*(f"p{i}" for i in range(6))))).id for _ in range(100)}
        assert picks == {"p0", "p1"}

    async def test_unmeasured_rows_are_always_candidates(self) -> None:
        """A restarted gateway or a new slot has no outcomes yet; without this the first scored rows took everything."""
        random.seed(4)
        outcomes = _measured(good=(100, 100, 5000.0), bad=(100, 10, 90000.0), fresh=(HEALTH_MIN_SAMPLES - 1, 0, 0.0))
        strategy = HealthBasedStrategy(outcomes)
        picks = Counter([(await strategy.select(_rows("good", "bad", "fresh", "new"))).id for _ in range(400)])
        assert set(picks) == {"good", "fresh", "new"}

    async def test_a_row_with_enough_samples_is_scored(self) -> None:
        outcomes = _measured(good=(100, 100, 5000.0), bad=(HEALTH_MIN_SAMPLES, 0, 0.0))
        strategy = HealthBasedStrategy(outcomes)
        picks = {(await strategy.select(_rows("good", "bad"))).id for _ in range(30)}
        assert picks == {"good"}

    async def test_a_cold_gateway_spreads_over_every_row(self) -> None:
        random.seed(6)
        strategy = HealthBasedStrategy()
        picks = Counter([(await strategy.select(_rows("a", "b", "c"))).id for _ in range(300)])
        assert set(picks) == {"a", "b", "c"}

    def test_score(self) -> None:
        # High success rate, low latency = high score; an unmeasured latency is neutral.
        assert HealthBasedStrategy.score(100, 95, 5000.0) > HealthBasedStrategy.score(100, 30, 80000.0)
        assert HealthBasedStrategy.score(100, 100, 0.0) == pytest.approx(100 * 0.6 + 50 * 0.4)
        assert HealthBasedStrategy.score(0, 0, 0.0) == pytest.approx(50 * 0.4)


class TestGetStrategy:
    def test_every_name_builds_its_strategy(self) -> None:
        assert {get_strategy(name).name for name in STRATEGY_NAMES} == set(STRATEGY_NAMES)
        assert get_strategy("Sticky").name == "sticky"

    def test_an_unknown_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Unknown strategy"):
            get_strategy("fastest")

    async def test_counters_reach_the_strategies_that_route_on_them(self) -> None:
        """Twenty draws each: without its counters a strategy would pick either row at random."""
        inflight = InFlight()
        inflight.begin("a", "conn-1")
        least_used = get_strategy("least_used", inflight=inflight)
        assert {(await least_used.select(_rows("a", "b"))).id for _ in range(20)} == {"b"}
        health_based = get_strategy("health_based", outcomes=_measured(a=(100, 100, 1.0), b=(100, 0, 1.0)))
        assert {(await health_based.select(_rows("a", "b"))).id for _ in range(20)} == {"a"}

    async def test_built_without_counters_a_strategy_sees_nothing_in_flight_and_no_outcomes(self) -> None:
        """Validation and the api's previews build strategies no gateway serves through: every row is idle and unmeasured."""
        random.seed(8)
        rows = _rows("a", "b", "c")
        for name in ("least_used", "health_based"):
            strategy = get_strategy(name)
            assert {(await strategy.select(rows)).id for _ in range(100)} == {"a", "b", "c"}, name

    async def test_every_strategy_can_be_settled(self) -> None:
        """The router settles every strategy it holds as the gateway stops; only sticky has writes to wait for."""
        for name in STRATEGY_NAMES:
            assert await get_strategy(name).settle() is None


def _group(key: str, weight: int, count: int) -> ProxyGroup:
    return ProxyGroup(
        key=key,
        weight=weight,
        proxies=[
            Proxy(id=f"{key}-{i}", host=f"{key}{i}", port=1, connector_id=key, status=ProxyStatus.HEALTHY)
            for i in range(count)
        ],
    )


def _open(inflight: InFlight, group: ProxyGroup, per_proxy: int) -> None:
    """``per_proxy`` transfers open through every proxy of the group."""
    for proxy in group.proxies:
        for _ in range(per_proxy):
            inflight.begin(proxy.id, group.key)


class TestWeightedSelection:
    """Two-step selection: the connector by weight, then the proxy inside it."""

    async def test_empty_groups_return_none(self):
        for strategy in (RandomStrategy(), RoundRobinStrategy(), LeastUsedStrategy(), StickySessionStrategy(), HealthBasedStrategy()):
            assert await strategy.select_weighted([]) is None
            assert await strategy.select_weighted([ProxyGroup(key="a", weight=5)]) is None

    async def test_single_group_behaves_like_flat_select(self):
        strategy = RoundRobinStrategy()
        group = _group("a", 7, 3)
        picks = [(await strategy.select_weighted([group])).id for _ in range(4)]
        assert picks == ["a-0", "a-1", "a-2", "a-0"]

    async def test_groups_without_proxies_take_no_share(self):
        strategy = RandomStrategy()
        groups = [_group("a", 1, 0), _group("b", 1, 2)]
        for _ in range(20):
            assert (await strategy.select_weighted(groups)).connector_id == "b"

    async def test_random_follows_weights(self):
        random.seed(7)
        strategy = RandomStrategy()
        groups = [_group("a", 1, 10), _group("b", 3, 1)]
        counts = Counter([(await strategy.select_weighted(groups)).connector_id for _ in range(4000)])
        assert 0.70 < counts["b"] / 4000 < 0.80

    async def test_round_robin_interleaves_by_weight(self):
        strategy = RoundRobinStrategy()
        groups = [_group("a", 3, 2), _group("b", 1, 1)]
        picks = [(await strategy.select_weighted(groups)).id for _ in range(8)]
        assert [p.split("-")[0] for p in picks].count("a") == 6
        assert [p.split("-")[0] for p in picks][:4].count("b") == 1
        # Inside "a" the two proxies alternate regardless of the "b" picks in between.
        a_picks = [p for p in picks if p.startswith("a")]
        assert a_picks == ["a-0", "a-1", "a-0", "a-1", "a-0", "a-1"]

    async def test_round_robin_keeps_credit_of_a_briefly_absent_group(self):
        strategy = RoundRobinStrategy()
        a, b = _group("a", 3, 1), _group("b", 1, 1)
        # Smooth WRR at 3:1 runs A A B A: two picks in, B is owed the third.
        for _ in range(2):
            assert (await strategy.select_weighted([a, b])).connector_id == "a"
        # B drops out for one call (quarantined); its credit is not forgotten.
        assert (await strategy.select_weighted([a])).connector_id == "a"
        assert (await strategy.select_weighted([a, b])).connector_id == "b"

    async def test_round_robin_forget_group_drops_state(self):
        strategy = RoundRobinStrategy()
        await strategy.select_weighted([_group("a", 1, 2), _group("b", 1, 1)])
        strategy.forget_group("a")
        assert "a" not in strategy._current
        assert "a" not in strategy._indices
        assert "b" in strategy._current

    async def test_least_used_treats_weight_as_capacity(self) -> None:
        inflight = InFlight()
        strategy = LeastUsedStrategy(inflight)
        # "a" has 9 transfers open at weight 3 (3 per unit); "b" 4 at weight 1 (4 per unit).
        a, b = _group("a", 3, 3), _group("b", 1, 1)
        _open(inflight, a, 3)
        _open(inflight, b, 4)
        assert (await strategy.select_weighted([a, b])).connector_id == "a"
        # Two more each through "a": 15 at weight 3 is 5 per unit.
        _open(inflight, a, 2)
        assert (await strategy.select_weighted([a, b])).connector_id == "b"

    async def test_least_used_counts_the_whole_connector_not_the_eligible_slice(self) -> None:
        inflight = InFlight()
        strategy = LeastUsedStrategy(inflight)
        # "a" is a busy connector of which one idle row serves this request; "b" is one idle row.
        busy, idle = _group("a", 1, 1), _group("b", 1, 1)
        for _ in range(50):
            inflight.begin("a-other", "a")
        assert (await strategy.select_weighted([busy, idle])).connector_id == "b"

    async def test_least_used_splits_idle_connectors_by_weight(self) -> None:
        random.seed(9)
        strategy = LeastUsedStrategy(InFlight())
        groups = [_group("a", 1, 1), _group("b", 3, 1)]
        counts = Counter([(await strategy.select_weighted(groups)).connector_id for _ in range(4000)])
        assert 0.70 < counts["b"] / 4000 < 0.80

    async def test_sticky_same_session_same_connector(self):
        strategy = StickySessionStrategy()
        groups = [_group("a", 1, 3), _group("b", 3, 3)]
        first = await strategy.select_weighted(groups, session_id="s-1")
        strategy.reset()
        again = await strategy.select_weighted(groups, session_id="s-1")
        assert first.id == again.id

    async def test_sticky_places_new_sessions_by_weight(self):
        strategy = StickySessionStrategy()
        groups = [_group("a", 1, 1), _group("b", 3, 1)]
        counts = Counter([strategy.pick_group(groups, f"session-{i}").key for i in range(4000)])
        assert 0.70 < counts["b"] / 4000 < 0.80

    async def test_sticky_weight_change_moves_only_a_slice(self):
        strategy = StickySessionStrategy()
        before = {i: strategy.pick_group([_group("a", 1, 1), _group("b", 1, 1)], f"s-{i}").key for i in range(2000)}
        after = {i: strategy.pick_group([_group("a", 1, 1), _group("b", 2, 1)], f"s-{i}").key for i in range(2000)}
        moved = sum(1 for i in before if before[i] != after[i])
        # a: 50% -> 33%; only sessions leaving "a" move, and none leave "b".
        assert all(after[i] == "b" for i in before if before[i] != after[i])
        assert 0.10 < moved / 2000 < 0.25

    async def test_sticky_keeps_existing_binding_over_weights(self):
        strategy = StickySessionStrategy()
        a, b = _group("a", 1, 1), _group("b", 1, 1)
        bound = await strategy.select_weighted([a, b], session_id="s-1")
        other = b if bound.connector_id == "a" else a
        heavy = ProxyGroup(key=other.key, weight=100, proxies=other.proxies)
        light = ProxyGroup(key=bound.connector_id, weight=1, proxies=(a if bound.connector_id == "a" else b).proxies)
        assert (await strategy.select_weighted([light, heavy], session_id="s-1")).id == bound.id

    async def test_sticky_without_session_uses_weights(self):
        random.seed(3)
        strategy = StickySessionStrategy()
        groups = [_group("a", 1, 2), _group("b", 3, 2)]
        counts = Counter([(await strategy.select_weighted(groups)).connector_id for _ in range(4000)])
        assert 0.70 < counts["b"] / 4000 < 0.80

    async def test_health_based_ignores_health_when_picking_connector(self) -> None:
        random.seed(11)
        strategy = HealthBasedStrategy(_measured(a_0=(100, 10, 90000.0), b_0=(100, 100, 5000.0)))
        sick, well = _group("a", 3, 1), _group("b", 1, 1)
        counts = Counter([(await strategy.select_weighted([sick, well])).connector_id for _ in range(4000)])
        assert 0.70 < counts["a"] / 4000 < 0.80
