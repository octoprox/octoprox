# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the in-flight counts least_used routes on."""

from api.routing.inflight import InFlight


class TestInFlight:
    def test_counts_per_proxy_and_per_connector(self) -> None:
        inflight = InFlight()
        inflight.begin("p1", "c1")
        inflight.begin("p1", "c1")
        inflight.begin("p2", "c1")
        inflight.begin("p3", "c2")
        assert (inflight.of_proxy("p1"), inflight.of_proxy("p2"), inflight.of_proxy("other")) == (2, 1, 0)
        assert (inflight.of_connector("c1"), inflight.of_connector("c2")) == (3, 1)
        assert inflight.total == 4

    def test_an_ended_transfer_is_no_longer_counted(self) -> None:
        inflight = InFlight()
        inflight.begin("p1", "c1")
        inflight.begin("p1", "c1")
        inflight.end("p1", "c1")
        assert (inflight.of_proxy("p1"), inflight.of_connector("c1"), inflight.total) == (1, 1, 1)
        inflight.end("p1", "c1")
        assert (inflight.of_proxy("p1"), inflight.of_connector("c1"), inflight.total) == (0, 0, 0)

    def test_an_end_with_nothing_open_never_goes_below_zero(self) -> None:
        inflight = InFlight()
        inflight.end("p1", "c1")
        inflight.begin("p1", "c1")
        assert (inflight.of_proxy("p1"), inflight.of_connector("c1")) == (1, 1)
