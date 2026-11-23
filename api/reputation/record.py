# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The records exit reputation keeps, and the decisions they carry, made in Python as evidence arrives.

* :class:`ExitIpReputation`: one exit IP on one site of a project, and until when it is avoided there.
* :class:`ConnectorSiteReputation`: one connector on one site and requested place, and the factor its routing weight is
  multiplied by there.

An exit IP record's weights decay with the project's "Remember for" half-life, a connector record's with its requests
("Connector memory") and at least half every hour, each on one clock per record. A decision is stored and moves only
with new evidence: an avoid's end is a time, and a factor, decided once a minute by the metrics flush, moves only once
its block rate is a whole step from it (see docs/exit-reputation.md).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import NamedTuple

from api.models.project import Project

# The longest an avoid lasts, however often an exit was avoided before.
AVOID_CEILING = timedelta(days=7)
# The longest reason a record keeps from a report.
MAX_REASON_LENGTH = 64
# The most sites routing avoids one exit IP for at once: an IP that served a wide crawl can hold a record for
# thousands. The ones avoided longest are kept.
MAX_EXIT_SITES = 100
# The most a lifetime count of reports holds, its column's limit.
MAX_COUNT = 2**31 - 1
# Below this many weighted requests a connector's record tells too little, and stores no factor.
MIN_FACTOR_REQUESTS = 20.0
# How finely a connector's factor is stored at strength 1: steps of 1 / FACTOR_STEPS (0.05); a project's strength
# divides the step, since a higher strength tells finer success rates apart.
FACTOR_STEPS = 20
# How far short of a whole step a block rate may fall and still move a stored factor: in floating point 1 - 17 / 20 is
# a hair over 0.15.
_STEP_TOLERANCE = 1e-9
# The time floor of a connector record's memory: its evidence counts half every hour besides every "Connector memory"
# requests, so a quiet record's block rate stays recent.
CONNECTOR_HALF_LIFE_TIME = timedelta(hours=1)
# The longest requested place a record is kept at: a country, a state and a city slug with their separators.
MAX_PLACE_LENGTH = 128


class ReputationStatus(StrEnum):
    """Where an exit IP's record for a site stands at a moment: avoided there, or not."""

    AVOIDED = "avoided"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ReputationPolicy:
    """How a project weighs evidence: an exit IP record's half-life in seconds ("Remember for"), a connector record's in
    requests ("Connector memory"), the bad weight past which an exit is avoided (the project's bad reports to avoid,
    less half a report), the least share of a site's reports that must be bad (0 to 1), a first avoid's length in
    seconds ("Avoid for"), and the strength connectors are compared at, which sets how finely a factor is stored."""

    half_life: float
    connector_half_life: float
    threshold: float
    bad_share: float
    avoid_base: float
    strength: int = 1

    @classmethod
    def of(cls, project: Project) -> ReputationPolicy:
        """The policy of ``project``'s exit reputation settings."""
        return cls.of_settings(
            project.exit_reputation_avoid_minutes,
            project.exit_reputation_half_life_hours,
            project.exit_reputation_connector_half_life_requests,
            project.exit_reputation_bad_reports,
            project.exit_reputation_bad_share,
            project.exit_reputation_strength,
        )

    @classmethod
    def of_settings(
        cls,
        avoid_minutes: int,
        half_life_hours: int,
        connector_half_life_requests: int,
        bad_reports: int,
        bad_share: int,
        strength: int = 1,
    ) -> ReputationPolicy:
        """The policy of the settings as a project stores them: minutes, hours, requests, fresh bad reports, a
        percentage and a strength."""
        return cls(
            half_life=half_life_hours * 3600.0,
            connector_half_life=float(connector_half_life_requests),
            # N fresh reports pass N - 0.5; the half a report of slack lets one that arrived a little earlier still count.
            threshold=bad_reports - 0.5,
            bad_share=bad_share / 100,
            avoid_base=avoid_minutes * 60.0,
            strength=strength,
        )

    @property
    def factor_steps(self) -> int:
        """How many steps a connector's factor is stored in: ``FACTOR_STEPS`` per unit of strength."""
        return FACTOR_STEPS * self.strength

    def avoid_length(self, avoids: float) -> timedelta:
        """How long an avoid lasts after ``avoids`` earlier ones (decayed, rounded to whole ones): "Avoid for" doubled
        for each, at most ``AVOID_CEILING``."""
        # Past 2**32 any base is longer than the ceiling; the cap keeps the power finite.
        doublings = min(max(round(avoids), 0), 32)
        return timedelta(seconds=min(self.avoid_base * 2.0**doublings, AVOID_CEILING.total_seconds()))


def place_levels(place: str) -> tuple[str, ...]:
    """The levels of a requested place (a ``LocationTarget`` key), finest first: the key and each coarser one.

    ``US/NY/new_york`` is that, ``US/NY`` and ``US``; ``US//austin`` is that and ``US``; "" (no place) is its own and
    only level, never a parent's.
    """
    if not place:
        return ("",)
    country, _, below = place.partition("/")
    state, _, city = below.partition("/")
    levels = [place]
    if state and city:
        levels.append(f"{country}/{state}")
    if below:
        levels.append(country)
    return tuple(levels)


class ExitIpKey(NamedTuple):
    """What an exit IP's record is kept for: a project, the IP as its canonical text, and a site."""

    project_id: str
    ip: str
    site: str


class ConnectorSiteKey(NamedTuple):
    """What a connector's record is kept for: a project, the connector, a site, and a level of the requested place
    ("" for none)."""

    project_id: str
    connector_id: str
    site: str
    place: str

    def levels(self) -> tuple[ConnectorSiteKey, ...]:
        """The key at every level of its place, finest first (:func:`place_levels`): evidence is kept at each."""
        return tuple(self._replace(place=level) for level in place_levels(self.place))


@dataclass(frozen=True, slots=True)
class ReportCounts:
    """The reports a batch holds for one record: good and bad ones, with the latest reason given.

    Counts add up, so a batch folds to one per record before it is applied.
    """

    good: int = 0
    bad: int = 0
    reason: str | None = None

    def __add__(self, other: ReportCounts) -> ReportCounts:
        """Both as one, with the later one's reason when it gives one."""
        return ReportCounts(
            good=self.good + other.good, bad=self.bad + other.bad, reason=other.reason or self.reason
        )

    @classmethod
    def fold[K](cls, evidence: Iterable[tuple[K, ReportCounts]]) -> dict[K, ReportCounts]:
        """Evidence in the order it came, as one count per record key."""
        folded: dict[K, ReportCounts] = {}
        for key, counts in evidence:
            current = folded.get(key)
            folded[key] = counts if current is None else current + counts
        return folded


class IpChange(NamedTuple):
    """What a batch applied at ``at`` did to an exit IP's avoid: its end before and after."""

    key: ExitIpKey
    at: datetime
    old_avoid_until: datetime | None
    new_avoid_until: datetime | None

    @property
    def moved(self) -> bool:
        """Whether the avoid in force moved: begun, lifted or ended differently; an end already past counts as none."""
        return self._live(self.old_avoid_until) != self._live(self.new_avoid_until)

    def _live(self, end: datetime | None) -> datetime | None:
        return end if end is not None and end > self.at else None


class ConnectorChange(NamedTuple):
    """What a flush did to a connector's stored factor: before and after."""

    key: ConnectorSiteKey
    old_factor: float | None
    new_factor: float | None

    @property
    def moved(self) -> bool:
        return self.old_factor != self.new_factor


@dataclass(frozen=True, slots=True)
class ReputationChanges:
    """What one transaction did to the records: each exit IP's avoid and each dynamic connector's factor it touched, and
    which of their projects route by them (``avoiding``)."""

    ips: tuple[IpChange, ...] = ()
    connectors: tuple[ConnectorChange, ...] = ()
    avoiding: frozenset[str] = frozenset()

    @property
    def moved_projects(self) -> list[str]:
        """The projects whose routing the transaction moved, in order: an avoid in force or a stored factor changed in
        a project that avoids."""
        moved = {change.key.project_id for change in self.ips if change.moved}
        moved |= {change.key.project_id for change in self.connectors if change.moved}
        return sorted(moved & self.avoiding)


@dataclass(slots=True, kw_only=True)
class ReputationRecord(ABC):
    """What every record keeps besides its evidence: one clock its weights were last decayed at, and when it was first
    and last seen, with the day of the last for the retention sweep."""

    weighted_at: datetime
    first_seen_at: datetime
    last_seen_at: datetime
    last_seen_day: date

    @property
    @abstractmethod
    def key(self) -> tuple[str, ...]:
        """What the record is kept for."""

    @abstractmethod
    def decay_factor(self, now: datetime, policy: ReputationPolicy) -> float:
        """What the weights are worth at ``now`` with no new evidence: never more than they were."""

    def decay(self, now: datetime, policy: ReputationPolicy) -> None:
        """Bring the weights up to ``now``; a ``now`` before the last decay leaves them, and the clock, as they are."""
        self._decay_by(self.decay_factor(now, policy), now)

    def _decay_by(self, factor: float, now: datetime) -> None:
        """Multiply every weight by ``factor`` and move the clock to ``now``, never back."""
        self._scale(factor)
        self.weighted_at = max(self.weighted_at, now)

    def _elapsed(self, now: datetime) -> float:
        """Seconds from the clock to ``now``; none for a ``now`` before it."""
        return max((now - self.weighted_at).total_seconds(), 0.0)

    @abstractmethod
    def _scale(self, factor: float) -> None:
        """Multiply every weight by ``factor``."""

    def _seen(self, at: datetime) -> None:
        """Note evidence at ``at``: the last sighting only moves forward."""
        self.last_seen_at = max(self.last_seen_at, at)
        self.last_seen_day = self.last_seen_at.date()


@dataclass(slots=True, kw_only=True)
class ExitIpReputation(ReputationRecord):
    """One exit IP on one site of a project: the reports counted for its life, their decayed weights, the avoids begun
    there (decayed, for doubling), and until when the IP is avoided for the site."""

    project_id: str
    ip: str
    site: str
    good_reports: int = 0
    bad_reports: int = 0
    weighted_good: float = 0.0
    weighted_bad: float = 0.0
    weighted_avoids: float = 0.0
    avoid_until: datetime | None = None
    last_report_at: datetime | None = None
    last_reason: str | None = None

    @classmethod
    def new(cls, key: ExitIpKey, at: datetime) -> ExitIpReputation:
        """A record that holds no evidence yet, first seen at ``at``."""
        return cls(
            project_id=key.project_id,
            ip=key.ip,
            site=key.site,
            weighted_at=at,
            first_seen_at=at,
            last_seen_at=at,
            last_seen_day=at.date(),
        )

    @property
    def key(self) -> ExitIpKey:
        return ExitIpKey(self.project_id, self.ip, self.site)

    @property
    def bad_share(self) -> float | None:
        """The share of the site's recent reports that are bad; None before any report."""
        reported = self.weighted_bad + self.weighted_good
        return self.weighted_bad / reported if reported > 0 else None

    def avoided_until(self, now: datetime) -> datetime | None:
        """Until when the IP is avoided for the site as of ``now``; None when no avoid is in force."""
        end = self.avoid_until
        return end if end is not None and end > now else None

    def status(self, now: datetime) -> ReputationStatus:
        """Where the record stands as of ``now``."""
        return ReputationStatus.AVOIDED if self.avoided_until(now) is not None else ReputationStatus.NONE

    def decay_factor(self, now: datetime, policy: ReputationPolicy) -> float:
        """Half per "Remember for" since the clock."""
        return math.exp2(-self._elapsed(now) / policy.half_life)

    def _scale(self, factor: float) -> None:
        self.weighted_good *= factor
        self.weighted_bad *= factor
        self.weighted_avoids *= factor

    def add_reports(
        self, good: int, bad: int, at: datetime, reason: str | None, policy: ReputationPolicy
    ) -> None:
        """Count ``good`` and ``bad`` reports made at ``at`` and decide the site's avoid again.

        With no avoid in force, bad reports past the threshold and the bad share begin one, "Avoid for" doubled for each
        recent avoid; while one runs, bad reports add weight but never lengthen it, and good ones lift it once the bad
        share falls under the project's.
        """
        if good <= 0 and bad <= 0:
            return
        self.decay(at, policy)
        self.weighted_good += good
        self.weighted_bad += bad
        self.good_reports = min(self.good_reports + good, MAX_COUNT)
        self.bad_reports = min(self.bad_reports + bad, MAX_COUNT)
        self._seen(at)
        if self.last_report_at is None or at >= self.last_report_at:
            self.last_report_at = at
            if reason:
                self.last_reason = reason[:MAX_REASON_LENGTH]
        share = self.weighted_bad / (self.weighted_bad + self.weighted_good)
        if self.avoided_until(at) is not None:
            if good > 0 and share < policy.bad_share:
                self.avoid_until = None
        elif bad > 0 and self.weighted_bad > policy.threshold and share >= policy.bad_share:
            self.avoid_until = at + policy.avoid_length(self.weighted_avoids)
            self.weighted_avoids += 1

    def apply(self, counts: ReportCounts, now: datetime, policy: ReputationPolicy) -> IpChange:
        """Add a batch's reports for this record as of ``now``; what that did to its avoid."""
        before = self.avoid_until
        self.add_reports(counts.good, counts.bad, now, counts.reason, policy)
        return IpChange(self.key, now, before, self.avoid_until)


@dataclass(slots=True, kw_only=True)
class ConnectorSiteReputation(ReputationRecord):
    """One connector on one site and level of the requested place: the reports counted for its life, the requests it
    carried there since the record was made, their decayed weights, and the factor they make.

    The metrics flush applies a minute's evidence at once (:meth:`apply_evidence`) and decides the factor then.
    """

    project_id: str
    connector_id: str
    site: str
    place: str
    blocked_reports: int = 0
    good_reports: int = 0
    requests: int = 0
    weighted_blocked: float = 0.0
    weighted_requests: float = 0.0
    factor: float | None = None

    @classmethod
    def new(cls, key: ConnectorSiteKey, at: datetime) -> ConnectorSiteReputation:
        """A record that holds no evidence yet, first seen at ``at``."""
        return cls(
            project_id=key.project_id,
            connector_id=key.connector_id,
            site=key.site,
            place=key.place,
            weighted_at=at,
            first_seen_at=at,
            last_seen_at=at,
            last_seen_day=at.date(),
        )

    @property
    def key(self) -> ConnectorSiteKey:
        return ConnectorSiteKey(self.project_id, self.connector_id, self.site, self.place)

    @property
    def block_rate(self) -> float | None:
        """Recent blocked reports per recent request; None before any request."""
        return self.weighted_blocked / self.weighted_requests if self.weighted_requests > 0 else None

    def decay_factor(self, now: datetime, policy: ReputationPolicy) -> float:
        """Half per hour since the clock (``CONNECTOR_HALF_LIFE_TIME``): between flushes no request fades the weights,
        and "Remember for" never does."""
        return math.exp2(-self._elapsed(now) / CONNECTOR_HALF_LIFE_TIME.total_seconds())

    def _scale(self, factor: float) -> None:
        self.weighted_blocked *= factor
        self.weighted_requests *= factor

    def apply_evidence(
        self, requests: int, blocked: int, good: int, now: datetime, policy: ReputationPolicy
    ) -> ConnectorChange:
        """Add a flush's requests and blocked and good reports as of ``now``, and decide the factor once; what that did
        to it.

        The weights decay first, half per "Connector memory" of the requests added and half per hour since the clock.
        Blocked reports weigh against the requests; good ones are counted for the record's life only.
        """
        before = self.factor
        requests, blocked, good = max(requests, 0), max(blocked, 0), max(good, 0)
        if not (requests or blocked or good):
            return ConnectorChange(self.key, before, before)
        self._decay_by(self.decay_factor(now, policy) * math.exp2(-requests / policy.connector_half_life), now)
        self.weighted_blocked += blocked
        self.weighted_requests += requests
        self.blocked_reports = min(self.blocked_reports + blocked, MAX_COUNT)
        self.good_reports = min(self.good_reports + good, MAX_COUNT)
        self.requests += requests
        self._seen(now)
        self.factor = self._decided(policy)
        return ConnectorChange(self.key, before, self.factor)

    def _decided(self, policy: ReputationPolicy) -> float | None:
        """The factor the weights make: none below ``MIN_FACTOR_REQUESTS`` weighted requests; else one less the block
        rate to the nearest step of the project's (0.05 / strength), at least one step, a stored one kept until the rate
        is a whole step from it. Routing's lowest factor is the project's, applied as it routes."""
        if self.weighted_requests < MIN_FACTOR_REQUESTS:
            return None
        grid = policy.factor_steps
        steps = (1.0 - self.weighted_blocked / self.weighted_requests) * grid
        # Compared in whole steps, never as factors: 0.15 - 0.1 is a hair short of 0.05. A factor stored at another
        # strength is read on this one's grid.
        if self.factor is not None and abs(steps - round(self.factor * grid)) < 1 - _STEP_TOLERANCE:
            return self.factor
        return max(round(steps), 1) / grid

    def factor_on(self, policy: ReputationPolicy) -> float | None:
        """The factor the last flush's weights make on ``policy``'s grid with none stored to keep, as a flush at its
        strength would settle on: none below ``MIN_FACTOR_REQUESTS`` weighted requests, else one step at least."""
        if self.weighted_requests < MIN_FACTOR_REQUESTS:
            return None
        grid = policy.factor_steps
        return max(round((1.0 - self.weighted_blocked / self.weighted_requests) * grid), 1) / grid
