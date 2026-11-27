# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reputation as the Exit locations page and the project form read it: records, one IP's, per-IP totals, one site
across every place, a place's history, and a draft Strength's shares. Times are UTC with a ``Z``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, NamedTuple

from pydantic import AfterValidator, BaseModel, Field

from api.models.location import COUNTRY_NAMES, state_name_slug
from api.models.project import ExitReputationMode
from api.reputation.record import ReputationStatus
from api.reputation.weight import PlaceState, ReputationKind, ReputationRule

if TYPE_CHECKING:
    from api.models.audit import AuditEntry
    from api.models.project import Project
    from api.reputation.record import ConnectorSiteReputation, ReputationPolicy
    from api.reputation.record import ExitIpReputation as IpRecord
    from api.reputation.weight import ConnectorReputation

# The most IPs one summary answers: a page of the exit IPs view at its largest.
MAX_SUMMARY_IPS = 1000
# The most sites one IP's answer lists, those in force first: an IP that served a wide crawl stays one quick read.
MAX_IP_SITES = 100
# The longest text an IP can be: a full IPv6 address with an IPv4 tail.
IP_TEXT_LENGTH = 45
# The most places a Strength preview lists: the site's most reported.
PREVIEW_PLACES = 5
# How the place of a request naming none reads.
NO_PLACE_LABEL = "No place named"
# Under this a connector's value counts as heavily lowered on its tile.
HEAVILY_LOWERED = 0.5
# The project settings the rule is made of, as its audit entries name them.
_MODE, _STRENGTH, _FLOOR = "exit_reputation", "exit_reputation_strength", "exit_reputation_floor"


def _as_utc(value: datetime) -> datetime:
    """A stored time, naive UTC, as an aware one, so the answer spells its zone."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


UtcTime = Annotated[datetime, AfterValidator(_as_utc)]


@dataclass(frozen=True)
class ExitReputationFilters:
    """What a page of a project's records is narrowed to; a filter left None or blank, or a status of any, does not
    narrow."""

    # A whole IP, or the start of one.
    ip: str | None = None
    # Any part of the site.
    site: str | None = None
    # What holds now: avoided, or not.
    status: Literal["avoided", "none", "any"] = "any"


class ExitReputationRecord(BaseModel):
    """One exit IP's record on one site as it stands when answered: its counts, its weights decayed to now, its avoid
    and the status it makes."""

    ip: str
    site: str = Field(description="The exact host requests went to, or an IP literal")
    status: ReputationStatus = Field(description="avoided while an avoid is in force, else none")
    avoided_until: UtcTime | None = Field(description="The avoid's end while it is in force")
    good_reports: int
    bad_reports: int
    weighted_good: float = Field(
        description="The good reports, decayed by the half-life to the time of the answer"
    )
    weighted_bad: float
    weighted_avoids: float = Field(
        description='The avoids begun for the site, decayed the same way: the next one lasts "Avoid for" doubled '
        "this many times, rounded"
    )
    avoid_until: UtcTime | None = Field(description="The last avoid's end; may be past")
    first_seen_at: UtcTime
    last_seen_at: UtcTime
    last_report_at: UtcTime | None
    last_reason: str | None

    @classmethod
    def of(cls, record: IpRecord, now: datetime, policy: ReputationPolicy) -> ExitReputationRecord:
        """``record`` as it stands at ``now`` under the project's ``policy``."""
        factor = record.decay_factor(now, policy)
        return cls(
            ip=record.ip,
            site=record.site,
            status=record.status(now),
            avoided_until=record.avoided_until(now),
            good_reports=record.good_reports,
            bad_reports=record.bad_reports,
            weighted_good=record.weighted_good * factor,
            weighted_bad=record.weighted_bad * factor,
            weighted_avoids=record.weighted_avoids * factor,
            avoid_until=record.avoid_until,
            first_seen_at=record.first_seen_at,
            last_seen_at=record.last_seen_at,
            last_report_at=record.last_report_at,
            last_reason=record.last_reason,
        )


class ExitReputationPage(BaseModel):
    records: list[ExitReputationRecord]
    # Counted no further than 10,000 rows past the page: total_exact false makes it a lower bound.
    total: int
    total_exact: bool
    limit: int
    offset: int


class ExitIpConnector(BaseModel):
    """A connector of the project that handed out the IP, from its exit IPs."""

    connector_id: str
    connector_name: str
    first_seen: UtcTime
    last_seen: UtcTime
    sightings: int


class ExitIpReputation(BaseModel):
    """One IP in the project: how many sites it has a record for, those records in force first, and the project's
    connectors that served it."""

    ip: str
    sites: int = Field(description="Sites the IP has a record for")
    avoided_sites: int = Field(description="Sites that avoid the IP now")
    records: list[ExitReputationRecord] = Field(
        description=f"At most {MAX_IP_SITES}: the sites avoiding it now first, then the one seen last first"
    )
    connectors: list[ExitIpConnector] = Field(description="The one that saw it last first")


class ExitReputationSummaryRequest(BaseModel):
    ips: list[Annotated[str, Field(max_length=IP_TEXT_LENGTH)]] = Field(max_length=MAX_SUMMARY_IPS)


class ReputationSummary(NamedTuple):
    """One IP's records in a project over every site: its reports, the sites it has a record for, and those avoiding
    it now with the latest of their ends."""

    ip: str
    good_reports: int
    bad_reports: int
    sites: int
    avoided_sites: int
    avoided_until: datetime | None


class ExitIpSummary(BaseModel):
    """One IP's records over every site."""

    ip: str = Field(description="As the request gave it")
    good_reports: int
    bad_reports: int
    sites: int = Field(description="Sites the IP has a record for")
    avoided_sites: int = Field(description="Sites that avoid the IP now")
    avoided_until: UtcTime | None = Field(description="The latest end among those avoids")

    @classmethod
    def of(cls, ip: str, summary: ReputationSummary) -> ExitIpSummary:
        """``summary`` under the text the request named its IP by."""
        return cls(
            ip=ip,
            good_reports=summary.good_reports,
            bad_reports=summary.bad_reports,
            sites=summary.sites,
            avoided_sites=summary.avoided_sites,
            avoided_until=summary.avoided_until,
        )


class ExitReputationSummaryResponse(BaseModel):
    ips: list[ExitIpSummary] = Field(
        description="The IPs asked for that have a record, in the order asked"
    )


def place_label(place: str) -> str:
    """How a requested place reads: a country by its name ("United States"), a state with its country ("New York,
    United States", or "ENG, United Kingdom" without a name on record), a city with its state, else its country
    ("New York, NY", "London, United Kingdom"). A code with no name on record reads as itself."""
    if not place:
        return NO_PLACE_LABEL
    country, _, below = place.partition("/")
    country_name = COUNTRY_NAMES.get(country, country)
    if not below:
        return country_name
    state, _, city = below.partition("/")
    if city:
        return f"{_words(city)}, {state or country_name}"
    name = state_name_slug(country, state)
    return f"{_words(name) if name else state}, {country_name}"


def _words(slug: str) -> str:
    """A place slug as words: "new_york" reads "New York"."""
    return " ".join(word.capitalize() for word in slug.split("_") if word)


class PlaceLevel(StrEnum):
    """How fine a requested place is."""

    NONE = "none"
    COUNTRY = "country"
    STATE = "state"
    CITY = "city"

    @property
    def depth(self) -> int:
        """How many levels the place names, by declaration order: 0 for no place, 3 for a city."""
        return list(PlaceLevel).index(self)

    @classmethod
    def of(cls, place: str) -> PlaceLevel:
        """The level ``place`` (a ``LocationTarget`` key) names."""
        if not place:
            return cls.NONE
        _country, _, below = place.partition("/")
        if not below:
            return cls.COUNTRY
        return cls.CITY if below.partition("/")[2] else cls.STATE


@dataclass(frozen=True, slots=True)
class ConnectorShare:
    """One connector's part of a site's requests from a place: its reputation there and where it stands, the recent
    counts the traffic split shows beside it (decayed to now), the record of the place's own evidence, and its share in
    percent configured, effective (by reputation only where the project avoids) and as Avoid would make it."""

    reputation: ConnectorReputation
    state: PlaceState
    blocked: int | None
    requests: int | None
    own: ConnectorSiteReputation | None
    own_factor: float | None
    effective_weight: float
    effective_share: float
    configured_share: float
    avoid_share: float

    @property
    def takes(self) -> bool:
        """Whether routing lets the connector take the requests."""
        return self.state is not PlaceState.TAKES_NONE

    @property
    def lowered(self) -> bool:
        """Whether its own value there takes its weight under the configured one; an inherited one never counts."""
        return self.state is PlaceState.OWN and self.reputation.lowers


@dataclass(slots=True)
class TileCounts:
    """One connector across a site's places: its own values per level, lowered (under 1) of those measured, and its
    listed places by where it stands; takes-none places are not counted."""

    lowered_countries: int = 0
    countries: int = 0
    lowered_states: int = 0
    states: int = 0
    lowered_cities: int = 0
    cities: int = 0
    heavy: int = 0
    lowered: int = 0
    inherited: int = 0
    full: int = 0
    not_judged: int = 0

    def count(self, level: PlaceLevel, share: ConnectorShare) -> None:
        """Count the connector's ``share`` at a listed place of ``level``."""
        if share.state is PlaceState.OWN:
            self._own(level, share.lowered)
            value = share.reputation.value
            if value < HEAVILY_LOWERED:
                self.heavy += 1
            elif share.lowered:
                self.lowered += 1
            else:
                self.full += 1
        elif share.state is PlaceState.INHERITED:
            self.inherited += 1
        elif share.state is PlaceState.NOT_JUDGED:
            self.not_judged += 1
        elif share.state is PlaceState.NO_REPORTS:
            self.full += 1

    def _own(self, level: PlaceLevel, lowered: bool) -> None:
        """Count an own value at ``level``."""
        if level is PlaceLevel.COUNTRY:
            self.countries += 1
            self.lowered_countries += lowered
        elif level is PlaceLevel.STATE:
            self.states += 1
            self.lowered_states += lowered
        elif level is PlaceLevel.CITY:
            self.cities += 1
            self.lowered_cities += lowered


class PlaceCell(BaseModel):
    """One connector at one place of the site: where its reputation stands there, the value in force, the place's own
    evidence as routing last counted it, and its share of the requests from the place."""

    state: PlaceState = Field(
        description="own: measured at the place; inherited: a dynamic connector's factor from a coarser level; "
        "not_judged: reported, but no level holds enough requests yet; no_reports: nothing reported; takes_none: "
        "routing keeps the connector off these requests"
    )
    reputation: float = Field(
        description="What the connector's weight is multiplied by here; 1 when nothing says otherwise"
    )
    reputation_kind: ReputationKind | None = Field(
        default=None, description="block_rate for a dynamic connector, usable_ips for one with IPs"
    )
    reputation_place: str | None = Field(
        default=None,
        description="The level of the place a dynamic connector's factor came from; absent when no level has one, and "
        "for a connector with IPs",
    )
    own_factor: float | None = Field(
        default=None, description="A dynamic connector's factor stored at this place itself"
    )
    success: float | None = Field(
        default=None,
        description="A judged dynamic connector's own factor (one less its block rate) at the level its reputation came "
        "from, before it is compared with the best; absent when not judged",
    )
    best_success: float | None = Field(
        default=None,
        description="The own factor of the best judged dynamic connector taking these requests, which this one is "
        "compared with; absent when this one is not judged",
    )
    best_connector_id: str | None = Field(
        default=None, description="That best connector; absent with best_success"
    )
    blocked: int | None = Field(
        default=None,
        description="A dynamic connector: blocked reports at this place as routing last counted them, not decayed since; "
        "absent without a record",
    )
    requests: int | None = Field(
        default=None, description="Requests counted the same way; absent without a record"
    )
    counted_at: UtcTime | None = Field(
        default=None, description="When those counts were last decided; absent without a record"
    )
    usable_ips: int | None = Field(
        default=None,
        description="A connector with IPs: its eligible rows here not avoided for the site",
    )
    eligible_ips: int | None = Field(
        default=None,
        description="A connector with IPs: its rows eligible for a request to the site from the place, as routing finds them",
    )
    configured_share: float = Field(
        description="Share of the requests from the place, 0-100, by configured weight"
    )
    effective_share: float = Field(
        description="Share of the requests from the place, 0-100, by weight times reputation whatever the mode: what "
        "Avoid does, or would do"
    )

    @classmethod
    def of(cls, share: ConnectorShare) -> PlaceCell:
        """``share`` as the view lays it out; a connector with IPs' site-wide counts are left out, being the same at
        every place."""
        reputation = share.reputation
        own = share.own if reputation.kind is ReputationKind.BLOCK_RATE else None
        return cls(
            state=share.state,
            reputation=reputation.value,
            reputation_kind=reputation.kind,
            reputation_place=reputation.place,
            own_factor=share.own_factor,
            success=reputation.success,
            best_success=reputation.best,
            best_connector_id=reputation.best_connector_id,
            blocked=round(own.weighted_blocked) if own is not None else None,
            requests=round(own.weighted_requests) if own is not None else None,
            counted_at=own.weighted_at if own is not None else None,
            usable_ips=reputation.usable_ips,
            eligible_ips=reputation.eligible_ips,
            configured_share=round(share.configured_share, 2),
            effective_share=round(share.avoid_share, 2),
        )


class PlaceLowest(BaseModel):
    """The lowest value in force at a place, and where it came from."""

    value: float
    inherited: bool = Field(
        description="The value is a dynamic connector's factor from a coarser level"
    )


class SitePlace(BaseModel):
    """One place the site has records for, and every connector there."""

    place: str = Field(
        description='A LocationTarget key: "" (no place named), US, US/NY, US/NY/new_york or US//austin'
    )
    label: str = Field(
        description="No place named, United States, New York, United States or New York, NY"
    )
    level: PlaceLevel
    parent: str | None = Field(
        default=None,
        description="The next coarser level's place; absent for a country and for no place",
    )
    reports: int = Field(
        description="Reports through dynamic connectors at this exact place since its first; a country's include its "
        "cities'"
    )
    lowered: bool = Field(description="Some connector's own value here is under 1")
    not_judged: bool = Field(
        description="Some connector taking the requests is not judged here yet"
    )
    lowest: PlaceLowest | None = Field(
        default=None,
        description="The lowest value in force among the connectors taking the requests; absent when none takes them",
    )
    cities_lowered: int | None = Field(
        default=None,
        description="A country with listed cities: those with some own value under 1; absent without any",
    )
    cities_total: int | None = Field(
        default=None, description="A country with listed cities: how many; absent without any"
    )
    cells: dict[str, PlaceCell] = Field(
        description="Connector id -> the connector here, for every connector"
    )


class TileBuckets(BaseModel):
    """A connector's listed places by where it stands; places it takes none of are left out."""

    heavy: int = Field(description="Own value under 0.5")
    lowered: int = Field(description="Own value from 0.5 to under 1")
    inherited: int
    full: int = Field(description="Own value of 1, or nothing reported")
    not_judged: int


class ConnectorTile(BaseModel):
    """One connector across the site's listed places: its own values per level, and how many are under 1."""

    lowered_countries: int
    countries: int
    lowered_states: int
    states: int
    lowered_cities: int
    cities: int
    buckets: TileBuckets
    usable_ips: int | None = Field(
        default=None,
        description="A connector with IPs, for a request naming no place: its usable rows",
    )
    eligible_ips: int | None = None
    reputation: float | None = Field(
        default=None, description="A connector with IPs, for a request naming no place: its value"
    )

    @classmethod
    def of(cls, tile: TileCounts, no_place: ConnectorShare, dynamic: bool) -> ConnectorTile:
        """``tile`` as the view lays it out, with a connector with IPs' value for a request naming no place."""
        reputation = no_place.reputation
        return cls(
            lowered_countries=tile.lowered_countries,
            countries=tile.countries,
            lowered_states=tile.lowered_states,
            states=tile.states,
            lowered_cities=tile.lowered_cities,
            cities=tile.cities,
            buckets=TileBuckets(
                heavy=tile.heavy,
                lowered=tile.lowered,
                inherited=tile.inherited,
                full=tile.full,
                not_judged=tile.not_judged,
            ),
            usable_ips=None if dynamic else reputation.usable_ips,
            eligible_ips=None if dynamic else reputation.eligible_ips,
            reputation=None if dynamic else reputation.value,
        )


class SiteConnector(BaseModel):
    """One connector of the project on the site."""

    connector_id: str
    name: str
    credential_type: str
    enabled: bool
    weight: int
    dynamic: bool = Field(
        description="Weighed by its block rate per place; otherwise by its usable exit IPs"
    )
    excluded_reason: str | None = Field(
        default=None,
        description="Why it takes no request naming no site and no place, as the traffic split says; absent while it "
        "does",
    )
    tile: ConnectorTile


class ReputationSite(BaseModel):
    """A site with connector records, and the reports made on it."""

    site: str
    reports: int


class HardestHit(BaseModel):
    """The connector at the place with the lowest own value under 1."""

    connector_id: str
    place: str
    value: float


class SiteSummary(BaseModel):
    """The site in one line: how many connectors are lowered somewhere, and the hardest hit."""

    lowered_connectors: int = Field(
        description="Connectors with an own value under 1 at some listed place"
    )
    serving_connectors: int = Field(
        description="Connectors taking the requests from some listed place"
    )
    hardest: HardestHit | None = Field(
        default=None,
        description="The lowest own value under 1; on a tie the finer place first, then the more reported; a connector "
        "with IPs is skipped at a state or a city where it has at most 2 eligible rows",
    )


class SitePlacesView(BaseModel):
    """One site across every place its records name."""

    site: str | None = Field(
        default=None, description="The site shown; absent when the project has no connector records"
    )
    mode: ExitReputationMode
    reputation_applied: bool = Field(
        description="Routing weighs the connectors by it: the project's mode is avoid"
    )
    reputation_strength: int = Field(
        description="How hard a dynamic connector is weighed against the best one at a place; 1 is the plain trim"
    )
    reputation_floor: float = Field(
        description="The least factor a dynamic connector's weight is multiplied by, 0.01-1"
    )
    sites: list[ReputationSite] = Field(
        description="Every site with connector records, most reported first; an asked site without any first"
    )
    connectors: list[SiteConnector] = Field(description="By name, as the traffic split lists them")
    places: list[SitePlace] = Field(
        description="No place named first when a dynamic connector has a record there, then each country, the most "
        "reported first, followed by its states and cities"
    )
    summary: SiteSummary


class HistoryRange(StrEnum):
    """How far back the chart reaches from now."""

    HOUR = "1h"
    SIX_HOURS = "6h"
    DAY = "24h"
    WEEK = "7d"
    MONTH = "30d"

    @property
    def span(self) -> timedelta:
        return _SPANS[self]


_SPANS = {
    HistoryRange.HOUR: timedelta(hours=1),
    HistoryRange.SIX_HOURS: timedelta(hours=6),
    HistoryRange.DAY: timedelta(hours=24),
    HistoryRange.WEEK: timedelta(days=7),
    HistoryRange.MONTH: timedelta(days=30),
}


@dataclass(frozen=True, slots=True)
class RuleInForce:
    """The project's rule from ``at`` on: whether routing applies it (the mode avoids), and how it weighs."""

    at: datetime
    applied: bool
    rule: ReputationRule

    @classmethod
    def rebuilt(
        cls, project: Project, entries: Sequence[AuditEntry], start: datetime
    ) -> list[RuleInForce]:
        """The rule in force at ``start``, then one per change of it after, from the project's settings now and its
        audit ``entries`` made after ``start`` (the oldest first), undone newest first. A change that leaves the rule
        as it was (a mode that still does not avoid) is no new rule."""
        settings: dict[str, Any] = {
            _MODE: project.exit_reputation.value,
            _STRENGTH: project.exit_reputation_strength,
            _FLOOR: project.exit_reputation_floor,
        }
        later: list[tuple[datetime, dict[str, Any]]] = []
        for entry in reversed(entries):
            before = {
                name: change[0]
                for name, change in entry.changes.items()
                if name in settings
                and isinstance(change, list)
                and len(change) == 2
                and change[0] is not None
            }
            if before:
                later.append((entry.created_at, dict(settings)))
                settings.update(before)
        rules: list[RuleInForce] = []
        for at, values in [(start, settings), *reversed(later)]:
            rule = cls._of(at, values)
            if not rules or (rules[-1].applied, rules[-1].rule) != (rule.applied, rule.rule):
                rules.append(rule)
        return rules

    @classmethod
    def _of(cls, at: datetime, settings: Mapping[str, Any]) -> RuleInForce:
        """The rule of ``settings`` as a project stores them: a mode, a strength and a floor in percent."""
        return cls(
            at,
            ExitReputationMode(settings[_MODE]).avoids,
            ReputationRule(int(settings[_STRENGTH]), int(settings[_FLOOR]) / 100),
        )


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    """One connector at one point of the chart: its own value and the level of the place a dynamic connector's factor
    came from (None while not judged), its routed value, the requests and blocked reports a dynamic connector counted
    since its previous move (None when unknown), and a connector with IPs' usable rows (a bucket's mean to a tenth) and
    eligible rows."""

    at: datetime
    own: float | None
    level: str | None
    routed: float
    requests: int | None = None
    blocked: int | None = None
    usable: float | None = None
    eligible: int | None = None

    def repeats(self, previous: HistoryPoint) -> bool:
        """Whether the point says nothing new after ``previous``: no move counted, and the same values."""
        return self.requests is None and self._values() == previous._values()

    def _values(self) -> tuple[float | None, str | None, float, float | None, int | None]:
        return self.own, self.level, self.routed, self.usable, self.eligible


@dataclass(frozen=True, slots=True)
class HistorySeries:
    """One connector's line, and what measures it."""

    connector_id: str
    name: str
    kind: ReputationKind
    points: list[HistoryPoint]


class HistoryRule(BaseModel):
    """The project's rule from ``at`` on."""

    at: UtcTime
    applied: bool = Field(
        description="Routing weighs the connectors by it: the project's mode was avoid"
    )
    strength: int
    floor: float = Field(description="The Lowest factor, 0.01-1")

    @classmethod
    def of(cls, rule: RuleInForce) -> HistoryRule:
        return cls(
            at=rule.at, applied=rule.applied, strength=rule.rule.strength, floor=rule.rule.floor
        )


class PlaceHistoryPoint(BaseModel):
    """One connector at one point: an exact moment up to a day, a bucket's time-weighted means over 7 or 30 days."""

    at: UtcTime = Field(
        description="When the values took hold; with bucket_seconds, the start of the bucket"
    )
    own: float | None = Field(
        default=None,
        description="block_rate: its own factor at the finest level of the place with one; absent when not judged. "
        "usable_ips: the share of its eligible rows whose exit IP was not avoided for the site (1 with none eligible)",
    )
    level: str | None = Field(
        default=None,
        description="block_rate: the level of the place that factor came from; absent with own",
    )
    routed: float = Field(
        description="What its weight is multiplied by under the rule in force, beside the other dynamic connectors "
        "taking the requests; 1 when not judged. usable_ips: the same as own, since Strength and Lowest factor do "
        "not touch it"
    )
    requests: int | None = Field(
        default=None,
        description="block_rate: requests the record at level counted since that record's previous move, for its "
        "moves at this point (summed over the bucket with bucket_seconds); absent where it did not move, or when unknown",
    )
    blocked: int | None = Field(
        default=None, description="block_rate: blocked reports counted the same way"
    )
    usable: int | float | None = Field(
        default=None,
        description="usable_ips: its eligible rows whose exit IP was not avoided (the bucket's mean to a tenth, with "
        "bucket_seconds)",
    )
    eligible: int | None = Field(
        default=None,
        description="usable_ips: its rows eligible for the requests now, with the exit IPs they have now",
    )

    @classmethod
    def of(cls, point: HistoryPoint) -> PlaceHistoryPoint:
        return cls(
            at=point.at,
            own=point.own,
            level=point.level,
            routed=point.routed,
            requests=point.requests,
            blocked=point.blocked,
            usable=point.usable,
            eligible=point.eligible,
        )


class PlaceHistorySeries(BaseModel):
    """One connector taking the requests at the place, and what measures it."""

    connector_id: str
    name: str
    kind: ReputationKind = Field(
        description="block_rate for a dynamic connector, its factor; usable_ips for a connector with IPs, the share "
        "of its eligible rows not avoided"
    )
    points: list[PlaceHistoryPoint]

    @classmethod
    def of(cls, series: HistorySeries) -> PlaceHistorySeries:
        return cls(
            connector_id=series.connector_id,
            name=series.name,
            kind=series.kind,
            points=[PlaceHistoryPoint.of(point) for point in series.points],
        )


class PlaceHistoryView(BaseModel):
    """How a site's connectors' values moved at one place over a range of time."""

    site: str
    place: str = Field(
        description='The place as a canonical LocationTarget key; "" for requests naming none'
    )
    range: HistoryRange
    since: UtcTime = Field(description="The start of the range")
    until: UtcTime = Field(description="The end of the range: when answered")
    recorded_since: UtcTime | None = Field(
        default=None,
        description="When the history kept begins; nothing is drawn before it. Absent before any factor or avoid "
        "moved",
    )
    bucket_seconds: int | None = Field(
        default=None,
        description="The seconds each point's means span: 3600 for 7d, 10800 for 30d; absent when each point is a move",
    )
    rules: list[HistoryRule] = Field(
        description="The rule in force at the first point, then one per change of the mode, Strength or Lowest factor"
    )
    series: list[PlaceHistorySeries] = Field(
        description="One per connector taking the requests at the place now, dynamic or with IPs, by name. Up to a day "
        "a point at the start and at each moment the connector's values moved, each holding until the next; over a "
        "week one per hour, over 30 days one per three hours"
    )


class StrengthPreviewConnector(BaseModel):
    """One connector taking the requests from a place, and its share of them at the saved settings and the draft."""

    connector_id: str
    name: str
    current_share: float = Field(
        description="Share of the requests from the place, 0-100, as Avoid routes them now"
    )
    draft_share: float = Field(description="The same under the draft Strength and Lowest factor")


class StrengthPreviewPlace(BaseModel):
    """One of the site's most reported places."""

    place: str = Field(description="A LocationTarget key: US, US/NY, US/NY/new_york or US//austin")
    label: str = Field(description="United States, New York, United States or New York, NY")
    reports: int = Field(
        description="Reports through the project's connectors at this exact place since its first"
    )
    connectors: list[StrengthPreviewConnector] = Field(
        description="The connectors taking the requests, by name"
    )


class StrengthPreviewResponse(BaseModel):
    """What a draft Strength and Lowest factor would do to the shares of a site's requests, as Avoid routes them."""

    site: str | None = Field(
        description="The site previewed; null when the project has no connector records"
    )
    places: list[StrengthPreviewPlace] = Field(
        description=f"The site's {PREVIEW_PLACES} most reported places, the most reported first; empty before any"
    )
