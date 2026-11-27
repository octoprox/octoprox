# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""How a site's connectors moved at one requested place over a range of time, as the Over time chart of the Reputation
tab draws them: a dynamic connector's own factor and the value routing multiplies its weight by, and a connector with
IPs' share of its eligible rows whose exit IP the site did not avoid.

The metrics flush keeps every move of a dynamic connector's stored factor, with the record's lifetime counts once it
decided (``ExitReputationRepository.factor_moves``). A connector's own value at a moment is its factor at the finest
level of the place that has one, as routing reads it (:meth:`~api.reputation.weight.ConnectorReputation.by_block_rate`);
its routed value is the project's rule in force at that moment (rebuilt from the project's audit entries) applied over
the dynamic connectors that take requests at the place now. The report ingest keeps every change of an exit IP's avoid
(``ExitReputationRepository.avoid_moves``); a connector with IPs is drawn from its rows eligible now, by the exit IPs
they have now (:meth:`~api.reputation.weight.ConnectorReputation.by_usable_ips`). History starts with the first move
kept, and with the project's Keep data for days: nothing is drawn before.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from itertools import accumulate, pairwise
from typing import TYPE_CHECKING, Any, ClassVar

from api.db.audit_repository import AuditRepository
from api.db.repository import ConnectorRepository
from api.db.reputation_repository import ExitReputationRepository
from api.models.audit import AuditEntityType
from api.models.exit_reputation import (
    HistoryPoint,
    HistoryRange,
    HistoryRule,
    HistorySeries,
    PlaceHistorySeries,
    PlaceHistoryView,
    RuleInForce,
)
from api.reputation.record import ConnectorChange, IpChange, place_levels
from api.reputation.split import ReputationSplit
from api.reputation.weight import ConnectorReputation, ReputationKind

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.models.project import Project
    from api.models.proxy import Proxy
    from api.routing.router import Router

# The places a value is rounded to in an answer.
_DIGITS = 4
# Where every bucket of BucketMeans' points is counted from, as the metrics charts count theirs.
_EPOCH = datetime(1970, 1, 1)


class _Track:
    """One connector's factor at one level of the place over the range: what it was at the start, and each move after,
    with the requests and blocked reports each counted since the record's move before it."""

    def __init__(self, initial: float | None, moves: Sequence[ConnectorChange], before: ConnectorChange | None) -> None:
        self.initial = initial
        self.times = [move.at for move in moves]
        self.values = [move.new_factor for move in moves]
        self.counts: list[tuple[int, int] | None] = []
        previous = before
        for move in moves:
            self.counts.append(_since(previous, move))
            previous = move

    def at(self, moment: datetime) -> float | None:
        """The factor from ``moment`` on."""
        index = bisect_right(self.times, moment)
        return self.values[index - 1] if index else self.initial

    def counted(self, start: datetime, end: datetime, *, inclusive: bool) -> list[tuple[int, int] | None]:
        """The counts of the moves made from ``start`` until ``end`` (with it when ``inclusive``)."""
        last = bisect_right(self.times, end) if inclusive else bisect_left(self.times, end)
        return self.counts[bisect_left(self.times, start) : last]


def _since(previous: ConnectorChange | None, move: ConnectorChange) -> tuple[int, int] | None:
    """The requests and blocked reports ``move``'s record counted since ``previous``; None without one, or when the
    record was made again since and its lifetime counts began anew."""
    if previous is None or move.requests < previous.requests or move.blocked_reports < previous.blocked_reports:
        return None
    return move.requests - previous.requests, move.blocked_reports - previous.blocked_reports


class _Routing:
    """The dynamic connectors' routed values: the rule in force at a moment over every one's own factor there, as
    gateways compare them, worked out once per moment."""

    def __init__(self, place: str, tracks: Mapping[str, Mapping[str, _Track]], rules: Sequence[RuleInForce]) -> None:
        self._place = place
        self._tracks = tracks
        self._rules = rules
        self._by_moment: dict[datetime, dict[str, float]] = {}
        self._moments = {rule.at for rule in rules}.union(
            *(track.times for by_level in tracks.values() for track in by_level.values())
        )

    def moments(self) -> set[datetime]:
        """The moments a routed value can move: a dynamic connector's factor moved, or the rule did."""
        return self._moments

    def at(self, moment: datetime) -> dict[str, float]:
        """Each dynamic connector's routed value from ``moment`` on; 1 for one not judged."""
        routed = self._by_moment.get(moment)
        if routed is None:
            rule = self.rule(moment).rule
            reputations = {
                connector_id: ConnectorReputation.by_block_rate(
                    {level: factor for level, track in tracks.items() if (factor := track.at(moment)) is not None},
                    self._place,
                )
                for connector_id, tracks in self._tracks.items()
            }
            routed = self._by_moment[moment] = {
                connector_id: reputation.value for connector_id, reputation in rule.apply(reputations).items()
            }
        return routed

    def rule(self, moment: datetime) -> RuleInForce:
        """The rule in force at ``moment``."""
        index = bisect_right(self._rules, moment, key=lambda rule: rule.at)
        return self._rules[max(index - 1, 0)]


class _Avoids:
    """When each exit IP was avoided for the site from the start of the range, rebuilt from its avoid moves there:
    before the first, the end it moved from rules; without one, the end its record holds now. An avoid that ends has no
    move: it lasts until its end or the next move, whichever comes first."""

    def __init__(
        self, moves: Iterable[IpChange], ends: Mapping[str, datetime], start: datetime, until: datetime
    ) -> None:
        by_ip: dict[str, list[IpChange]] = {}
        for move in sorted(moves, key=lambda move: move.at):
            if start <= move.at <= until:
                by_ip.setdefault(move.key.ip, []).append(move)
        self._spans: dict[str, list[tuple[datetime, datetime]]] = {}
        for ip in by_ip.keys() | ends.keys():
            moved = by_ip.get(ip, [])
            held = [(start, moved[0].old_avoid_until if moved else ends.get(ip))]
            held += [(move.at, move.new_avoid_until) for move in moved]
            # The last end holds on past the range: one still ahead now has not ended.
            stops: list[datetime | None] = [move.at for move in moved] + [None]
            spans: list[tuple[datetime, datetime]] = []
            for (begin, end), stop in zip(held, stops, strict=True):
                if end is not None and (finish := end if stop is None else min(end, stop)) > begin:
                    spans.append((begin, finish))
            if spans:
                self._spans[ip] = spans

    def spans(self, ip: str) -> list[tuple[datetime, datetime]]:
        """The stretches ``ip`` was avoided from the start of the range, each from its start until before its end."""
        return self._spans.get(ip, [])


class ConnectorLine(ABC):
    """One connector's line on the chart: what measures it, the moments its values move within the range, and its point
    from a moment on or over a bucket of time."""

    kind: ClassVar[ReputationKind]

    def __init__(self, connector_id: str, name: str) -> None:
        self.connector_id = connector_id
        self.name = name

    @abstractmethod
    def moments(self) -> set[datetime]:
        """The moments its values can move; it is read at these alone."""

    @abstractmethod
    def at(self, moment: datetime) -> HistoryPoint:
        """Its point from ``moment`` on, with what moved at it."""

    @abstractmethod
    def mean(self, bounds: Sequence[datetime], *, last: bool) -> HistoryPoint:
        """Its point for the span from the first of ``bounds`` to the last, its values changing at the others alone:
        each value's mean weighted by how long it held. ``last`` says the span ends now, so a move then counts."""


class BlockRateLine(ConnectorLine):
    """A dynamic connector's line: its own factor at the finest level of the place with one, and its routed value beside
    the other dynamic connectors taking the requests."""

    kind = ReputationKind.BLOCK_RATE

    def __init__(self, connector_id: str, name: str, tracks: Mapping[str, _Track], routing: _Routing) -> None:
        super().__init__(connector_id, name)
        self._tracks = tracks
        self._routing = routing

    def moments(self) -> set[datetime]:
        """Every moment a routed value can move: its routed value is weighed beside the others'."""
        return self._routing.moments()

    def at(self, moment: datetime) -> HistoryPoint:
        own, level = self._own(moment)
        requests, blocked = self._counts(level, moment, moment, inclusive=True)
        routed = self._routing.at(moment)[self.connector_id]
        return HistoryPoint(moment, _rounded(own), level, round(routed, _DIGITS), requests, blocked)

    def mean(self, bounds: Sequence[datetime], *, last: bool) -> HistoryPoint:
        """The own factor's mean is over the time the connector was judged alone (None when it never was), with the
        level it came from longest and the requests and blocked reports of the span's moves."""
        own_sum = judged = routed_sum = 0.0
        level_time: dict[str, float] = {}
        for begin, finish in pairwise(bounds):
            held = (finish - begin).total_seconds()
            routed_sum += self._routing.at(begin)[self.connector_id] * held
            own, level = self._own(begin)
            if own is not None and level is not None:
                own_sum += own * held
                judged += held
                level_time[level] = level_time.get(level, 0.0) + held
        start, end = bounds[0], bounds[-1]
        level = max(level_time, key=level_time.__getitem__, default=None)
        requests, blocked = self._counts(level, start, end, inclusive=last)
        length = (end - start).total_seconds()
        return HistoryPoint(
            start,
            _rounded(own_sum / judged if judged else None),
            level,
            round(routed_sum / length, _DIGITS),
            requests,
            blocked,
        )

    def _own(self, moment: datetime) -> tuple[float | None, str | None]:
        """The own factor from ``moment`` on, at the finest level of the place with one, and that level."""
        for level, track in self._tracks.items():
            factor = track.at(moment)
            if factor is not None:
                return factor, level
        return None, None

    def _counts(
        self, level: str | None, start: datetime, end: datetime, *, inclusive: bool
    ) -> tuple[int | None, int | None]:
        """The requests and blocked reports the moves from ``start`` until ``end`` counted since the move before each,
        at ``level``, the one the own value comes from; else at a finer level that stopped judging the connector, which
        moved the value to ``level``. None when none is known."""
        tracks = self._tracks
        moved = {
            track_level: counted
            for track_level, track in tracks.items()
            if (counted := track.counted(start, end, inclusive=inclusive))
        }
        levels = list(tracks)
        finer = levels if level is None else levels[: levels.index(level)]
        chosen = level if level in moved else next(
            (finer_level for finer_level in finer if finer_level in moved and tracks[finer_level].at(end) is None), None
        )
        if chosen is None:
            return None, None
        known = [counts for counts in moved[chosen] if counts is not None]
        if not known:
            return None, None
        return sum(requests for requests, _blocked in known), sum(blocked for _requests, blocked in known)


class UsableIpsLine(ConnectorLine):
    """A connector with IPs' line: the share of its rows eligible now whose exit IP (as the row has it now) was not
    avoided for the site, 1 with none eligible; Strength and Lowest factor never touch it, so own and routed agree."""

    kind = ReputationKind.USABLE_IPS

    def __init__(self, connector_id: str, name: str, exits: Sequence[str | None], avoids: _Avoids) -> None:
        """``exits`` holds the exit IP of each eligible row, None for a row with none known."""
        super().__init__(connector_id, name)
        self.eligible = len(exits)
        steps: dict[datetime, int] = {}
        for ip, rows in Counter(ip for ip in exits if ip is not None).items():
            for begin, end in avoids.spans(ip):
                steps[begin] = steps.get(begin, 0) + rows
                steps[end] = steps.get(end, 0) - rows
        # The rows avoided from each time a count changed on.
        self._times = sorted(steps)
        self._avoided = list(accumulate(steps[time] for time in self._times))

    def moments(self) -> set[datetime]:
        return set(self._times)

    def at(self, moment: datetime) -> HistoryPoint:
        usable = self._usable(moment)
        value = round(self._value(usable), _DIGITS)
        return HistoryPoint(moment, value, None, value, usable=usable, eligible=self.eligible)

    def mean(self, bounds: Sequence[datetime], *, last: bool) -> HistoryPoint:
        """The usable rows' mean is rounded to a tenth of a row."""
        value_sum = usable_sum = 0.0
        for begin, finish in pairwise(bounds):
            held = (finish - begin).total_seconds()
            usable = self._usable(begin)
            value_sum += self._value(usable) * held
            usable_sum += usable * held
        length = (bounds[-1] - bounds[0]).total_seconds()
        value = round(value_sum / length, _DIGITS)
        return HistoryPoint(bounds[0], value, None, value, usable=round(usable_sum / length, 1), eligible=self.eligible)

    def _usable(self, moment: datetime) -> int:
        """The eligible rows not avoided from ``moment`` on."""
        index = bisect_right(self._times, moment)
        return self.eligible - (self._avoided[index - 1] if index else 0)

    def _value(self, usable: int) -> float:
        return ConnectorReputation.by_usable_ips(usable, self.eligible).value


class FactorHistory(ABC):
    """The chart's lines over a range: one per connector taking the requests at the place, from the start of the range
    (or of the history, when later) until now, each point the connector's own and routed values.

    Pure computation over what :meth:`read` reads; a subclass says where the points fall.
    """

    # The span one point stands for; None when a point is each move.
    bucket_seconds: ClassVar[int | None] = None

    def __init__(
        self,
        *,
        site: str,
        place: str,
        range: HistoryRange,
        until: datetime,
        connectors: Mapping[str, str],
        moves: Iterable[ConnectorChange] = (),
        last_before: Iterable[ConnectorChange] = (),
        current: Mapping[tuple[str, str], float | None] | None = None,
        rules: Sequence[RuleInForce],
        recorded_since: datetime | None,
        exits: Mapping[str, Sequence[str | None]] | None = None,
        avoid_moves: Iterable[IpChange] = (),
        avoid_ends: Mapping[str, datetime] | None = None,
    ) -> None:
        """``connectors``, id -> name in the chart's order, are the connectors taking the requests; those in ``exits``
        have IPs, with the exit IP of each of their eligible rows, and the rest are dynamic.

        For the dynamic ones, ``moves`` are their moves at the levels of ``place`` within the range, ``last_before`` the
        move before the range of each level that has one, ``current`` each (connector, level)'s factor now, and
        ``rules`` the rule in force from the start (:meth:`RuleInForce.rebuilt`). For those with IPs, ``avoid_moves``
        are their exit IPs' avoid moves for the site within the range, and ``avoid_ends`` the end of each one's last
        avoid as its record holds it now."""
        self.site = site
        self.place = place
        self.range = range
        self.until = until
        self.since = until - range.span
        self.recorded_since = recorded_since
        self.start = max(self.since, recorded_since) if recorded_since is not None else self.since
        self.rules = list(rules)
        exits = exits or {}
        current = current or {}
        dynamic = [connector_id for connector_id in connectors if connector_id not in exits]
        levels = place_levels(place)
        by_track: dict[tuple[str, str], list[ConnectorChange]] = {}
        for move in sorted(moves, key=lambda move: move.at):
            if move.key.connector_id in dynamic and move.at <= until:
                by_track.setdefault((move.key.connector_id, move.key.place), []).append(move)
        before = {(move.key.connector_id, move.key.place): move for move in last_before}
        tracks: dict[str, dict[str, _Track]] = {}
        for connector_id in dynamic:
            for level in levels:
                track_moves = by_track.get((connector_id, level), [])
                # Before its first move in the range a factor was what that move moved it from; without one it kept
                # the factor it has now.
                initial = track_moves[0].old_factor if track_moves else current.get((connector_id, level))
                tracks.setdefault(connector_id, {})[level] = _Track(
                    initial, track_moves, before.get((connector_id, level))
                )
        routing = _Routing(place, tracks, self.rules)
        avoids = _Avoids(avoid_moves, avoid_ends or {}, self.start, until)
        self.lines: list[ConnectorLine] = [
            UsableIpsLine(connector_id, name, exits[connector_id], avoids)
            if connector_id in exits
            else BlockRateLine(connector_id, name, tracks[connector_id], routing)
            for connector_id, name in connectors.items()
        ]
        # Each line is read at its own moments alone: a dynamic line never moves at an avoid's, nor one with IPs at a
        # factor's or a rule's.
        self._moments = {
            line.connector_id: sorted(
                {self.start, self.until} | {at for at in line.moments() if self.start < at < self.until}
            )
            for line in self.lines
        }

    @classmethod
    def of(cls, range: HistoryRange, **inputs: Any) -> FactorHistory:
        """The history of ``range``: each move up to a day, hourly means over a week, three-hour means over 30 days."""
        return _HISTORIES.get(range, ExactSteps)(range=range, **inputs)

    @classmethod
    async def read(
        cls,
        session: AsyncSession,
        project: Project,
        preview: Router,
        *,
        site: str,
        place: str,
        range: HistoryRange,
        now: datetime,
    ) -> FactorHistory:
        """The history of ``site`` at ``place`` (a canonical ``LocationTarget`` key, "" for none) over ``range`` until
        ``now``, for the project's connectors that take the requests there now, as the preview router finds them
        eligible, those with IPs by the exit IPs their eligible rows have now. A handful of statements whatever the
        range."""
        split = ReputationSplit(project, preview, avoided={}, records=(), now=now)
        taking = split.groups_for(site, place)
        listed = sorted(
            (
                connector
                for connector in await ConnectorRepository(session).get_by_project(project.id)
                if connector.id in taking
            ),
            key=lambda connector: connector.name.lower(),
        )
        connectors = {connector.id: connector.name for connector in listed}
        dynamic = [connector_id for connector_id in connectors if split.dynamic(connector_id)]
        exits = {
            connector_id: _exits(preview, connector_id, taking[connector_id].proxies)
            for connector_id in connectors
            if connector_id not in dynamic
        }
        ips = sorted({ip for rows in exits.values() for ip in rows if ip is not None})
        repository = ExitReputationRepository(session)
        levels = place_levels(place)
        recorded_since = await repository.first_move_at(project.id)
        if recorded_since is not None and project.exit_reputation_retention_days > 0:
            # The project's moves before its Keep data for days go with the next sweep.
            recorded_since = max(recorded_since, now - timedelta(days=project.exit_reputation_retention_days))
        since = now - range.span
        start = max(since, recorded_since) if recorded_since is not None else since
        records = await repository.connector_records(project.id, [site], levels, connector_ids=dynamic)
        moves = await repository.factor_moves(project.id, site, levels, dynamic, start)
        last_before = await repository.last_moves_before(project.id, site, levels, dynamic, start)
        entries = await AuditRepository(session).made_after(AuditEntityType.PROJECT.value, project.id, start)
        return cls.of(
            range,
            site=site,
            place=place,
            until=now,
            connectors=connectors,
            moves=moves,
            last_before=last_before,
            current={(record.connector_id, record.place): record.factor for record in records},
            rules=RuleInForce.rebuilt(project, entries, start),
            recorded_since=recorded_since,
            exits=exits,
            avoid_moves=await repository.avoid_moves(project.id, site, ips, start),
            avoid_ends=await repository.avoid_ends(project.id, site, ips),
        )

    def series(self) -> list[HistorySeries]:
        """One line per connector, in the chart's order."""
        points = self._points()
        return [
            HistorySeries(line.connector_id, line.name, line.kind, points[line.connector_id]) for line in self.lines
        ]

    def view(self) -> PlaceHistoryView:
        """The chart's answer for this history: its range, the rules in force and every connector's line."""
        return PlaceHistoryView(
            site=self.site,
            place=self.place,
            range=self.range,
            since=self.since,
            until=self.until,
            recorded_since=self.recorded_since,
            bucket_seconds=self.bucket_seconds,
            rules=[HistoryRule.of(rule) for rule in self.rules],
            series=[PlaceHistorySeries.of(series) for series in self.series()],
        )

    @abstractmethod
    def _points(self) -> dict[str, list[HistoryPoint]]:
        """Each connector's points, by id."""


class ExactSteps(FactorHistory):
    """Every move, for a range of a day or less: a connector's point at the start, then at each moment one of its values
    moved or a dynamic connector's own level counted a move, each holding until the next."""

    def _points(self) -> dict[str, list[HistoryPoint]]:
        points: dict[str, list[HistoryPoint]] = {}
        for line in self.lines:
            listed = points[line.connector_id] = []
            for moment in self._moments[line.connector_id]:
                point = line.at(moment)
                # A point that repeats the one before it says nothing new: the one before holds on.
                if listed and point.repeats(listed[-1]):
                    continue
                listed.append(point)
        return points


class BucketMeans(FactorHistory):
    """A point per bucket of ``bucket_seconds``, counted from the epoch: each value's mean over the bucket weighted by
    how long it held (:meth:`ConnectorLine.mean`). A subclass says the bucket's size."""

    bucket_seconds: ClassVar[int]

    def _points(self) -> dict[str, list[HistoryPoint]]:
        points: dict[str, list[HistoryPoint]] = {line.connector_id: [] for line in self.lines}
        for start, end in self._buckets():
            for line in self.lines:
                # The moments the line's values could move within the bucket, each holding until the next.
                moments = self._moments[line.connector_id]
                inside = moments[bisect_right(moments, start) : bisect_left(moments, end)]
                points[line.connector_id].append(line.mean([start, *inside, end], last=end == self.until))
        return points

    def _buckets(self) -> list[tuple[datetime, datetime]]:
        """The buckets from the start until now, the first and last cut to them; none of no length."""
        step = timedelta(seconds=self.bucket_seconds)
        mark = self.start - (self.start - _EPOCH.replace(tzinfo=self.start.tzinfo)) % step + step
        bounds = [self.start, *_marks(mark, self.until, step), self.until]
        return [(start, end) for start, end in pairwise(bounds) if end > start]


class HourlyMeans(BucketMeans):
    """A point per hour of the clock, for a week."""

    bucket_seconds = 3600


class ThreeHourMeans(BucketMeans):
    """A point per three hours of the clock (from midnight UTC), for 30 days: 240 points a line beside a week's 168."""

    bucket_seconds = 3 * 3600


# The histories of means, by range; any other range is ExactSteps.
_HISTORIES: dict[HistoryRange, type[FactorHistory]] = {
    HistoryRange.WEEK: HourlyMeans,
    HistoryRange.MONTH: ThreeHourMeans,
}


def _exits(preview: Router, connector_id: str, rows: Sequence[Proxy]) -> list[str | None]:
    """The exit IP of each of the connector's ``rows`` by its view's exits, None for a row with none."""
    view = preview.snapshot.connector_view(connector_id)
    exit_of = {row: ip for ip, ids in view.exits.items() for row in ids} if view is not None else {}
    return [exit_of.get(row.id) for row in rows]


def _marks(first: datetime, until: datetime, step: timedelta) -> list[datetime]:
    """``first`` and each ``step`` after it, before ``until``."""
    marks: list[datetime] = []
    mark = first
    while mark < until:
        marks.append(mark)
        mark += step
    return marks


def _rounded(value: float | None) -> float | None:
    return round(value, _DIGITS) if value is not None else None
