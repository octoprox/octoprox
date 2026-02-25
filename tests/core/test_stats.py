# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for stats helper functions."""

from dataclasses import dataclass

from api.core.stats import apply_metrics, combine_metrics, increment_stats


@dataclass
class MockStatsObject:
    """Mock object that implements HasStats protocol."""

    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_latency_ms: float = 0.0
    bytes_sent: int = 0
    bytes_received: int = 0


class TestCombineMetrics:
    """Tests for combine_metrics function."""

    def test_combine_empty_metrics(self) -> None:
        """Test combining two empty metric dicts."""
        result = combine_metrics({}, {})

        assert result["request_count"] == 0
        assert result["success_count"] == 0
        assert result["failure_count"] == 0
        assert result["avg_latency_ms"] == 0.0
        assert result["bytes_sent"] == 0
        assert result["bytes_received"] == 0

    def test_combine_postgres_only(self) -> None:
        """Test combining when only Postgres has metrics."""
        pg = {
            "request_count": 100,
            "success_count": 90,
            "failure_count": 10,
            "avg_latency_ms": 150.0,
            "bytes_sent": 50000,
            "bytes_received": 200000,
        }
        result = combine_metrics(pg, {})

        assert result["request_count"] == 100
        assert result["success_count"] == 90
        assert result["failure_count"] == 10
        assert result["avg_latency_ms"] == 150.0
        assert result["bytes_sent"] == 50000
        assert result["bytes_received"] == 200000

    def test_combine_redis_only(self) -> None:
        """Test combining when only Redis has metrics."""
        rd = {
            "request_count": 50,
            "success_count": 45,
            "failure_count": 5,
            "avg_latency_ms": 100.0,
            "bytes_sent": 25000,
            "bytes_received": 100000,
        }
        result = combine_metrics({}, rd)

        assert result["request_count"] == 50
        assert result["success_count"] == 45
        assert result["failure_count"] == 5
        assert result["avg_latency_ms"] == 100.0
        assert result["bytes_sent"] == 25000
        assert result["bytes_received"] == 100000

    def test_combine_both_sources(self) -> None:
        """Test combining metrics from both Postgres and Redis."""
        pg = {
            "request_count": 100,
            "success_count": 90,
            "failure_count": 10,
            "avg_latency_ms": 200.0,
            "bytes_sent": 50000,
            "bytes_received": 200000,
        }
        rd = {
            "request_count": 50,
            "success_count": 45,
            "failure_count": 5,
            "avg_latency_ms": 100.0,
            "bytes_sent": 25000,
            "bytes_received": 100000,
        }
        result = combine_metrics(pg, rd)

        assert result["request_count"] == 150
        assert result["success_count"] == 135
        assert result["failure_count"] == 15
        assert result["bytes_sent"] == 75000
        assert result["bytes_received"] == 300000
        # Weighted average: (200*100 + 100*50) / 150 = 25000/150 = 166.67
        assert abs(result["avg_latency_ms"] - 166.67) < 0.01

    def test_weighted_average_latency(self) -> None:
        """Test that latency is properly weighted by request count."""
        pg = {"request_count": 10, "avg_latency_ms": 100.0}
        rd = {"request_count": 90, "avg_latency_ms": 200.0}
        result = combine_metrics(pg, rd)

        # Weighted: (100*10 + 200*90) / 100 = 19000/100 = 190
        assert result["avg_latency_ms"] == 190.0


class TestApplyMetrics:
    """Tests for apply_metrics function."""

    def test_apply_metrics_to_object(self) -> None:
        """Test applying metrics to a target object."""
        target = MockStatsObject()
        metrics = {
            "request_count": 100,
            "success_count": 90,
            "failure_count": 10,
            "avg_latency_ms": 150.5,
            "bytes_sent": 50000,
            "bytes_received": 200000,
        }
        apply_metrics(target, metrics)

        assert target.request_count == 100
        assert target.success_count == 90
        assert target.failure_count == 10
        assert target.avg_latency_ms == 150.5
        assert target.bytes_sent == 50000
        assert target.bytes_received == 200000

    def test_apply_partial_metrics(self) -> None:
        """Test applying partial metrics uses defaults."""
        target = MockStatsObject(request_count=50, success_count=40)
        metrics = {"request_count": 100}
        apply_metrics(target, metrics)

        assert target.request_count == 100
        assert target.success_count == 0  # Default when not in metrics
        assert target.failure_count == 0
        assert target.avg_latency_ms == 0.0


class TestIncrementStats:
    """Tests for increment_stats function."""

    def test_increment_first_request_success(self) -> None:
        """Test incrementing stats for first successful request."""
        target = MockStatsObject()
        increment_stats(target, success=True, latency_ms=100.0, bytes_sent=1000, bytes_received=5000)

        assert target.request_count == 1
        assert target.success_count == 1
        assert target.failure_count == 0
        assert target.avg_latency_ms == 100.0
        assert target.bytes_sent == 1000
        assert target.bytes_received == 5000

    def test_increment_first_request_failure(self) -> None:
        """Test incrementing stats for first failed request."""
        target = MockStatsObject()
        increment_stats(target, success=False, latency_ms=50.0, bytes_sent=500, bytes_received=0)

        assert target.request_count == 1
        assert target.success_count == 0
        assert target.failure_count == 1
        assert target.avg_latency_ms == 50.0
        assert target.bytes_sent == 500
        assert target.bytes_received == 0

    def test_increment_multiple_requests(self) -> None:
        """Test incrementing stats for multiple requests."""
        target = MockStatsObject()

        increment_stats(target, success=True, latency_ms=100.0, bytes_sent=1000, bytes_received=5000)
        increment_stats(target, success=True, latency_ms=200.0, bytes_sent=2000, bytes_received=10000)
        increment_stats(target, success=False, latency_ms=50.0, bytes_sent=500, bytes_received=0)

        assert target.request_count == 3
        assert target.success_count == 2
        assert target.failure_count == 1
        assert target.bytes_sent == 3500
        assert target.bytes_received == 15000
        # Running average: (100 + 200 + 50) / 3 = 116.67
        assert abs(target.avg_latency_ms - 116.67) < 0.01

    def test_running_average_latency(self) -> None:
        """Test that running average latency is calculated correctly."""
        target = MockStatsObject()

        # First request: avg = 100
        increment_stats(target, success=True, latency_ms=100.0)
        assert target.avg_latency_ms == 100.0

        # Second request: avg = (100 + 200) / 2 = 150
        increment_stats(target, success=True, latency_ms=200.0)
        assert target.avg_latency_ms == 150.0

        # Third request: avg = (150*2 + 300) / 3 = 600/3 = 200
        increment_stats(target, success=True, latency_ms=300.0)
        assert target.avg_latency_ms == 200.0

    def test_increment_with_existing_stats(self) -> None:
        """Test incrementing stats on object with existing values."""
        target = MockStatsObject(
            request_count=100,
            success_count=90,
            failure_count=10,
            avg_latency_ms=150.0,
            bytes_sent=50000,
            bytes_received=200000,
        )

        increment_stats(target, success=True, latency_ms=50.0, bytes_sent=1000, bytes_received=5000)

        assert target.request_count == 101
        assert target.success_count == 91
        assert target.failure_count == 10
        assert target.bytes_sent == 51000
        assert target.bytes_received == 205000
        # Running average: (150*100 + 50) / 101 = 15050/101 ≈ 149.01
        assert abs(target.avg_latency_ms - 149.01) < 0.01

    def test_increment_default_bytes(self) -> None:
        """Test that bytes default to 0 when not provided."""
        target = MockStatsObject()
        increment_stats(target, success=True, latency_ms=100.0)

        assert target.bytes_sent == 0
        assert target.bytes_received == 0
