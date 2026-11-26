# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The decisions records carry: an exit IP's avoid ("Avoid for" doubled per recent avoid, lifted by good reports) and a
connector's factor (one less its block rate in steps of 0.05 / Strength, moved a whole step at a time), with the examples the design gives at the
defaults (Avoid for 60 minutes, Remember for 24 hours, Connector memory 100 requests and 10 minutes, one fresh bad report
avoids, half the reports bad)."""

from datetime import datetime, timedelta

import pytest

from api.models.project import ExitReputationMode, Project
from api.reputation.record import (
    AVOID_CEILING,
    MAX_COUNT,
    MAX_REASON_LENGTH,
    ConnectorChange,
    ConnectorSiteKey,
    ConnectorSiteReputation,
    ExitIpKey,
    ExitIpReputation,
    IpChange,
    ReportCounts,
    ReputationChanges,
    ReputationPolicy,
    ReputationStatus,
    place_levels,
)

T0 = datetime(2026, 11, 15, 12, 0, 0)
KEY = ExitIpKey("project-1", "203.0.113.7", "google.com")
CONNECTOR = ConnectorSiteKey("project-1", "connector-1", "google.com", "US")
# A project's default settings.
SETTINGS = {
    "avoid_minutes": 60,
    "half_life_hours": 24,
    "connector_half_life_requests": 100,
    "connector_half_life_minutes": 10,
    "bad_reports": 1,
    "bad_share": 50,
}


def policy(**changed: int) -> ReputationPolicy:
    """The policy of the default settings with the ``changed`` ones."""
    return ReputationPolicy.of_settings(**{**SETTINGS, **changed})


DEFAULTS = policy()
STRENGTH_10 = policy(strength=10)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


def at_minute(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def hours_until(end: datetime | None, since: datetime) -> float | None:
    """How many hours past ``since`` an end is, to the hundredth; None for no end."""
    return None if end is None else round((end - since).total_seconds() / 3600, 2)


def ip_record() -> ExitIpReputation:
    return ExitIpReputation.new(KEY, T0)


def report(
    r: ExitIpReputation, when: datetime, *, good: int = 0, bad: int = 0, policy: ReputationPolicy = DEFAULTS
) -> IpChange:
    return r.apply(ReportCounts(good=good, bad=bad), when, policy)


def connector_record() -> ConnectorSiteReputation:
    return ConnectorSiteReputation.new(CONNECTOR, T0)


def flush(
    r: ConnectorSiteReputation,
    when: datetime,
    *,
    requests: int = 0,
    blocked: int = 0,
    good: int = 0,
    window: timedelta = timedelta(minutes=1),
    policy: ReputationPolicy = DEFAULTS,
) -> ConnectorChange:
    """A metrics flush's evidence for the record, counted over the ``window`` before ``when`` and applied then."""
    return r.apply_evidence(requests, blocked, good, when, window, policy)


def weighed(r: ConnectorSiteReputation, requests: float, blocked: float, factor: float | None) -> ConnectorSiteReputation:
    """``r`` holding ``requests`` and ``blocked`` weighted, and ``factor`` stored."""
    r.weighted_requests, r.weighted_blocked, r.factor = requests, blocked, factor
    return r


class TestDesignExamples:
    """The examples of the design, at "Avoid for" 60 minutes and "Remember for" 24 hours."""

    def test_one_bad_report_avoids_for_an_hour(self) -> None:
        r = ip_record()
        report(r, T0, bad=1)
        assert hours_until(r.avoid_until, T0) == 1.0
        assert r.weighted_avoids == 1.0

    def test_reported_again_three_hours_later_avoids_for_two(self) -> None:
        """The first avoid ended; the decayed count of earlier avoids, about 0.92, rounds to one doubling."""
        r = ip_record()
        report(r, T0, bad=1)
        report(r, at(3), bad=1)
        assert hours_until(r.avoid_until, at(3)) == 2.0
        assert r.weighted_avoids == pytest.approx(2 ** (-3 / 24) + 1)

    def test_a_burst_of_eight_reports_at_once_avoids_for_an_hour(self) -> None:
        one_batch = ip_record()
        report(one_batch, T0, bad=8)
        assert hours_until(one_batch.avoid_until, T0) == 1.0
        # Sent one by one in the same moment: the first begins the avoid and the others add weight to it.
        one_by_one = ip_record()
        for _ in range(8):
            report(one_by_one, T0, bad=1)
        assert hours_until(one_by_one.avoid_until, T0) == 1.0
        assert (one_batch.weighted_avoids, one_by_one.weighted_avoids) == (1.0, 1.0)

    def test_three_episodes_in_a_day_last_one_two_and_four_hours(self) -> None:
        r = ip_record()
        lengths = []
        for hours in (0, 3, 6):
            report(r, at(hours), bad=1)
            lengths.append(hours_until(r.avoid_until, at(hours)))
        assert lengths == [1.0, 2.0, 4.0]


class TestAvoid:
    def test_a_running_avoid_is_never_lengthened_by_more_bad_reports(self) -> None:
        r = ip_record()
        report(r, T0, bad=1)
        end = r.avoid_until
        change = report(r, at(0.5), bad=5)
        assert r.avoid_until == end and not change.moved
        assert r.weighted_bad == pytest.approx(2 ** (-0.5 / 24) + 5)
        # No new avoid began: the one avoid only decays.
        assert r.weighted_avoids == pytest.approx(2 ** (-0.5 / 24))

    def test_good_reports_lift_an_avoid_once_the_bad_share_falls_under_the_projects(self) -> None:
        r = ip_record()
        report(r, T0, bad=1)
        # One good report leaves the share at half, which still avoids.
        report(r, T0, good=1)
        assert r.avoid_until is not None and r.status(T0) is ReputationStatus.AVOIDED
        change = report(r, T0, good=1)
        assert r.avoid_until is None and change.moved
        assert r.bad_share == pytest.approx(1 / 3)

    def test_good_reports_never_begin_or_lengthen_one(self) -> None:
        r = ip_record()
        report(r, T0, good=3)
        assert r.avoid_until is None
        report(r, at(0.1), bad=10)
        end = r.avoid_until
        report(r, at(0.2), good=1)
        assert r.avoid_until == end

    def test_a_site_whose_reports_are_mostly_good_is_not_avoided(self) -> None:
        r = ip_record()
        report(r, T0, good=2)
        report(r, at(0.05), bad=1)
        assert r.avoid_until is None
        # A second bad report brings the share to a half.
        report(r, at(0.1), bad=1)
        assert hours_until(r.avoid_until, at(0.1)) == 1.0

    def test_more_bad_reports_to_avoid_need_that_many_fresh_ones(self) -> None:
        three = policy(bad_reports=3)
        r = ip_record()
        report(r, T0, bad=1, policy=three)
        report(r, T0, bad=1, policy=three)
        assert r.avoid_until is None
        # The third fresh report passes the threshold of 2.5 and begins the avoid.
        report(r, T0, bad=1, policy=three)
        assert r.avoid_until is not None

    def test_a_report_from_a_little_earlier_still_counts_towards_the_setting(self) -> None:
        two = policy(bad_reports=2)
        r = ip_record()
        report(r, T0, bad=1, policy=two)
        assert r.avoid_until is None
        # An hour later the first weighs about 0.97: with the second, past 1.5.
        report(r, at(1), bad=1, policy=two)
        assert r.avoid_until is not None

    def test_an_avoid_lasts_at_most_seven_days(self) -> None:
        r = ip_record()
        r.weighted_avoids = 30.0
        report(r, T0, bad=1, policy=policy(avoid_minutes=10_080))
        assert r.avoid_until == T0 + AVOID_CEILING
        assert policy(avoid_minutes=1).avoid_length(1e9) == AVOID_CEILING

    def test_avoid_for_doubles_per_recent_avoid_rounded(self) -> None:
        assert [DEFAULTS.avoid_length(n) for n in (0, 0.4, 0.6, 1, 2.49, 3)] == [
            timedelta(hours=hours) for hours in (1, 1, 2, 2, 4, 8)
        ]

    def test_the_end_does_not_move_with_decay(self) -> None:
        """Ends are times: a record read days later says the same, while its weights fall."""
        r = ip_record()
        report(r, T0, bad=4)
        end = r.avoid_until
        r.decay(at(48), DEFAULTS)
        assert r.avoid_until == end
        assert r.weighted_bad == pytest.approx(1.0)
        assert r.weighted_avoids == pytest.approx(0.25)

    def test_the_clock_never_goes_back(self) -> None:
        r = ip_record()
        report(r, at(2), bad=1)
        report(r, at(1), good=1)
        assert r.weighted_at == at(2)
        assert (r.weighted_good, r.weighted_bad) == (1.0, 1.0)

    def test_status_and_avoided_until_follow_the_end(self) -> None:
        r = ip_record()
        assert r.status(T0) is ReputationStatus.NONE and r.avoided_until(T0) is None
        report(r, T0, bad=1)
        assert r.status(at(0.5)) is ReputationStatus.AVOIDED and r.avoided_until(at(0.5)) == at(1)
        assert r.status(at(1)) is ReputationStatus.NONE and r.avoided_until(at(1)) is None


class TestIpBookkeeping:
    def test_the_latest_report_keeps_its_reason_and_time(self) -> None:
        r = ip_record()
        r.apply(ReportCounts(bad=1, reason="captcha"), at(2), DEFAULTS)
        r.apply(ReportCounts(good=1, reason="ok"), at(1), DEFAULTS)
        r.apply(ReportCounts(good=1), at(3), DEFAULTS)
        assert (r.last_report_at, r.last_reason) == (at(3), "captcha")
        r.apply(ReportCounts(bad=1, reason="x" * 100), at(4), DEFAULTS)
        assert r.last_reason == "x" * MAX_REASON_LENGTH

    def test_lifetime_counts_stop_at_their_columns_limit(self) -> None:
        r = ip_record()
        r.good_reports = MAX_COUNT - 1
        report(r, T0, good=5)
        assert r.good_reports == MAX_COUNT

    def test_the_day_follows_the_last_sighting(self) -> None:
        r = ip_record()
        report(r, at(13), bad=1)
        assert (r.last_seen_at, r.last_seen_day, r.first_seen_at) == (at(13), at(13).date(), T0)
        assert r.last_seen_day != T0.date()

    def test_a_change_counts_a_live_end_only(self) -> None:
        began = IpChange(KEY, T0, None, at(1))
        expired_then_none = IpChange(KEY, T0, at(-1), None)
        same = IpChange(KEY, T0, at(1), at(1))
        assert (began.moved, expired_then_none.moved, same.moved) == (True, False, False)

    def test_a_batch_stamped_before_one_applied_first_moves_the_avoid_after_it(self) -> None:
        """Two ingest workers can apply their batches in the other order than they took their times."""
        r = ip_record()
        began = report(r, at(0.5), bad=1)
        lifted = report(r, at(0.25), good=5)

        assert began == IpChange(KEY, at(0.5), None, at(1.5))
        assert lifted == IpChange(KEY, at(0.5) + timedelta(microseconds=1), at(1.5), None)


class TestConnectorMemory:
    """A connector record's evidence counts half after "Connector memory" in minutes or in requests, whichever is
    longer, and never later than "Remember for"."""

    def test_a_quiet_record_counts_half_after_that_many_more_requests(self) -> None:
        """A hundred requests over half an hour: they take longer than the ten minutes."""
        r = connector_record()
        flush(r, T0, requests=100, blocked=40)
        assert (r.weighted_requests, r.weighted_blocked, r.factor) == (100.0, 40.0, 0.6)

        flush(r, at(0.5), requests=100, window=timedelta(minutes=30))

        assert (r.weighted_requests, r.weighted_blocked) == (pytest.approx(150.0), pytest.approx(20.0))
        assert r.factor == 0.85

    def test_a_busy_record_counts_half_after_that_many_minutes(self) -> None:
        """A thousand requests in ten minutes: ten times the hundred, half all the same."""
        r = connector_record()
        flush(r, T0, requests=100, blocked=40)

        flush(r, at_minute(10), requests=1000, window=timedelta(minutes=10))

        assert (r.weighted_requests, r.weighted_blocked) == (pytest.approx(1050.0), pytest.approx(20.0))

    def test_a_flush_a_minute_fades_a_quiet_record_by_requests_and_a_busy_one_by_minutes(self) -> None:
        """A flush a minute: one request each fades by its requests, a thousand each by their minute."""
        quiet, busy = connector_record(), connector_record()
        flush(quiet, T0, requests=100, blocked=40)
        flush(busy, T0, requests=1000, blocked=400)

        for flushed in range(1, 101):
            flush(quiet, at_minute(flushed), requests=1)
        for flushed in range(1, 11):
            flush(busy, at_minute(flushed), requests=1000)

        assert (quiet.weighted_blocked, busy.weighted_blocked) == (pytest.approx(20.0), pytest.approx(200.0))

    def test_a_quiet_gap_before_a_burst_counts_no_minutes_however_full_its_first_flush(self) -> None:
        """Half an hour without requests, then a flush of 7,000 or of 70: each is one minute of traffic, a tenth of
        the ten minutes, and the gap fades by "Remember for" alone, which a tenth outweighs."""
        full, sparse = connector_record(), connector_record()
        for r in (full, sparse):
            flush(r, T0, requests=1000, blocked=400)

        flush(full, at_minute(31), requests=7000)
        flush(sparse, at_minute(31), requests=70)

        assert full.weighted_blocked == sparse.weighted_blocked == pytest.approx(400 * 2**-0.1)

    def test_a_stretch_with_no_requests_fades_by_remember_for_alone(self) -> None:
        r = connector_record()
        flush(r, T0, requests=100, blocked=40)

        flush(r, at(24), blocked=1)

        assert (r.weighted_requests, r.weighted_blocked) == (pytest.approx(50.0), pytest.approx(21.0))
        assert r.weighted_at == at(24)

    def test_the_designs_places_busy_quiet_and_sparse(self) -> None:
        """The design's places at 10 minutes, 1,000 requests and a day of Remember for."""
        places = policy(connector_half_life_requests=1000)
        # The UK at 7,000 requests a minute: half after 10 minutes, some 70,000 requests.
        assert places.connector_decay(600, 70_000, 600) == pytest.approx(0.5)
        # Lithuania at 1.4 a minute: half after its thousand requests, about 12 hours.
        assert places.connector_decay(1000 / 1.4 * 60, 1000, 1000 / 1.4 * 60) == pytest.approx(0.5)
        # 50 requests a day: half after the day, though its thousand requests take 20 days.
        assert places.connector_decay(24 * 3600, 50, 24 * 3600) == pytest.approx(0.5)

    def test_remember_for_wins_over_longer_minutes(self) -> None:
        """At a day of minutes and an hour of Remember for, an hour halves a busy record."""
        hour = policy(connector_half_life_minutes=1440, half_life_hours=1)
        r = connector_record()
        flush(r, T0, requests=100, blocked=40, policy=hour)

        flush(r, at(1), requests=10_000, policy=hour)

        assert (r.weighted_requests, r.weighted_blocked) == (pytest.approx(10_050.0), pytest.approx(20.0))

    def test_the_memory_is_the_projects(self) -> None:
        r = connector_record()
        flush(r, T0, requests=40, blocked=40)

        flush(r, at(1), requests=20, window=timedelta(hours=1), policy=policy(connector_half_life_requests=20))

        assert (r.weighted_requests, r.weighted_blocked) == (pytest.approx(40.0), pytest.approx(20.0))

        flush(r, at(1.5), requests=1000, window=timedelta(minutes=30), policy=policy(connector_half_life_minutes=60))

        assert r.weighted_blocked == pytest.approx(20.0 * 2**-0.5)

    def test_a_flush_dated_before_the_clock_fades_nothing(self) -> None:
        """No time has passed for it, so the minutes have not, whatever its requests."""
        r = connector_record()
        flush(r, at(1), requests=100, blocked=40)

        flush(r, T0, requests=100)

        assert (r.weighted_requests, r.weighted_blocked, r.weighted_at) == (200.0, 40.0, at(1))

    def test_between_flushes_only_remember_for_decays_it(self) -> None:
        """What the views show of a record a minute after its flush: its weights as that flush left them, "Remember for"
        halving them, whatever "Connector memory" says."""
        r = connector_record()
        flush(r, T0, requests=80, blocked=20)
        week = policy(half_life_hours=168)

        assert r.decay_factor(at(24), DEFAULTS) == r.decay_factor(at(168), week) == 0.5
        assert r.decay_factor(at(24), policy(connector_half_life_minutes=1, connector_half_life_requests=20)) == 0.5
        assert r.decay_factor(at(1), DEFAULTS) == pytest.approx(2 ** (-1 / 24))
        assert r.decay_factor(at(-1), DEFAULTS) == 1.0
        # An exit IP's record decays by Remember for too.
        assert ip_record().decay_factor(at(24), DEFAULTS) == 0.5

    def test_a_busy_connector_a_site_starts_blocking_falls_over_its_minutes(self) -> None:
        """A hundred requests a minute, a tenth of them blocked, then every one: a step a minute, and half the way down
        after the ten minutes. Remembering its hundred requests alone, it fell in three."""
        r, brief = connector_record(), connector_record()
        one_minute = policy(connector_half_life_minutes=1)
        for flushed in range(120):
            flush(r, at_minute(flushed), requests=100, blocked=10)
            flush(brief, at_minute(flushed), requests=100, blocked=10, policy=one_minute)
        assert r.factor == brief.factor == 0.9

        factors, brief_factors = [], []
        for flushed in range(120, 130):
            flush(r, at_minute(flushed), requests=100, blocked=100)
            flush(brief, at_minute(flushed), requests=100, blocked=100, policy=one_minute)
            factors.append(r.factor)
            brief_factors.append(brief.factor)

        assert factors[:3] == [0.85, 0.8, 0.75]
        assert r.block_rate == pytest.approx(0.55, abs=0.001) and r.factor == 0.45
        assert brief_factors[:3] == [0.45, 0.2, 0.1]


class TestConnectorFactor:
    def test_no_factor_below_twenty_weighted_requests(self) -> None:
        r = connector_record()
        change = flush(r, T0, requests=19, blocked=1)
        assert r.factor is None and change == ConnectorChange(CONNECTOR, T0, None, None, 19, 1)

        change = flush(r, T0, requests=2)

        # The 19 count in full in the same moment: 21 weighted, one of them blocked.
        assert r.weighted_requests == 21.0
        assert r.factor == 0.95 and change == ConnectorChange(CONNECTOR, T0, None, 0.95, 21, 1)

    def test_a_change_carries_the_flushs_time_and_the_records_lifetime_counts(self) -> None:
        """What the factor moves are kept with: the flush's decision time and the counts once it decided."""
        r = connector_record()
        flush(r, T0, requests=30, blocked=3, good=2)

        change = flush(r, at(0.5), requests=10, blocked=1)
        assert change == ConnectorChange(CONNECTOR, at(0.5), 0.9, 0.9, 40, 4) and not change.moved
        # A flush with nothing for the record decides nothing, and says so as of its time.
        assert flush(r, at(3)) == ConnectorChange(CONNECTOR, at(3), 0.9, 0.9, 40, 4)

    def test_the_factor_is_one_less_the_block_rate_to_the_nearest_step(self) -> None:
        r = connector_record()
        flush(r, T0, requests=100, blocked=13)
        # 0.87 is nearer 0.85 than 0.9.
        assert r.block_rate == pytest.approx(0.13) and r.factor == 0.85
        # No floor is stored: routing's lowest factor is the project's, applied as it routes.
        low = connector_record()
        flush(low, T0, requests=100, blocked=95)
        assert low.factor == 0.05

    def test_a_factor_is_at_least_one_step_so_never_zero(self) -> None:
        r = connector_record()
        flush(r, T0, requests=100, blocked=100)
        assert r.factor == 0.05
        strong = connector_record()
        flush(strong, T0, requests=100, blocked=100, policy=STRENGTH_10)
        assert strong.factor == 0.005

    def test_good_reports_are_counted_but_leave_the_rate(self) -> None:
        r = connector_record()
        flush(r, T0, requests=40, blocked=10, good=4)
        change = flush(r, T0, good=3)
        assert (r.good_reports, r.blocked_reports, r.factor, change.moved) == (7, 10, 0.75, False)
        assert (r.weighted_requests, r.weighted_blocked) == (40.0, 10.0)

    def test_the_decay_between_flushes_keeps_the_rate(self) -> None:
        """Both weights halve together, so the factor a record keeps between flushes is the one its evidence made."""
        r = connector_record()
        flush(r, T0, requests=200, blocked=40)
        r.decay(at(48), DEFAULTS)
        assert (r.weighted_requests, r.weighted_blocked) == (pytest.approx(50.0), pytest.approx(10.0))
        assert r.block_rate == pytest.approx(0.2) and r.factor == 0.8

    def test_counts_add_up_for_the_records_life(self) -> None:
        r = connector_record()
        flush(r, T0, requests=30, blocked=2)
        flush(r, at(24), requests=30, good=1)
        assert (r.requests, r.blocked_reports, r.good_reports) == (60, 2, 1)
        assert (r.last_seen_at, r.last_seen_day, r.first_seen_at) == (at(24), at(24).date(), T0)
        r.blocked_reports = MAX_COUNT - 1
        flush(r, at(25), blocked=5)
        assert r.blocked_reports == MAX_COUNT

    def test_a_flush_with_no_evidence_changes_nothing(self) -> None:
        r = connector_record()
        flush(r, T0, requests=40, blocked=10)

        change = flush(r, at(5))

        assert not change.moved
        assert (r.weighted_requests, r.weighted_at, r.last_seen_at) == (40.0, T0, T0)


class TestHysteresis:
    """A stored factor moves only once the rate is a whole step (0.05) from it, counted in steps."""

    def test_the_factor_stays_until_the_rate_is_a_whole_step_from_it(self) -> None:
        r = weighed(connector_record(), 100.0, 10.0, 0.9)

        # 0.86 rounds to 0.85, yet is less than a step from 0.9.
        change = flush(r, T0, blocked=4)
        assert r.factor == 0.9 and not change.moved

        change = flush(r, T0, blocked=1)
        assert r.factor == 0.85 and change == ConnectorChange(CONNECTOR, T0, 0.9, 0.85, 0, 5)

    def test_it_comes_back_a_whole_step_too(self) -> None:
        r = weighed(connector_record(), 100.0, 11.0, 0.85)
        flush(r, T0, good=1)
        assert r.factor == 0.85
        r.weighted_blocked = 10.0
        flush(r, T0, good=1)
        assert r.factor == 0.9

    def test_a_step_short_of_a_hair_in_floating_point_is_one_in_steps(self) -> None:
        """0.15 - 0.1 is a hair short of 0.05 in floating point; in steps it is one."""
        r = weighed(connector_record(), 100.0, 85.0, 0.15)

        flush(r, T0, blocked=5)

        assert r.factor == 0.1

    def test_down_to_one_step_and_back(self) -> None:
        r = weighed(connector_record(), 100.0, 85.0, 0.15)
        flush(r, T0, blocked=13)
        assert r.factor == 0.05
        # 0.08 is less than a step from 0.05.
        r.weighted_blocked = 92.0
        flush(r, T0, good=1)
        assert r.factor == 0.05
        r.weighted_blocked = 90.0
        flush(r, T0, good=1)
        assert r.factor == 0.1

    def test_a_rate_a_hair_short_of_a_whole_step_moves_it(self) -> None:
        """1 - 17 / 20 is a hair over 0.15: three steps and a hair, still a whole step from 0.2."""
        r = weighed(connector_record(), 20.0, 16.0, 0.2)

        flush(r, T0, blocked=1)

        assert r.factor == 0.15

    def test_too_few_requests_drop_the_factor_and_enough_decide_one_afresh(self) -> None:
        r = connector_record()
        flush(r, T0, requests=20, blocked=10)
        assert r.factor == 0.5

        flush(r, at(24), good=1)
        assert r.weighted_requests == pytest.approx(10.0) and r.factor is None

        # 21 weighted again, 5 of them blocked: decided as a new factor, with no step to clear.
        change = flush(r, at(24), requests=11)
        assert r.factor == 0.75 and change == ConnectorChange(CONNECTOR, at(24), None, 0.75, 31, 10)


class TestStrength:
    """At strength 10 a factor is stored in steps of 0.005, and moves a whole one of them at a time."""

    def test_the_step_is_0_05_over_the_strength(self) -> None:
        assert (DEFAULTS.factor_steps, STRENGTH_10.factor_steps) == (20, 200)
        r = connector_record()
        flush(r, T0, requests=100, blocked=13, policy=STRENGTH_10)
        assert r.factor == 0.87
        busy = connector_record()
        # Blocked 1.3% of the time: 0.987 is nearer 0.985 than 0.99.
        flush(busy, T0, requests=1000, blocked=13, policy=STRENGTH_10)
        assert busy.factor == 0.985

    def test_the_factor_stays_until_the_rate_is_a_whole_step_of_its_grid_from_it(self) -> None:
        r = weighed(connector_record(), 1000.0, 130.0, 0.87)

        # 0.866 is less than a step (0.005) from 0.87.
        change = flush(r, T0, blocked=4, policy=STRENGTH_10)
        assert r.factor == 0.87 and not change.moved

        change = flush(r, T0, blocked=1, policy=STRENGTH_10)
        assert r.factor == 0.865 and change == ConnectorChange(CONNECTOR, T0, 0.87, 0.865, 0, 5)

    def test_a_factor_stored_at_another_strength_is_read_on_the_new_grid(self) -> None:
        # 0.85 from strength 1, the rate 0.87: four steps of 0.005 away.
        r = weighed(connector_record(), 100.0, 13.0, 0.85)
        flush(r, T0, good=1, policy=STRENGTH_10)
        assert r.factor == 0.87
        # Back at strength 1, 0.87 is less than a step of 0.05 from 0.85's 17 steps.
        flush(r, T0, good=1)
        assert r.factor == 0.87
        r.weighted_blocked = 6.0
        flush(r, T0, good=1)
        assert r.factor == 0.95


class TestPlaces:
    @pytest.mark.parametrize(
        ("place", "levels"),
        [
            ("US/NY/new_york", ("US/NY/new_york", "US/NY", "US")),
            ("US//austin", ("US//austin", "US")),
            ("US/NY", ("US/NY", "US")),
            ("US", ("US",)),
            ("", ("",)),
        ],
    )
    def test_a_places_levels_are_finest_first_and_none_is_its_own(self, place: str, levels: tuple[str, ...]) -> None:
        assert place_levels(place) == levels
        assert [key.place for key in CONNECTOR._replace(place=place).levels()] == list(levels)


class TestBatches:
    def test_counts_fold_per_key_with_the_latest_reason(self) -> None:
        other = KEY._replace(site="bing.com")
        folded = ReportCounts.fold(
            [
                (KEY, ReportCounts(bad=1, reason="captcha")),
                (other, ReportCounts(good=1)),
                (KEY, ReportCounts(good=1)),
                (KEY, ReportCounts(bad=1, reason="403")),
            ]
        )
        assert folded == {KEY: ReportCounts(good=1, bad=2, reason="403"), other: ReportCounts(good=1)}

    def test_only_projects_that_avoid_are_named_as_moved(self) -> None:
        recording = ExitIpKey("recording", "203.0.113.7", "google.com")
        changes = ReputationChanges(
            ips=(IpChange(KEY, T0, None, at(1)), IpChange(recording, T0, None, at(1))),
            connectors=(ConnectorChange(CONNECTOR._replace(project_id="other"), T0, 0.5, 0.5, 0, 0),),
            avoiding=frozenset({"project-1", "other"}),
        )
        assert changes.moved_projects == ["project-1"]

    def test_a_projects_settings_make_its_policy(self) -> None:
        project = Project(
            id="p",
            name="P",
            username="u",
            password="p",
            exit_reputation=ExitReputationMode.AVOID,
            exit_reputation_avoid_minutes=30,
            exit_reputation_half_life_hours=12,
            exit_reputation_connector_half_life_requests=50,
            exit_reputation_connector_half_life_minutes=30,
            exit_reputation_bad_reports=3,
            exit_reputation_bad_share=25,
            exit_reputation_strength=10,
        )
        assert ReputationPolicy.of(project) == ReputationPolicy(
            half_life=12 * 3600.0,
            connector_half_life=50.0,
            connector_half_life_time=1800.0,
            threshold=2.5,
            bad_share=0.25,
            avoid_base=1800.0,
            strength=10,
        )
