# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for stats helper functions."""

from dataclasses import dataclass

from api.core.stats import MetricDelta


@dataclass
class MockStatsObject:
    """Mock object that implements HasStats protocol."""

    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_latency_ms: float = 0.0
    bytes_sent: int = 0
    bytes_received: int = 0


class TestSummedAndSetOn:
    """Hydration: history totals plus the Redis window, set on an entity."""

    def test_summed_weights_latency_by_count(self) -> None:
        pg = MetricDelta(request_count=100, success_count=90, failure_count=10, latency_sum_ms=20000.0, bytes_sent=50000, bytes_received=200000)
        rd = MetricDelta(request_count=50, success_count=45, failure_count=5, latency_sum_ms=5000.0, bytes_sent=25000, bytes_received=100000)
        total = MetricDelta.summed(pg, rd)
        assert (total.request_count, total.success_count, total.failure_count) == (150, 135, 15)
        assert (total.bytes_sent, total.bytes_received) == (75000, 300000)
        # (200*100 + 100*50) / 150 = 166.67
        assert abs(total.avg_latency_ms - 166.67) < 0.01

    def test_summed_skips_missing_sources(self) -> None:
        only = MetricDelta(request_count=10, latency_sum_ms=1000.0)
        assert MetricDelta.summed(None, only) == only
        assert MetricDelta.summed(None, None) == MetricDelta()
        assert MetricDelta.summed().avg_latency_ms == 0.0

    def test_set_on_replaces_every_counter(self) -> None:
        target = MockStatsObject(request_count=50, success_count=40, avg_latency_ms=9.0)
        MetricDelta(request_count=100, success_count=90, failure_count=10, latency_sum_ms=15050.0, bytes_sent=50000, bytes_received=200000).set_on(target)
        assert (target.request_count, target.success_count, target.failure_count) == (100, 90, 10)
        assert target.avg_latency_ms == 150.5
        assert (target.bytes_sent, target.bytes_received) == (50000, 200000)

    def test_set_on_empty_clears(self) -> None:
        target = MockStatsObject(request_count=50, success_count=40, avg_latency_ms=9.0, bytes_sent=5)
        MetricDelta().set_on(target)
        assert target == MockStatsObject()


class TestMetricDelta:
    """Tests for MetricDelta: accumulate, merge, wire form."""

    def test_starts_at_zero(self) -> None:
        d = MetricDelta()
        assert d == MetricDelta(request_count=0, success_count=0, failure_count=0, latency_sum_ms=0.0, bytes_sent=0, bytes_received=0)

    def test_single_success(self) -> None:
        d = MetricDelta()
        d.add_request(success=True, latency_ms=100.0, bytes_sent=10, bytes_received=20)
        assert d == MetricDelta(
            request_count=1, success_count=1, failure_count=0, latency_sum_ms=100.0, bytes_sent=10, bytes_received=20,
        )

    def test_single_failure(self) -> None:
        d = MetricDelta()
        d.add_request(success=False, latency_ms=50.0)
        assert (d.request_count, d.success_count, d.failure_count, d.latency_sum_ms) == (1, 0, 1, 50.0)

    def test_running_sum_across_many(self) -> None:
        d = MetricDelta()
        for latency in (100.0, 200.0, 300.0):
            d.add_request(success=True, latency_ms=latency, bytes_sent=1, bytes_received=2)
        # The delta keeps the SUM of latencies, not the average -
        # that's what makes batched aggregation correct downstream.
        assert (d.request_count, d.success_count, d.failure_count) == (3, 3, 0)
        assert d.latency_sum_ms == 600.0
        assert (d.bytes_sent, d.bytes_received) == (3, 6)

    def test_add_bytes_counts_no_request(self) -> None:
        d = MetricDelta()
        d.add_bytes(100, 200)
        assert d == MetricDelta(bytes_sent=100, bytes_received=200)

    def test_merge_field_by_field(self) -> None:
        dst = MetricDelta(request_count=3, success_count=2, failure_count=1, latency_sum_ms=60.0, bytes_sent=30, bytes_received=60)
        src = MetricDelta(request_count=7, success_count=6, failure_count=1, latency_sum_ms=140.0, bytes_sent=70, bytes_received=140)
        dst.merge(src)
        assert dst == MetricDelta(request_count=10, success_count=8, failure_count=2, latency_sum_ms=200.0, bytes_sent=100, bytes_received=200)

    def test_merge_into_empty(self) -> None:
        dst = MetricDelta()
        src = MetricDelta()
        src.add_request(success=True, latency_ms=42.0, bytes_sent=5, bytes_received=10)
        dst.merge(src)
        assert dst == src

    def test_wire_round_trip(self) -> None:
        deltas = {"a": MetricDelta(request_count=2, latency_sum_ms=10.5), "b": MetricDelta(bytes_sent=7)}
        wire = MetricDelta.dump_many(deltas)
        assert wire["a"] == {"request_count": 2, "success_count": 0, "failure_count": 0, "latency_sum_ms": 10.5, "bytes_sent": 0, "bytes_received": 0}
        assert MetricDelta.parse_many(wire) == deltas

    def test_parse_tolerates_missing_fields_and_junk(self) -> None:
        """A peer on a version with fewer fields still applies; garbage entries are dropped."""
        parsed = MetricDelta.parse_many({"a": {"request_count": 2}, "b": {"request_count": "lots"}, "c": "nope"})
        assert parsed == {"a": MetricDelta(request_count=2)}
        assert MetricDelta.parse_many(None) == {}
        assert MetricDelta.parse_many([1, 2]) == {}


class TestApplyTo:
    """Tests for MetricDelta.apply_to() - applies a batch of N requests to a target."""

    def test_no_op_on_zero_requests(self) -> None:
        target = MockStatsObject(request_count=42, avg_latency_ms=99.0)
        MetricDelta().apply_to(target)
        assert target.request_count == 42
        assert target.avg_latency_ms == 99.0

    def test_apply_to_empty_target(self) -> None:
        target = MockStatsObject()
        delta = MetricDelta()
        # 3 successful requests, latency sum 300ms → avg 100
        for _ in range(3):
            delta.add_request(success=True, latency_ms=100.0, bytes_sent=10, bytes_received=20)
        delta.apply_to(target)
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
        delta = MetricDelta()
        # 5 more requests at 100ms each → 500ms sum
        for _ in range(5):
            delta.add_request(success=True, latency_ms=100.0)
        delta.apply_to(target)
        # Combined: 15 requests, total latency 500 + 500 = 1000ms → avg 66.67
        assert target.request_count == 15
        assert target.success_count == 15
        assert abs(target.avg_latency_ms - (1000.0 / 15)) < 1e-9

    def test_progress_only_delta_moves_bytes_but_not_requests(self) -> None:
        """A running transfer reports bytes without a request; latency and counts stay put."""
        target = MockStatsObject(request_count=4, avg_latency_ms=80.0, bytes_sent=10, bytes_received=20)
        delta = MetricDelta()
        delta.add_bytes(100, 200)
        delta.apply_to(target)
        assert target.request_count == 4
        assert target.avg_latency_ms == 80.0
        assert (target.bytes_sent, target.bytes_received) == (110, 220)

    def test_apply_preserves_existing_byte_counters(self) -> None:
        target = MockStatsObject(bytes_sent=1000, bytes_received=2000)
        delta = MetricDelta()
        delta.add_request(success=True, latency_ms=10.0, bytes_sent=100, bytes_received=200)
        delta.apply_to(target)
        assert target.bytes_sent == 1100
        assert target.bytes_received == 2200

    def test_apply_handles_failures(self) -> None:
        target = MockStatsObject()
        delta = MetricDelta()
        delta.add_request(success=True, latency_ms=100.0)
        delta.add_request(success=False, latency_ms=200.0)
        delta.apply_to(target)
        assert target.success_count == 1
        assert target.failure_count == 1
        assert target.request_count == 2

    def test_apply_from_partial_wire_form(self) -> None:
        """Deltas deserialised from JSON may be missing optional keys; they read as zero."""
        target = MockStatsObject()
        delta = MetricDelta.model_validate({"request_count": 4, "success_count": 4, "latency_sum_ms": 400.0})
        delta.apply_to(target)
        assert target.request_count == 4
        assert target.success_count == 4
        assert target.failure_count == 0
        assert target.avg_latency_ms == 100.0
        assert target.bytes_sent == 0
        assert target.bytes_received == 0
