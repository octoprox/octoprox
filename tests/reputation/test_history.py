# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A place's history as the Over time chart draws it: each dynamic connector's own factor at the finest level of the
place with one, rebuilt from the moves kept in the range (before the first, what it moved from; without one, the factor
now), its routed value under the rule in force at each moment (rebuilt backward from the project's settings through its
audit entries); each connector with IPs' share of its eligible rows whose exit IP was not avoided, rebuilt the same way
from the avoid moves and stepping where an avoid runs out; every move up to a day, hourly time-weighted means over a
week and three-hour ones over 30 days, nothing before the history began.

Pure computation over the rows a read hands it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from api.models.audit import AuditEntry
from api.models.project import ExitReputationMode, Project
from api.reputation.history import (
    ExactSteps,
    FactorHistory,
    HistoryPoint,
    HistoryRange,
    HourlyMeans,
    RuleInForce,
    ThreeHourMeans,
)
from api.reputation.record import ConnectorChange, ConnectorSiteKey, ExitIpKey, IpChange
from api.reputation.weight import ReputationKind, ReputationRule

SITE = "www.google.com"
CITY = "US/NY/new_york"
NOW = datetime(2026, 11, 25, 12, 0)
# Long before any range: every range is drawn whole.
LONG_AGO = NOW - timedelta(days=31)
PLAIN = ReputationRule(1, 0.1)
# 0.85 beside the best's 0.95 at strength 10: about 0.31.
STRONGER = 0.85 * (0.85 / 0.95) ** 9


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def move(
    connector_id: str,
    place: str,
    at: datetime,
    old: float | None,
    new: float | None,
    requests: int = 0,
    blocked: int = 0,
) -> ConnectorChange:
    return ConnectorChange(ConnectorSiteKey("p", connector_id, SITE, place), at, old, new, requests, blocked)


def project(mode: ExitReputationMode = ExitReputationMode.AVOID, strength: int = 1, floor: int = 10) -> Project:
    return Project(
        id="p",
        name="P",
        username="p",
        password="pw",
        exit_reputation=mode,
        exit_reputation_strength=strength,
        exit_reputation_floor=floor,
    )


def entry(at: datetime, **changes: list[Any]) -> AuditEntry:
    """The project's audit entry made at ``at`` changing ``changes`` (``[before, after]``)."""
    return AuditEntry(created_at=at, action="updated", entity_type="project", entity_id="p", changes=changes)


def history(
    range: HistoryRange = HistoryRange.DAY,
    *,
    place: str = CITY,
    connectors: Mapping[str, str] | None = None,
    moves: Sequence[ConnectorChange] = (),
    last_before: Sequence[ConnectorChange] = (),
    current: Mapping[tuple[str, str], float | None] | None = None,
    rules: Sequence[RuleInForce] | None = None,
    recorded_since: datetime | None = LONG_AGO,
    exits: Mapping[str, Sequence[str | None]] | None = None,
    avoid_moves: Sequence[IpChange] = (),
    avoid_ends: Mapping[str, datetime] | None = None,
) -> FactorHistory:
    since = NOW - range.span
    return FactorHistory.of(
        range,
        site=SITE,
        place=place,
        until=NOW,
        connectors=connectors if connectors is not None else {"a": "Alpha"},
        moves=moves,
        last_before=last_before,
        current=current or {},
        rules=rules if rules is not None else [RuleInForce(since, True, PLAIN)],
        recorded_since=recorded_since,
        exits=exits,
        avoid_moves=avoid_moves,
        avoid_ends=avoid_ends,
    )


def avoid(ip: str, at: datetime, old: datetime | None, new: datetime | None) -> IpChange:
    """A change of ``ip``'s avoid for the site, made at ``at``: its end before and after."""
    return IpChange(ExitIpKey("p", ip, SITE), at, old, new)


def points(found: FactorHistory, connector_id: str = "a") -> list[HistoryPoint]:
    [series] = [series for series in found.series() if series.connector_id == connector_id]
    return series.points


def own(found: FactorHistory, connector_id: str = "a") -> list[tuple[datetime, float | None, str | None]]:
    return [(point.at, point.own, point.level) for point in points(found, connector_id)]


class TestOwnFactor:
    def test_before_the_first_move_in_the_range_a_factor_is_what_it_moved_from(self) -> None:
        found = history(
            moves=[move("a", "US/NY", ago(120), 0.9, 0.8), move("a", "US/NY", ago(60), 0.8, 0.7)],
            # Now it holds the last move's factor.
            current={("a", "US/NY"): 0.7},
        )

        assert isinstance(found, ExactSteps) and found.bucket_seconds is None
        assert own(found) == [
            (ago(24 * 60), 0.9, "US/NY"),
            (ago(120), 0.8, "US/NY"),
            (ago(60), 0.7, "US/NY"),
        ]

    def test_without_a_move_in_the_range_a_factor_kept_the_one_it_has_now(self) -> None:
        found = history(current={("a", "US"): 0.85, ("a", CITY): None})

        # One point, which holds until now.
        assert own(found) == [(ago(24 * 60), 0.85, "US")]
        assert [point.routed for point in points(found)] == [0.85]

    def test_a_city_that_gains_a_factor_mid_range_takes_over_from_its_state(self) -> None:
        found = history(
            moves=[move("a", CITY, ago(90), None, 0.6), move("a", "US/NY", ago(30), 0.8, 0.75)],
            current={("a", CITY): 0.6, ("a", "US/NY"): 0.75, ("a", "US"): 0.9},
        )

        assert own(found) == [
            (ago(24 * 60), 0.8, "US/NY"),
            (ago(90), 0.6, CITY),
            # The state's move changes nothing the city holds: no point.
        ]

    def test_a_connector_not_judged_is_a_gap_and_routes_at_one(self) -> None:
        found = history(moves=[move("a", "US", ago(60), 0.5, None)], current={("a", "US"): None})

        assert [(point.own, point.level, point.routed) for point in points(found)] == [
            (0.5, "US", 0.5),
            (None, None, 1.0),
        ]

    def test_requests_and_blocked_are_counted_since_the_move_before(self) -> None:
        found = history(
            moves=[move("a", "US", ago(60), 0.9, 0.8, 160, 16), move("a", "US", ago(30), 0.8, 0.85, 200, 17)],
            # The last move before the range, which the first in it is told against.
            last_before=[move("a", "US", ago(26 * 60), 1.0, 0.9, 100, 10)],
        )

        assert [(point.at, point.requests, point.blocked) for point in points(found)] == [
            (ago(24 * 60), None, None),
            (ago(60), 60, 6),
            (ago(30), 40, 1),
        ]

    def test_a_first_move_with_none_before_it_counts_nothing(self) -> None:
        found = history(moves=[move("a", "US", ago(60), None, 0.8, 30, 6)])

        assert [(point.requests, point.blocked) for point in points(found)] == [(None, None)] * 2

    def test_a_coarser_level_s_move_counts_nothing_for_a_finer_own_factor(self) -> None:
        found = history(
            moves=[move("a", "US", ago(30), 0.9, 0.7, 500, 150)],
            last_before=[move("a", "US", ago(26 * 60), 1.0, 0.9)],
            current={("a", "US/NY"): 0.8, ("a", "US"): 0.7},
        )

        assert [(point.at, point.own, point.level, point.requests) for point in points(found)] == [
            (ago(24 * 60), 0.8, "US/NY", None)
        ]

    def test_a_finer_level_that_stops_judging_counts_for_the_coarser_value(self) -> None:
        found = history(
            moves=[move("a", "US/NY", ago(30), 0.8, None, 300, 30)],
            last_before=[move("a", "US/NY", ago(26 * 60), 0.9, 0.8, 100, 10)],
            current={("a", "US/NY"): None, ("a", "US"): 0.7},
        )

        assert [(point.at, point.own, point.level, point.requests, point.blocked) for point in points(found)] == [
            (ago(24 * 60), 0.8, "US/NY", None, None),
            (ago(30), 0.7, "US", 200, 20),
        ]


class TestRouted:
    """Routed values weigh every dynamic connector taking the requests under the rule in force at each moment."""

    CURRENT = {("a", "US/NY"): 0.85, ("b", "US/NY"): 0.95}
    CONNECTORS = {"a": "Alpha", "b": "Beta"}

    def test_a_strength_change_from_the_audit_entries(self) -> None:
        start = NOW - HistoryRange.SIX_HOURS.span
        rules = RuleInForce.rebuilt(
            project(strength=10, floor=10),
            [entry(ago(120), exit_reputation_strength=[1, 10], name=["Old", "P"])],
            start,
        )

        found = history(HistoryRange.SIX_HOURS, connectors=self.CONNECTORS, current=self.CURRENT, rules=rules)

        assert [(rule.at, rule.applied, rule.rule) for rule in rules] == [
            (start, True, ReputationRule(1, 0.1)),
            (ago(120), True, ReputationRule(10, 0.1)),
        ]
        assert [(point.at, point.routed) for point in points(found, "a")] == [
            (start, 0.85),
            (ago(120), round(STRONGER, 4)),
        ]
        # The best keeps its own factor, and its own line never moves.
        assert [(point.own, point.routed) for point in points(found, "b")] == [(0.95, 0.95)]

    def test_a_floor_change_rebuilt_newest_first(self) -> None:
        start = NOW - HistoryRange.SIX_HOURS.span
        rules = RuleInForce.rebuilt(
            project(strength=10, floor=40),
            [
                entry(ago(180), exit_reputation_strength=[1, 10]),
                entry(ago(60), exit_reputation_floor=[10, 40]),
            ],
            start,
        )

        found = history(HistoryRange.SIX_HOURS, connectors=self.CONNECTORS, current=self.CURRENT, rules=rules)

        assert [(rule.at, rule.rule) for rule in rules] == [
            (start, ReputationRule(1, 0.1)),
            (ago(180), ReputationRule(10, 0.1)),
            (ago(60), ReputationRule(10, 0.4)),
        ]
        assert [point.routed for point in points(found, "a")] == [0.85, round(STRONGER, 4), 0.4]

    def test_mode_off_gives_applied_false(self) -> None:
        start = NOW - HistoryRange.DAY.span
        rules = RuleInForce.rebuilt(
            project(ExitReputationMode.OFF),
            [
                entry(ago(300), exit_reputation=["avoid", "record"]),
                # Off records nothing either: no new rule.
                entry(ago(200), exit_reputation=["record", "off"]),
            ],
            start,
        )

        assert [(rule.at, rule.applied) for rule in rules] == [(start, True), (ago(300), False)]

    def test_a_change_before_the_range_or_of_another_setting_is_no_marker(self) -> None:
        start = NOW - HistoryRange.HOUR.span
        rules = RuleInForce.rebuilt(
            project(strength=5), [entry(ago(30), name=["Old", "P"], exit_reputation_strength=[5, 5])], start
        )

        assert [(rule.at, rule.applied, rule.rule) for rule in rules] == [(start, True, ReputationRule(5, 0.1))]


class TestBucketMeans:
    def test_a_week_is_a_point_per_hour_of_time_weighted_means(self) -> None:
        found = history(
            HistoryRange.WEEK,
            # Half past ten: half the hour from ten at 0.8, half at 0.6.
            moves=[move("a", "US", ago(90), 0.8, 0.6)],
            current={("a", "US"): 0.6},
        )

        assert isinstance(found, HourlyMeans) and found.bucket_seconds == 3600
        listed = points(found)
        assert len(listed) == 168
        assert listed[0].at == NOW - timedelta(days=7)
        by_hour = {point.at: (point.own, point.level, point.routed) for point in listed}
        assert by_hour[ago(180)] == (0.8, "US", 0.8)
        # Rounded to four places, as every value in an answer.
        assert by_hour[ago(120)] == (0.7, "US", 0.7)
        assert by_hour[ago(60)] == (0.6, "US", 0.6)

    def test_the_own_mean_counts_the_judged_time_alone(self) -> None:
        found = history(
            HistoryRange.WEEK,
            # Judged for the last quarter of the hour from ten.
            moves=[move("a", "US", ago(75), None, 0.6), move("a", "US", ago(30), 0.6, None)],
        )

        by_hour = {point.at: point for point in points(found)}
        # Routed at 0.6 for a quarter of the hour and at 1 for the rest.
        assert (by_hour[ago(120)].own, by_hour[ago(120)].routed) == (0.6, 0.9)
        assert by_hour[ago(180)].own is None
        # The last hour: half judged at 0.6, half not.
        assert (by_hour[ago(60)].own, by_hour[ago(60)].routed) == (0.6, 0.8)

    def test_30_days_are_a_point_per_three_hours_of_the_clock(self) -> None:
        found = history(
            HistoryRange.MONTH,
            # Half past ten: half the three hours from nine at 0.8, half at 0.6, with the move's counts.
            moves=[move("a", "US", ago(90), 0.8, 0.6, requests=130, blocked=40)],
            last_before=[move("a", "US", ago(2000), 0.9, 0.8, requests=100, blocked=30)],
            current={("a", "US"): 0.6},
        )

        assert isinstance(found, ThreeHourMeans) and found.bucket_seconds == 10800
        listed = points(found)
        assert len(listed) == 240
        assert listed[0].at == NOW - timedelta(days=30)
        by_bucket = {point.at: (point.own, point.routed, point.requests, point.blocked) for point in listed}
        assert by_bucket[ago(360)] == (0.8, 0.8, None, None)
        assert by_bucket[ago(180)] == (0.7, 0.7, 30, 10)

    def test_30_days_start_where_the_history_does_cut_to_the_next_mark(self) -> None:
        found = history(HistoryRange.MONTH, current={("a", "US"): 0.8}, recorded_since=ago(250))

        assert [point.at for point in points(found)] == [ago(250), ago(180)]


class TestRecordedSince:
    """Nothing is drawn before the history began."""

    def test_the_points_start_where_the_history_does(self) -> None:
        found = history(
            HistoryRange.HOUR,
            moves=[move("a", "US", ago(10), 0.9, 0.8)],
            current={("a", "US"): 0.8},
            recorded_since=ago(20),
        )

        assert (found.since, found.recorded_since) == (ago(60), ago(20))
        assert own(found) == [(ago(20), 0.9, "US"), (ago(10), 0.8, "US")]

    def test_a_week_s_hours_start_where_the_history_does(self) -> None:
        found = history(HistoryRange.WEEK, current={("a", "US"): 0.8}, recorded_since=ago(100))

        assert [point.at for point in points(found)] == [ago(100), ago(60)]

    def test_before_any_move_kept_the_range_is_drawn_whole_at_the_factors_now(self) -> None:
        found = history(HistoryRange.HOUR, current={("a", "US"): 0.8}, recorded_since=None)

        assert own(found) == [(ago(60), 0.8, "US")]


def usable(
    found: FactorHistory, connector_id: str = "i"
) -> list[tuple[datetime, float | None, int | None, float | None]]:
    """The connector with IPs' points as (at, usable, eligible, value), checking own and routed agree."""
    listed = points(found, connector_id)
    assert all(point.own == point.routed and point.level is None and point.requests is None for point in listed)
    return [(point.at, point.usable, point.eligible, point.own) for point in listed]


# A pool with IPs of four rows, one of which has no exit IP known.
POOL = {"i": "Pool"}
FOUR = {"i": ["198.51.100.1", "198.51.100.2", "198.51.100.3", None]}


class TestUsableIps:
    """A connector with IPs: of its rows eligible now, by the exit IPs they have now, the share not avoided."""

    def test_before_its_first_change_in_the_range_an_ip_is_as_that_change_found_it(self) -> None:
        # Avoided from before the range until half an hour ago, then ended early by good reports an hour ago.
        found = history(
            connectors=POOL, exits=FOUR, avoid_moves=[avoid("198.51.100.1", ago(60), ago(30), None)]
        )

        assert usable(found) == [(ago(24 * 60), 3, 4, 0.75), (ago(60), 4, 4, 1.0)]

    def test_without_a_change_in_the_range_the_record_now_decides_and_an_expiry_steps(self) -> None:
        # One avoid still ahead, one that ran out two hours ago, one that ran out before the range.
        found = history(
            connectors=POOL,
            exits=FOUR,
            avoid_ends={"198.51.100.1": ago(-30), "198.51.100.2": ago(120), "198.51.100.3": ago(25 * 60)},
        )

        assert usable(found) == [(ago(24 * 60), 2, 4, 0.5), (ago(120), 3, 4, 0.75)]

    def test_an_avoid_begun_in_the_range_steps_back_where_it_runs_out(self) -> None:
        found = history(
            connectors=POOL,
            exits=FOUR,
            avoid_moves=[
                avoid("198.51.100.2", ago(180), None, ago(120)),
                avoid("198.51.100.3", ago(30), None, ago(-30)),
            ],
            avoid_ends={"198.51.100.2": ago(120), "198.51.100.3": ago(-30)},
        )

        assert usable(found) == [
            (ago(24 * 60), 4, 4, 1.0),
            (ago(180), 3, 4, 0.75),
            (ago(120), 4, 4, 1.0),
            # Still ahead now: no step at the end of the range.
            (ago(30), 3, 4, 0.75),
        ]

    def test_rows_sharing_an_exit_ip_count_each_and_none_eligible_is_one(self) -> None:
        found = history(
            connectors={"i": "Pool", "j": "Spare"},
            exits={"i": ["198.51.100.1", "198.51.100.1", "198.51.100.2"], "j": []},
            avoid_ends={"198.51.100.1": ago(-30)},
        )

        assert usable(found) == [(ago(24 * 60), 1, 3, round(1 / 3, 4))]
        assert usable(found, "j") == [(ago(24 * 60), 0, 0, 1.0)]

    def test_a_week_is_an_hourly_time_weighted_mean_to_a_tenth_of_a_row(self) -> None:
        found = history(
            HistoryRange.WEEK,
            connectors=POOL,
            exits=FOUR,
            # Avoided for the last three quarters of the hour from ten, and the whole hour from nine.
            avoid_moves=[
                avoid("198.51.100.1", ago(240), None, ago(180)),
                avoid("198.51.100.1", ago(105), ago(180), ago(60)),
            ],
            avoid_ends={"198.51.100.1": ago(60)},
        )

        by_hour = {at: (rows, eligible, value) for at, rows, eligible, value in usable(found)}
        assert len(by_hour) == 168
        assert by_hour[ago(240)] == (3, 4, 0.75)
        assert by_hour[ago(180)] == (4, 4, 1.0)
        # 3.25 rows on average: to a tenth of a row, and the value's own mean.
        assert by_hour[ago(120)] == (3.2, 4, 0.8125)
        assert by_hour[ago(60)] == (4, 4, 1.0)

    def test_the_line_starts_where_the_history_does(self) -> None:
        found = history(
            HistoryRange.HOUR,
            connectors=POOL,
            exits=FOUR,
            avoid_moves=[avoid("198.51.100.1", ago(10), None, ago(-50))],
            recorded_since=ago(20),
        )

        assert usable(found) == [(ago(20), 4, 4, 1.0), (ago(10), 3, 4, 0.75)]


class TestMixedSeries:
    def test_dynamic_and_ip_lines_in_the_order_given_each_of_its_kind(self) -> None:
        """The read hands the connectors in name order; the connectors with IPs never weigh in the dynamic routing."""
        found = history(
            connectors={"a": "Alpha", "i": "Pool", "b": "Beta"},
            current=TestRouted.CURRENT,
            exits={"i": ["198.51.100.1", "198.51.100.2"]},
            avoid_moves=[avoid("198.51.100.1", ago(60), None, ago(-60))],
        )

        assert [(series.connector_id, series.kind) for series in found.series()] == [
            ("a", ReputationKind.BLOCK_RATE),
            ("i", ReputationKind.USABLE_IPS),
            ("b", ReputationKind.BLOCK_RATE),
        ]
        # The pool's step is no point of the dynamic lines.
        assert [(point.at, point.own, point.routed, point.usable) for point in points(found, "a")] == [
            (ago(24 * 60), 0.85, 0.85, None)
        ]
        assert usable(found) == [(ago(24 * 60), 2, 2, 1.0), (ago(60), 1, 2, 0.5)]
