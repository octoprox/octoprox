# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for routing strategies."""

import random
from collections import Counter

import pytest

from api.models.proxy import Proxy, ProxyStatus
from api.strategies.base import ProxyGroup
from api.strategies.health_based import HealthBasedStrategy
from api.strategies.least_used import LeastUsedStrategy
from api.strategies.random import RandomStrategy
from api.strategies.round_robin import RoundRobinStrategy
from api.strategies.sticky import StickySessionStrategy


@pytest.fixture
def sample_proxies() -> list[Proxy]:
    """Create a list of sample proxies for testing."""
    return [
        Proxy(id="proxy-1", host="proxy1.example.com", port=8080, connector_id="conn-1", request_count=10, success_count=9, status=ProxyStatus.HEALTHY, avg_latency_ms=100),
        Proxy(id="proxy-2", host="proxy2.example.com", port=8080, connector_id="conn-1", request_count=5, success_count=5, status=ProxyStatus.HEALTHY, avg_latency_ms=50),
        Proxy(id="proxy-3", host="proxy3.example.com", port=8080, connector_id="conn-1", request_count=20, success_count=15, status=ProxyStatus.DEGRADED, avg_latency_ms=200),
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
    """Tests for LeastUsedStrategy."""

    def test_name(self):
        strategy = LeastUsedStrategy()
        assert strategy.name == "least_used"

    async def test_select_empty_list(self):
        strategy = LeastUsedStrategy()
        result = await strategy.select([])
        assert result is None

    async def test_select_returns_least_used(self, sample_proxies: list[Proxy]):
        strategy = LeastUsedStrategy()

        # proxy-2 has the lowest request_count (5)
        result = await strategy.select(sample_proxies)
        assert result.id == "proxy-2"

    async def test_select_with_equal_counts(self):
        strategy = LeastUsedStrategy()
        proxies = [
            Proxy(id="proxy-1", host="p1.example.com", port=8080, connector_id="conn-1", request_count=10),
            Proxy(id="proxy-2", host="p2.example.com", port=8080, connector_id="conn-1", request_count=10),
        ]

        result = await strategy.select(proxies)
        assert result is not None
        assert result.request_count == 10


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
    """Tests for StickySessionStrategy.

    These cover the in-process behaviour only - no redis_client is
    passed, so the strategy operates purely from ``_session_map``.
    Cross-instance behaviour (Redis read-through inside ``select``) is
    exercised in tests/core/test_cross_instance_events.py.
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
        redis = AsyncMock()
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
        redis = AsyncMock()
        redis.get_sticky_binding.return_value = None
        first = await strategy.select(sample_proxies, session_id="s", redis_client=redis, project_id="p")
        assert redis.set_sticky_binding.await_count == 1
        for _ in range(10):
            now[0] += 10
            await strategy.select(sample_proxies, session_id="s", redis_client=redis, project_id="p")
        # 100 seconds in: still within the first half TTL, nothing rewritten.
        assert redis.set_sticky_binding.await_count == 1
        now[0] += 60
        await strategy.select(sample_proxies, session_id="s", redis_client=redis, project_id="p")
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


class TestHealthBasedStrategy:
    """Tests for HealthBasedStrategy."""

    def test_name(self):
        strategy = HealthBasedStrategy()
        assert strategy.name == "health_based"

    async def test_select_empty_list(self):
        strategy = HealthBasedStrategy()
        result = await strategy.select([])
        assert result is None

    async def test_select_prefers_healthy_proxies(self):
        strategy = HealthBasedStrategy()
        proxies = [
            Proxy(id="healthy", host="h.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.HEALTHY, success_count=90, request_count=100, avg_latency_ms=50),
            Proxy(id="unhealthy", host="u.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.UNHEALTHY, success_count=10, request_count=100, avg_latency_ms=500),
        ]

        # Run multiple times - should always prefer healthy
        for _ in range(10):
            result = await strategy.select(proxies)
            assert result.id == "healthy"

    async def test_select_falls_back_to_degraded(self):
        strategy = HealthBasedStrategy()
        proxies = [
            Proxy(id="degraded", host="d.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.DEGRADED, success_count=70, request_count=100),
            Proxy(id="unhealthy", host="u.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.UNHEALTHY, success_count=10, request_count=100),
        ]

        # Should prefer degraded over unhealthy
        for _ in range(10):
            result = await strategy.select(proxies)
            assert result.id == "degraded"

    async def test_select_considers_success_rate(self):
        strategy = HealthBasedStrategy()
        proxies = [
            Proxy(id="high-success", host="h.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.HEALTHY, success_count=95, request_count=100, avg_latency_ms=100),
            Proxy(id="low-success", host="l.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.HEALTHY, success_count=50, request_count=100, avg_latency_ms=100),
        ]

        # High success rate should be preferred
        results = [await strategy.select(proxies) for _ in range(20)]
        high_success_count = sum(1 for r in results if r.id == "high-success")

        # Should mostly select high-success proxy
        assert high_success_count > 10

    async def test_select_considers_latency(self):
        strategy = HealthBasedStrategy()
        proxies = [
            Proxy(id="fast", host="f.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.HEALTHY, success_count=90, request_count=100, avg_latency_ms=50),
            Proxy(id="slow", host="s.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.HEALTHY, success_count=90, request_count=100, avg_latency_ms=500),
        ]

        # Fast proxy should be preferred
        results = [await strategy.select(proxies) for _ in range(20)]
        fast_count = sum(1 for r in results if r.id == "fast")

        # Should mostly select fast proxy
        assert fast_count > 10

    def test_calculate_score(self):
        strategy = HealthBasedStrategy()

        # High success rate, low latency = high score
        good_proxy = Proxy(id="good", host="g.example.com", port=8080, connector_id="conn-1", success_count=95, request_count=100, avg_latency_ms=50)

        # Low success rate, high latency = low score
        bad_proxy = Proxy(id="bad", host="b.example.com", port=8080, connector_id="conn-1", success_count=30, request_count=100, avg_latency_ms=800)

        good_score = strategy._calculate_score(good_proxy)
        bad_score = strategy._calculate_score(bad_proxy)

        assert good_score > bad_score


def _group(key: str, weight: int, count: int, requests: int = 0, load: int | None = None) -> ProxyGroup:
    return ProxyGroup(
        key=key,
        weight=weight,
        proxies=[
            Proxy(id=f"{key}-{i}", host=f"{key}{i}", port=1, connector_id=key, request_count=requests, status=ProxyStatus.HEALTHY)
            for i in range(count)
        ],
        load=requests * count if load is None else load,
    )


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

    async def test_least_used_treats_weight_as_capacity(self):
        strategy = LeastUsedStrategy()
        # "a" has served 9 requests at weight 3 (3 per unit); "b" 4 at weight 1 (4 per unit).
        groups = [_group("a", 3, 3, requests=3), _group("b", 1, 1, requests=4)]
        assert (await strategy.select_weighted(groups)).connector_id == "a"
        groups = [_group("a", 3, 3, requests=5), _group("b", 1, 1, requests=4)]
        assert (await strategy.select_weighted(groups)).connector_id == "b"

    async def test_least_used_counts_the_whole_connector_not_the_eligible_slice(self):
        strategy = LeastUsedStrategy()
        # "a" is a busy 50-row connector of which 5 rows serve this request; "b" is one idle row.
        busy = _group("a", 1, 5, requests=200, load=10_000)
        idle = _group("b", 1, 1, requests=5000, load=5000)
        assert (await strategy.select_weighted([busy, idle])).connector_id == "b"

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

    async def test_health_based_ignores_health_when_picking_connector(self):
        random.seed(11)
        strategy = HealthBasedStrategy()
        sick = _group("a", 3, 1)
        for p in sick.proxies:
            p.request_count, p.success_count, p.avg_latency_ms = 100, 10, 900
        well = _group("b", 1, 1)
        for p in well.proxies:
            p.request_count, p.success_count, p.avg_latency_ms = 100, 100, 50
        counts = Counter([(await strategy.select_weighted([sick, well])).connector_id for _ in range(4000)])
        assert 0.70 < counts["a"] / 4000 < 0.80
