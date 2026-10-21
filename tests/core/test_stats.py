# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for stats helper functions."""

import pytest

from api.core.stats import (
    HOST_OVERFLOW,
    MetricDelta,
    TunnelPeerMetricDelta,
    fold_host_tail,
    normalize_host,
)


class TestSummed:
    """An entity's counters: its stored totals plus the Redis window not flushed yet."""

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
        delta = MetricDelta(request_count=2, latency_sum_ms=10.5)
        wire = delta.to_dict()
        assert wire == {"request_count": 2, "success_count": 0, "failure_count": 0, "latency_sum_ms": 10.5, "bytes_sent": 0, "bytes_received": 0}
        assert MetricDelta.from_dict(wire) == delta

    def test_from_dict_reads_missing_fields_as_zero_and_refuses_junk(self) -> None:
        """A hash written with fewer fields still reads; a value that is not a number does not."""
        assert MetricDelta.from_dict({"request_count": 2}) == MetricDelta(request_count=2)
        with pytest.raises(ValueError):
            MetricDelta.from_dict({"request_count": "lots"})
        with pytest.raises(TypeError):
            MetricDelta.from_dict("nope")
        # Numbers arriving as strings (Redis hash values) or floats for int fields still land as the right types.
        coerced = MetricDelta.from_dict({"request_count": "3", "bytes_sent": 7.0, "latency_sum_ms": 5})
        assert coerced == MetricDelta(request_count=3, bytes_sent=7, latency_sum_ms=5.0)
        assert isinstance(coerced.latency_sum_ms, float) and isinstance(coerced.bytes_sent, int)


class TestIncrements:
    """What a window adds to its Redis hash: each counter that moved, typed and tagged by its field's declared type."""

    def test_each_counter_that_moved_in_field_order_extras_included(self) -> None:
        delta = TunnelPeerMetricDelta(request_count=2, latency_sum_ms=12.5, bytes_received=7, encrypted_dns_blocked=1)

        assert list(delta.increments()) == [
            ("request_count", 2, False),
            ("latency_sum_ms", 12.5, True),
            ("bytes_received", 7, False),
            ("encrypted_dns_blocked", 1, False),
        ]
        assert list(MetricDelta().increments()) == []

    def test_the_declared_type_decides_not_the_amount(self) -> None:
        """Redis refuses HINCRBY once a field holds a fraction, so a whole latency still goes as a fraction."""
        latency, sent = MetricDelta(latency_sum_ms=250, bytes_sent=3.0).increments()  # type: ignore[arg-type]

        assert latency == ("latency_sum_ms", 250.0, True) and isinstance(latency[1], float)
        assert sent == ("bytes_sent", 3, False) and isinstance(sent[1], int)


class TestTunnelPeerMetricDelta:
    """The device shape: the request fields plus the two name-resolution counters."""

    def test_extra_counters_travel_on_the_wire(self) -> None:
        d = TunnelPeerMetricDelta(by_address=2, encrypted_dns_blocked=1)
        d.add_request(success=True, latency_ms=20.0, bytes_sent=5, bytes_received=9)
        wire = d.to_dict()
        assert wire["by_address"] == 2 and wire["encrypted_dns_blocked"] == 1
        assert wire["request_count"] == 1 and wire["bytes_received"] == 9
        parsed = TunnelPeerMetricDelta.from_dict(wire)
        assert parsed == d
        assert isinstance(parsed, TunnelPeerMetricDelta)

    def test_merge_and_sum_include_the_extras(self) -> None:
        a = TunnelPeerMetricDelta(request_count=1, success_count=1, latency_sum_ms=10.0, by_address=1)
        b = TunnelPeerMetricDelta(request_count=1, failure_count=1, latency_sum_ms=30.0, encrypted_dns_blocked=4)
        total = TunnelPeerMetricDelta.summed(a, None, b)
        assert isinstance(total, TunnelPeerMetricDelta)
        assert (total.request_count, total.by_address, total.encrypted_dns_blocked) == (2, 1, 4)
        assert total.avg_latency_ms == 20.0
        # A plain delta merges its own fields and leaves the extras alone.
        total.merge(MetricDelta(bytes_sent=7))
        assert (total.bytes_sent, total.by_address) == (7, 1)

    def test_older_wire_form_reads_extras_as_zero(self) -> None:
        parsed = TunnelPeerMetricDelta.from_dict({"request_count": "3", "bytes_sent": "9"})
        assert parsed == TunnelPeerMetricDelta(request_count=3, bytes_sent=9)


class TestNormalizeHost:
    """The form a destination is counted under on the Hosts page."""

    def test_folds_case_and_trailing_dot(self) -> None:
        assert normalize_host("Shop.Example.COM.") == "shop.example.com"
        assert normalize_host("  api.example.org ") == "api.example.org"

    def test_keeps_address_literals(self) -> None:
        assert normalize_host("93.184.216.34") == "93.184.216.34"
        assert normalize_host("[2001:DB8::1]") == "[2001:db8::1]"

    def test_nothing_countable_is_none(self) -> None:
        assert normalize_host(None) is None
        assert normalize_host("") is None
        assert normalize_host("   ") is None
        assert normalize_host("bad host") is None
        # Control characters are not hosts either; a NUL would be rejected by
        # Postgres and abort the flush that carried it.
        assert normalize_host("evil\x00.example.com") is None
        assert normalize_host("tab\t.example.com") is None

    def test_overlong_names_fold_into_the_overflow_row(self) -> None:
        assert normalize_host("a" * 300) == HOST_OVERFLOW
        assert normalize_host(HOST_OVERFLOW) == HOST_OVERFLOW


class TestFoldHostTail:
    """A window's host rows under a ceiling: the busiest named per project, the rest in one row per connector."""

    def test_without_a_ceiling_every_host_is_named(self) -> None:
        window = {("P", "c1", f"h{i}.example.com"): MetricDelta(request_count=1) for i in range(50)}
        assert fold_host_tail(window, 0) is window

    def test_the_busiest_hosts_are_kept_whatever_order_they_arrived_in(self) -> None:
        window = {
            ("P", "c1", "a.example.com"): MetricDelta(request_count=1, bytes_sent=1),
            ("P", "c1", "b.example.com"): MetricDelta(request_count=1, bytes_sent=1),
            ("P", "c1", "c.example.com"): MetricDelta(request_count=1, bytes_sent=1),
            ("P", "c1", "d.example.com"): MetricDelta(request_count=2, bytes_sent=2),
            ("P", "c1", "e.example.com"): MetricDelta(request_count=3, bytes_sent=3),
        }
        folded = fold_host_tail(window, 2)
        assert {host: d.request_count for (_p, _c, host), d in folded.items()} == {
            "e.example.com": 3, "d.example.com": 2, HOST_OVERFLOW: 3,
        }
        assert folded[("P", "c1", HOST_OVERFLOW)].bytes_sent == 3

    def test_bytes_break_a_tie_in_requests(self) -> None:
        window = {
            ("P", "c1", "small.example.com"): MetricDelta(request_count=1, bytes_received=10),
            ("P", "c1", "large.example.com"): MetricDelta(request_count=1, bytes_received=1000),
        }
        assert set(fold_host_tail(window, 1)) == {("P", "c1", "large.example.com"), ("P", "c1", HOST_OVERFLOW)}

    def test_the_ceiling_is_per_project_and_keeps_connectors_apart(self) -> None:
        one = MetricDelta(request_count=1, success_count=1)
        window = {
            # Project P: "big" is spread over two connectors (3 requests) and beats "small" (2 on one).
            ("P", "c1", "big.example.com"): MetricDelta(request_count=1, success_count=1),
            ("P", "c2", "big.example.com"): MetricDelta(request_count=2, success_count=2),
            ("P", "c1", "small.example.com"): MetricDelta(request_count=2, success_count=2),
            ("P", "c2", "tiny.example.com"): one,
            # Project Q is ranked on its own.
            ("Q", "c3", "only.example.com"): one,
        }
        folded = fold_host_tail(window, 1)
        assert {k: d.request_count for k, d in folded.items()} == {
            ("P", "c1", "big.example.com"): 1,
            ("P", "c2", "big.example.com"): 2,
            ("P", "c1", HOST_OVERFLOW): 2,
            ("P", "c2", HOST_OVERFLOW): 1,
            ("Q", "c3", "only.example.com"): 1,
        }

    def test_folding_never_writes_into_the_callers_deltas(self) -> None:
        kept = MetricDelta(request_count=5)
        window = {
            ("P", "c1", "kept.example.com"): kept,
            ("P", "c1", HOST_OVERFLOW): MetricDelta(request_count=1),
            ("P", "c1", "tail.example.com"): MetricDelta(request_count=1),
        }
        folded = fold_host_tail(window, 1)
        # The overflow row of an earlier fold is not a host, and takes in the new tail.
        assert folded[("P", "c1", HOST_OVERFLOW)].request_count == 2
        assert folded[("P", "c1", "kept.example.com")] is not kept
        assert window[("P", "c1", HOST_OVERFLOW)].request_count == 1
