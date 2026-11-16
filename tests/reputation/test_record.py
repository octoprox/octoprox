# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The decisions records carry: an exit IP's avoid ("Avoid for" doubled per recent avoid, lifted by good reports) and a
connector's factor (one less its block rate, moved a whole step at a time), with the examples the design gives at the
defaults (Avoid for 60 minutes, Remember for 24 hours, Connector memory 100 requests, one fresh bad report avoids, half
the reports bad)."""

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
DEFAULTS = ReputationPolicy.of_settings(60, 24, 100, 1, 50)


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


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
    policy: ReputationPolicy = DEFAULTS,
) -> ConnectorChange:
    """A metrics flush's evidence for the record, applied at ``when``."""
    return r.apply_evidence(requests, blocked, good, when, policy)


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
        policy = ReputationPolicy.of_settings(60, 24, 100, 3, 50)
        r = ip_record()
        report(r, T0, bad=1, policy=policy)
        report(r, T0, bad=1, policy=policy)
        assert r.avoid_until is None
        # The third fresh report passes the threshold of 2.5 and begins the avoid.
        report(r, T0, bad=1, policy=policy)
        assert r.avoid_until is not None

    def test_a_report_from_a_little_earlier_still_counts_towards_the_setting(self) -> None:
        policy = ReputationPolicy.of_settings(60, 24, 100, 2, 50)
        r = ip_record()
        report(r, T0, bad=1, policy=policy)
        assert r.avoid_until is None
        # An hour later the first weighs about 0.97: with the second, past 1.5.
        report(r, at(1), bad=1, policy=policy)
        assert r.avoid_until is not None

    def test_an_avoid_lasts_at_most_seven_days(self) -> None:
        r = ip_record()
        r.weighted_avoids = 30.0
        report(r, T0, bad=1, policy=ReputationPolicy.of_settings(10_080, 24, 100, 1, 50))
        assert r.avoid_until == T0 + AVOID_CEILING
        assert ReputationPolicy.of_settings(1, 24, 100, 1, 50).avoid_length(1e9) == AVOID_CEILING

    def test_avoid_for_doubles_per_recent_avoid_rounded(self) -> None:
        policy = ReputationPolicy.of_settings(60, 24, 100, 1, 50)
        assert [policy.avoid_length(n) for n in (0, 0.4, 0.6, 1, 2.49, 3)] == [
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


class TestConnectorMemory:
    """A connector record's evidence counts half after "Connector memory" more requests, and half every hour."""

    def test_evidence_counts_half_after_that_many_more_requests(self) -> None:
        r = connector_record()
        flush(r, T0, requests=100, blocked=40)
        assert (r.weighted_requests, r.weighted_blocked, r.factor) == (100.0, 40.0, 0.6)

        flush(r, T0, requests=100)

        assert (r.weighted_requests, r.weighted_blocked) == (pytest.approx(150.0), pytest.approx(20.0))
        assert r.factor == 0.85

    def test_and_half_every_hour_with_no_request(self) -> None:
        r = connector_record()
        flush(r, T0, requests=100, blocked=40)

        flush(r, at(1), blocked=1)

        assert (r.weighted_requests, r.weighted_blocked) == (pytest.approx(50.0), pytest.approx(21.0))
        assert r.weighted_at == at(1)

    def test_requests_and_hours_add_up(self) -> None:
        r = connector_record()
        flush(r, T0, requests=100, blocked=40)

        flush(r, at(2), requests=100)

        # Half for the hundred requests, and a quarter for the two hours.
        assert (r.weighted_requests, r.weighted_blocked) == (pytest.approx(112.5), pytest.approx(5.0))

    def test_the_memory_is_the_projects(self) -> None:
        r = connector_record()
        flush(r, T0, requests=40, blocked=40)

        flush(r, T0, requests=20, policy=ReputationPolicy.of_settings(60, 24, 20, 1, 50))

        assert (r.weighted_requests, r.weighted_blocked) == (pytest.approx(40.0), pytest.approx(20.0))

    def test_a_flush_dated_before_the_clock_decays_by_its_requests_alone(self) -> None:
        r = connector_record()
        flush(r, at(1), requests=100, blocked=40)

        flush(r, T0, requests=100)

        assert (r.weighted_requests, r.weighted_blocked, r.weighted_at) == (pytest.approx(150.0), 20.0, at(1))

    def test_between_flushes_only_the_hours_decay_it_whatever_remember_for_says(self) -> None:
        """What the views show of a record a minute after its flush: its weights as that flush left them, an hour halving
        them."""
        r = connector_record()
        flush(r, T0, requests=80, blocked=20)
        week = ReputationPolicy.of_settings(60, 168, 100, 1, 50)

        assert r.decay_factor(at(1), DEFAULTS) == r.decay_factor(at(1), week) == 0.5
        assert r.decay_factor(at(-1), DEFAULTS) == 1.0
        # An exit IP's record decays by Remember for.
        assert ip_record().decay_factor(at(24), DEFAULTS) == 0.5

    def test_a_busy_connector_a_site_starts_blocking_falls_within_minutes(self) -> None:
        """A hundred requests a minute, a tenth of them blocked, then every one."""
        r = connector_record()
        for minute in range(120):
            flush(r, at(minute / 60), requests=100, blocked=10)
        assert r.factor == 0.9

        factors = []
        for minute in range(120, 123):
            flush(r, at(minute / 60), requests=100, blocked=100)
            factors.append(r.factor)

        assert factors == [0.45, 0.2, 0.1]


class TestConnectorFactor:
    def test_no_factor_below_twenty_weighted_requests(self) -> None:
        r = connector_record()
        change = flush(r, T0, requests=19, blocked=1)
        assert r.factor is None and change == ConnectorChange(CONNECTOR, None, None)

        change = flush(r, T0, requests=2)

        # The 19 count about 18.7 after two more requests: 20.7 weighted, about 0.99 of them blocked.
        assert r.weighted_requests == pytest.approx(19 * 2 ** -0.02 + 2)
        assert r.factor == 0.95 and change == ConnectorChange(CONNECTOR, None, 0.95)

    def test_the_factor_is_one_less_the_block_rate_to_the_nearest_step(self) -> None:
        r = connector_record()
        flush(r, T0, requests=100, blocked=13)
        # 0.87 is nearer 0.85 than 0.9.
        assert r.block_rate == pytest.approx(0.13) and r.factor == 0.85
        floored = connector_record()
        flush(floored, T0, requests=100, blocked=95)
        assert floored.factor == 0.1

    def test_good_reports_are_counted_but_leave_the_rate(self) -> None:
        r = connector_record()
        flush(r, T0, requests=40, blocked=10, good=4)
        change = flush(r, T0, good=3)
        assert (r.good_reports, r.blocked_reports, r.factor, change.moved) == (7, 10, 0.75, False)
        assert (r.weighted_requests, r.weighted_blocked) == (40.0, 10.0)

    def test_the_hourly_decay_keeps_the_rate(self) -> None:
        """Both weights halve together, so the factor a record keeps between flushes is the one its evidence made."""
        r = connector_record()
        flush(r, T0, requests=200, blocked=40)
        r.decay(at(2), DEFAULTS)
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
        assert r.factor == 0.85 and change == ConnectorChange(CONNECTOR, 0.9, 0.85)

    def test_it_comes_back_a_whole_step_too(self) -> None:
        r = weighed(connector_record(), 100.0, 11.0, 0.85)
        flush(r, T0, good=1)
        assert r.factor == 0.85
        r.weighted_blocked = 10.0
        flush(r, T0, good=1)
        assert r.factor == 0.9

    def test_from_a_step_above_the_floor_to_the_floor(self) -> None:
        """0.15 - 0.1 is a hair short of 0.05 in floating point; in steps it is one."""
        r = weighed(connector_record(), 100.0, 85.0, 0.15)

        flush(r, T0, blocked=10)

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

        flush(r, at(1), good=1)
        assert r.weighted_requests == pytest.approx(10.0) and r.factor is None

        # 20.3 weighted again, 4.6 of them blocked: decided as a new factor, with no step to clear.
        change = flush(r, at(1), requests=11)
        assert r.factor == 0.75 and change == ConnectorChange(CONNECTOR, None, 0.75)


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
            connectors=(ConnectorChange(CONNECTOR._replace(project_id="other"), 0.5, 0.5),),
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
            exit_reputation_bad_reports=3,
            exit_reputation_bad_share=25,
        )
        assert ReputationPolicy.of(project) == ReputationPolicy(
            half_life=12 * 3600.0, connector_half_life=50.0, threshold=2.5, bad_share=0.25, avoid_base=1800.0
        )
