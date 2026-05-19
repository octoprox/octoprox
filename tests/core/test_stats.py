# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for stats helper functions."""

from dataclasses import dataclass

from api.core.stats import (
    accumulate_delta,
    apply_delta,
    apply_metrics,
    combine_metrics,
    empty_delta,
    increment_stats,
    merge_delta_into,
)


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


class TestEmptyDelta:
    """Tests for empty_delta()."""

    def test_all_fields_zero(self) -> None:
        d = empty_delta()
        assert d["request_count"] == 0
        assert d["success_count"] == 0
        assert d["failure_count"] == 0
        assert d["latency_sum_ms"] == 0.0
        assert d["bytes_sent"] == 0
        assert d["bytes_received"] == 0

    def test_independent_instances(self) -> None:
        """Each call returns a fresh dict — mutating one doesn't affect the other."""
        a = empty_delta()
        b = empty_delta()
        a["request_count"] = 5
        assert b["request_count"] == 0


class TestAccumulateDelta:
    """Tests for accumulate_delta()."""

    def test_single_success(self) -> None:
        d = empty_delta()
        accumulate_delta(d, success=True, latency_ms=100.0, bytes_sent=10, bytes_received=20)
        assert d == {
            "request_count": 1,
            "success_count": 1,
            "failure_count": 0,
            "latency_sum_ms": 100.0,
            "bytes_sent": 10,
            "bytes_received": 20,
        }

    def test_single_failure(self) -> None:
        d = empty_delta()
        accumulate_delta(d, success=False, latency_ms=50.0)
        assert d["request_count"] == 1
        assert d["success_count"] == 0
        assert d["failure_count"] == 1
        assert d["latency_sum_ms"] == 50.0

    def test_running_sum_across_many(self) -> None:
        d = empty_delta()
        for latency in (100.0, 200.0, 300.0):
            accumulate_delta(d, success=True, latency_ms=latency, bytes_sent=1, bytes_received=2)
        # accumulate_delta keeps the SUM of latencies, not the average —
        # that's what makes batched aggregation correct downstream.
        assert d["request_count"] == 3
        assert d["success_count"] == 3
        assert d["failure_count"] == 0
        assert d["latency_sum_ms"] == 600.0
        assert d["bytes_sent"] == 3
        assert d["bytes_received"] == 6

    def test_mixed_success_failure(self) -> None:
        d = empty_delta()
        accumulate_delta(d, success=True, latency_ms=10.0)
        accumulate_delta(d, success=False, latency_ms=20.0)
        accumulate_delta(d, success=True, latency_ms=30.0)
        assert d["request_count"] == 3
        assert d["success_count"] == 2
        assert d["failure_count"] == 1
        assert d["latency_sum_ms"] == 60.0


class TestMergeDeltaInto:
    """Tests for merge_delta_into()."""

    def test_merges_field_by_field(self) -> None:
        dst = empty_delta()
        dst["request_count"] = 3
        dst["success_count"] = 2
        dst["failure_count"] = 1
        dst["latency_sum_ms"] = 60.0
        dst["bytes_sent"] = 30
        dst["bytes_received"] = 60

        src = empty_delta()
        src["request_count"] = 7
        src["success_count"] = 6
        src["failure_count"] = 1
        src["latency_sum_ms"] = 140.0
        src["bytes_sent"] = 70
        src["bytes_received"] = 140

        merge_delta_into(dst, src)

        assert dst["request_count"] == 10
        assert dst["success_count"] == 8
        assert dst["failure_count"] == 2
        assert dst["latency_sum_ms"] == 200.0
        assert dst["bytes_sent"] == 100
        assert dst["bytes_received"] == 200

    def test_merge_into_empty(self) -> None:
        dst = empty_delta()
        src = empty_delta()
        accumulate_delta(src, success=True, latency_ms=42.0, bytes_sent=5, bytes_received=10)
        merge_delta_into(dst, src)
        assert dst == src

    def test_partial_src_uses_zero_defaults(self) -> None:
        """A delta arriving over Pub/Sub with missing optional fields
        should still merge cleanly (uses .get with zero defaults)."""
        dst = empty_delta()
        dst["request_count"] = 5
        src: dict = {"request_count": 2}  # no other fields
        merge_delta_into(dst, src)
        assert dst["request_count"] == 7
        # Missing fields left untouched
        assert dst["success_count"] == 0
        assert dst["latency_sum_ms"] == 0.0


class TestApplyDelta:
    """Tests for apply_delta() — applies a batch of N requests to a target."""

    def test_no_op_on_zero_requests(self) -> None:
        target = MockStatsObject(request_count=42, avg_latency_ms=99.0)
        apply_delta(target, empty_delta())
        assert target.request_count == 42
        assert target.avg_latency_ms == 99.0

    def test_apply_to_empty_target(self) -> None:
        target = MockStatsObject()
        delta = empty_delta()
        # 3 successful requests, latency sum 300ms → avg 100
        for _ in range(3):
            accumulate_delta(delta, success=True, latency_ms=100.0, bytes_sent=10, bytes_received=20)
        apply_delta(target, delta)
        assert target.request_count == 3
        assert target.success_count == 3
        assert target.failure_count == 0
        assert target.avg_latency_ms == 100.0
        assert target.bytes_sent == 30
        assert target.bytes_received == 60

    def test_weighted_average_across_batches(self) -> None:
        """A batch added to a pre-existing target uses weighted-average latency."""
        target = MockStatsObject(
            request_count=10,
            success_count=10,
            failure_count=0,
            avg_latency_ms=50.0,  # 10 requests at 50ms each → 500ms total
        )
        delta = empty_delta()
        # 5 more requests at 100ms each → 500ms sum
        for _ in range(5):
            accumulate_delta(delta, success=True, latency_ms=100.0)
        apply_delta(target, delta)
        # Combined: 15 requests, total latency 500 + 500 = 1000ms → avg 66.67
        assert target.request_count == 15
        assert target.success_count == 15
        assert abs(target.avg_latency_ms - (1000.0 / 15)) < 1e-9

    def test_apply_preserves_existing_byte_counters(self) -> None:
        target = MockStatsObject(bytes_sent=1000, bytes_received=2000)
        delta = empty_delta()
        accumulate_delta(delta, success=True, latency_ms=10.0, bytes_sent=100, bytes_received=200)
        apply_delta(target, delta)
        assert target.bytes_sent == 1100
        assert target.bytes_received == 2200

    def test_apply_handles_failures(self) -> None:
        target = MockStatsObject()
        delta = empty_delta()
        accumulate_delta(delta, success=True, latency_ms=100.0)
        accumulate_delta(delta, success=False, latency_ms=200.0)
        apply_delta(target, delta)
        assert target.success_count == 1
        assert target.failure_count == 1
        assert target.request_count == 2

    def test_apply_with_only_partial_keys(self) -> None:
        """Deltas deserialised from JSON may be missing optional keys —
        apply_delta uses .get with zero defaults so it stays robust."""
        target = MockStatsObject()
        delta = {"request_count": 4, "success_count": 4, "latency_sum_ms": 400.0}
        apply_delta(target, delta)
        assert target.request_count == 4
        assert target.success_count == 4
        assert target.failure_count == 0
        assert target.avg_latency_ms == 100.0
        assert target.bytes_sent == 0
        assert target.bytes_received == 0
