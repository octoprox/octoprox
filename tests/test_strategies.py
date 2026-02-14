"""Tests for routing strategies."""

import pytest

from api.models.proxy import Proxy, ProxyStatus
from api.strategies.round_robin import RoundRobinStrategy
from api.strategies.least_used import LeastUsedStrategy
from api.strategies.random import RandomStrategy
from api.strategies.sticky import StickySessionStrategy
from api.strategies.health_based import HealthBasedStrategy


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

    def test_select_empty_list(self):
        strategy = RoundRobinStrategy()
        result = strategy.select([])
        assert result is None

    def test_select_cycles_through_proxies(self, sample_proxies: list[Proxy]):
        strategy = RoundRobinStrategy()
        
        # First cycle
        assert strategy.select(sample_proxies).id == "proxy-1"
        assert strategy.select(sample_proxies).id == "proxy-2"
        assert strategy.select(sample_proxies).id == "proxy-3"
        
        # Second cycle - should wrap around
        assert strategy.select(sample_proxies).id == "proxy-1"

    def test_reset(self, sample_proxies: list[Proxy]):
        strategy = RoundRobinStrategy()
        
        strategy.select(sample_proxies)  # Move to index 1
        strategy.reset()
        
        # Should start from beginning again
        assert strategy.select(sample_proxies).id == "proxy-1"


class TestLeastUsedStrategy:
    """Tests for LeastUsedStrategy."""

    def test_name(self):
        strategy = LeastUsedStrategy()
        assert strategy.name == "least_used"

    def test_select_empty_list(self):
        strategy = LeastUsedStrategy()
        result = strategy.select([])
        assert result is None

    def test_select_returns_least_used(self, sample_proxies: list[Proxy]):
        strategy = LeastUsedStrategy()
        
        # proxy-2 has the lowest request_count (5)
        result = strategy.select(sample_proxies)
        assert result.id == "proxy-2"

    def test_select_with_equal_counts(self):
        strategy = LeastUsedStrategy()
        proxies = [
            Proxy(id="proxy-1", host="p1.example.com", port=8080, connector_id="conn-1", request_count=10),
            Proxy(id="proxy-2", host="p2.example.com", port=8080, connector_id="conn-1", request_count=10),
        ]

        result = strategy.select(proxies)
        assert result is not None
        assert result.request_count == 10


class TestRandomStrategy:
    """Tests for RandomStrategy."""

    def test_name(self):
        strategy = RandomStrategy()
        assert strategy.name == "random"

    def test_select_empty_list(self):
        strategy = RandomStrategy()
        result = strategy.select([])
        assert result is None

    def test_select_returns_valid_proxy(self, sample_proxies: list[Proxy]):
        strategy = RandomStrategy()
        
        for _ in range(10):
            result = strategy.select(sample_proxies)
            assert result is not None
            assert result in sample_proxies


class TestStickySessionStrategy:
    """Tests for StickySessionStrategy."""

    def test_name(self):
        strategy = StickySessionStrategy()
        assert strategy.name == "sticky"

    def test_select_empty_list(self):
        strategy = StickySessionStrategy()
        result = strategy.select([])
        assert result is None

    def test_select_without_session_returns_random(self, sample_proxies: list[Proxy]):
        strategy = StickySessionStrategy()
        
        result = strategy.select(sample_proxies, session_id=None)
        assert result is not None
        assert result in sample_proxies

    def test_select_same_session_returns_same_proxy(self, sample_proxies: list[Proxy]):
        strategy = StickySessionStrategy()
        session_id = "user-session-123"
        
        first_result = strategy.select(sample_proxies, session_id=session_id)
        
        # Same session should return same proxy
        for _ in range(5):
            result = strategy.select(sample_proxies, session_id=session_id)
            assert result.id == first_result.id

    def test_select_different_sessions_can_differ(self, sample_proxies: list[Proxy]):
        strategy = StickySessionStrategy()
        
        result1 = strategy.select(sample_proxies, session_id="session-1")
        result2 = strategy.select(sample_proxies, session_id="session-2")
        
        # Both should be valid proxies (may or may not be the same)
        assert result1 in sample_proxies
        assert result2 in sample_proxies

    def test_reset_clears_session_map(self, sample_proxies: list[Proxy]):
        strategy = StickySessionStrategy()
        session_id = "user-session-123"

        first_result = strategy.select(sample_proxies, session_id=session_id)
        strategy.reset()

        # After reset, session map is cleared but consistent hashing should give same result
        second_result = strategy.select(sample_proxies, session_id=session_id)
        assert second_result.id == first_result.id  # Consistent hashing


class TestHealthBasedStrategy:
    """Tests for HealthBasedStrategy."""

    def test_name(self):
        strategy = HealthBasedStrategy()
        assert strategy.name == "health_based"

    def test_select_empty_list(self):
        strategy = HealthBasedStrategy()
        result = strategy.select([])
        assert result is None

    def test_select_prefers_healthy_proxies(self):
        strategy = HealthBasedStrategy()
        proxies = [
            Proxy(id="healthy", host="h.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.HEALTHY, success_count=90, request_count=100, avg_latency_ms=50),
            Proxy(id="unhealthy", host="u.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.UNHEALTHY, success_count=10, request_count=100, avg_latency_ms=500),
        ]

        # Run multiple times - should always prefer healthy
        for _ in range(10):
            result = strategy.select(proxies)
            assert result.id == "healthy"

    def test_select_falls_back_to_degraded(self):
        strategy = HealthBasedStrategy()
        proxies = [
            Proxy(id="degraded", host="d.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.DEGRADED, success_count=70, request_count=100),
            Proxy(id="unhealthy", host="u.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.UNHEALTHY, success_count=10, request_count=100),
        ]

        # Should prefer degraded over unhealthy
        for _ in range(10):
            result = strategy.select(proxies)
            assert result.id == "degraded"

    def test_select_considers_success_rate(self):
        strategy = HealthBasedStrategy()
        proxies = [
            Proxy(id="high-success", host="h.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.HEALTHY, success_count=95, request_count=100, avg_latency_ms=100),
            Proxy(id="low-success", host="l.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.HEALTHY, success_count=50, request_count=100, avg_latency_ms=100),
        ]

        # High success rate should be preferred
        results = [strategy.select(proxies) for _ in range(20)]
        high_success_count = sum(1 for r in results if r.id == "high-success")

        # Should mostly select high-success proxy
        assert high_success_count > 10

    def test_select_considers_latency(self):
        strategy = HealthBasedStrategy()
        proxies = [
            Proxy(id="fast", host="f.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.HEALTHY, success_count=90, request_count=100, avg_latency_ms=50),
            Proxy(id="slow", host="s.example.com", port=8080, connector_id="conn-1", status=ProxyStatus.HEALTHY, success_count=90, request_count=100, avg_latency_ms=500),
        ]

        # Fast proxy should be preferred
        results = [strategy.select(proxies) for _ in range(20)]
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

