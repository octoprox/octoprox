# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the request outcomes per proxy that ``health_based`` routes on."""

from api.routing.outcomes import LocalOutcomes


class TestLocalOutcomes:
    def test_records_requests_successes_and_latency_per_proxy(self) -> None:
        outcomes = LocalOutcomes()
        outcomes.record("p1", True, 100.0)
        outcomes.record("p1", False, 300.0)
        outcomes.record("p2", True, 50.0)
        assert outcomes.of_proxy("p1") == (2, 1, 400.0)
        assert outcomes.of_proxy("p2") == (1, 1, 50.0)

    def test_a_proxy_never_seen_has_nothing(self) -> None:
        outcomes = LocalOutcomes()
        outcomes.record("p1", True, 100.0)
        assert outcomes.of_proxy("never-used") == (0, 0, 0.0)

    def test_retain_forgets_proxies_that_left_the_snapshot(self) -> None:
        outcomes = LocalOutcomes()
        outcomes.record("kept", True, 10.0)
        outcomes.record("gone", True, 10.0)
        outcomes.retain(frozenset({"kept", "new"}))
        assert outcomes.of_proxy("kept") == (1, 1, 10.0)
        assert outcomes.of_proxy("gone") == (0, 0, 0.0)
        # Back in a later snapshot, a proxy starts again from nothing.
        outcomes.record("gone", False, 5.0)
        assert outcomes.of_proxy("gone") == (1, 0, 5.0)
