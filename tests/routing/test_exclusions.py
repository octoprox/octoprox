# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for a gateway's local exclusions: retired slots and its own quarantines, until the feed takes over."""

from datetime import datetime, timedelta

import pytest

from api.routing.exclusions import (
    LOCAL_QUARANTINE_HANDOVER_SECONDS,
    RETIRED_TTL_SECONDS,
    LocalExclusions,
)

START = datetime(2026, 10, 17, 12, 0, 0)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def exclusions(clock: Clock) -> LocalExclusions:
    return LocalExclusions(clock=clock)


class TestRetired:
    def test_a_retired_slot_stays_out_for_the_ttl(self, exclusions: LocalExclusions, clock: Clock) -> None:
        exclusions.retire("slot")
        assert exclusions.is_retired("slot") and not exclusions.is_retired("other")
        clock.advance(RETIRED_TTL_SECONDS - 1)
        assert exclusions.is_retired("slot")
        clock.advance(1)
        assert not exclusions.is_retired("slot")
        assert exclusions.retired_count == 0

    def test_retiring_again_starts_the_term_over(self, exclusions: LocalExclusions, clock: Clock) -> None:
        exclusions.retire("slot", ttl=10)
        clock.advance(8)
        exclusions.retire("slot", ttl=10)
        clock.advance(8)
        assert exclusions.is_retired("slot")


HANDOVER_END = START + timedelta(seconds=LOCAL_QUARANTINE_HANDOVER_SECONDS)


class TestQuarantined:
    def test_a_short_local_trip_holds_until_its_end(self, exclusions: LocalExclusions, clock: Clock) -> None:
        until = START + timedelta(seconds=LOCAL_QUARANTINE_HANDOVER_SECONDS / 2)
        exclusions.quarantine("px", until)
        assert exclusions.quarantined_until("px") == until and exclusions.quarantined_until("other") is None
        clock.advance(LOCAL_QUARANTINE_HANDOVER_SECONDS / 2)
        assert exclusions.quarantined_until("px") is None

    def test_a_long_local_trip_holds_only_until_the_feed_could_carry_it(
        self, exclusions: LocalExclusions, clock: Clock
    ) -> None:
        """Lifted before the controller saw it (an unquarantine, a rate-limit change), it is never in the feed."""
        exclusions.quarantine("px", START + timedelta(minutes=2))
        assert exclusions.quarantined_until("px") == HANDOVER_END
        clock.advance(LOCAL_QUARANTINE_HANDOVER_SECONDS)
        exclusions.reconcile({})
        assert exclusions.quarantined_until("px") is None

    def test_the_later_end_wins(self, exclusions: LocalExclusions) -> None:
        exclusions.quarantine("px", START + timedelta(seconds=LOCAL_QUARANTINE_HANDOVER_SECONDS / 2))
        exclusions.quarantine("px", START + timedelta(seconds=1))
        assert exclusions.quarantined_until("px") == START + timedelta(seconds=LOCAL_QUARANTINE_HANDOVER_SECONDS / 2)


class TestReconcile:
    def test_the_feed_takes_over_a_quarantine_it_carries(self, exclusions: LocalExclusions) -> None:
        exclusions.quarantine("px", START + timedelta(seconds=60))
        exclusions.quarantine("other", START + timedelta(seconds=60))
        exclusions.reconcile({"px": START + timedelta(seconds=59)})
        # From now on the section decides: an api unquarantine that drops it frees the proxy here too.
        assert exclusions.quarantined_until("px") is None
        assert exclusions.quarantined_until("other") is not None

    def test_an_ended_quarantine_in_the_feed_hands_nothing_over(self, exclusions: LocalExclusions) -> None:
        """The proxy tripped again right after its last quarantine ended, before the controller dropped that one."""
        exclusions.quarantine("px", START + timedelta(seconds=60))
        exclusions.reconcile({"px": START - timedelta(seconds=1)})
        assert exclusions.quarantined_until("px") == HANDOVER_END

    def test_expired_entries_are_forgotten(self, exclusions: LocalExclusions, clock: Clock) -> None:
        exclusions.retire("slot", ttl=5)
        exclusions.quarantine("px", START + timedelta(seconds=5))
        exclusions.retire("kept", ttl=50)
        assert (exclusions.retired_count, exclusions.quarantined_count) == (2, 1)
        clock.advance(10)
        exclusions.reconcile({})
        assert (exclusions.retired_count, exclusions.quarantined_count) == (1, 0)
        assert exclusions.is_retired("kept")
