# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reputation as the Exit locations page reads it: a project's records, one IP's, per-IP totals for the exit IPs
view, one site across every place, and how its connectors' values moved at one place over time; and for the project
form, what a draft Strength and Lowest factor would do. Open to every signed-in user; times in answers are UTC with a
``Z``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AfterValidator, BaseModel, Field

from api.core import utc_now
from api.core.auth import DbDep
from api.db.geo_repository import ObservationRepository
from api.db.repository import ConnectorRepository
from api.db.reputation_repository import ExitReputationRepository, ReputationSort, ReputationSummary
from api.models.project import MAX_REPUTATION_FLOOR, MAX_REPUTATION_STRENGTH, ExitReputationMode
from api.reputation.history import (
    FactorHistory,
    HistoryPoint,
    HistoryRange,
    HistorySeries,
    RuleInForce,
)
from api.reputation.record import ExitIpReputation as IpRecord
from api.reputation.record import ReputationPolicy, ReputationStatus, place_levels
from api.reputation.site import canonical_ip, report_site
from api.reputation.split import (
    ConnectorShare,
    PlaceLevel,
    ReputationSplit,
    SitePlaces,
    place_key,
    place_label,
)
from api.reputation.split import ConnectorTile as Tile
from api.reputation.weight import PlaceState, ReputationKind, ReputationRule
from api.routes.common import api_runtime_of, require_project

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.models.connector import Connector
    from api.models.project import Project
    from api.routing.router import Router

router = APIRouter(prefix="/projects/{project_id}/exit-reputation")

# The most IPs one summary answers: a page of the exit IPs view at its largest.
MAX_SUMMARY_IPS = 1000
# The most sites one IP's answer lists, those in force first: an IP that served a wide crawl stays one quick read.
MAX_IP_SITES = 100
# The longest text an IP can be: a full IPv6 address with an IPv4 tail.
_IP_TEXT_LENGTH = 45
# The most places a Strength preview lists: the site's most reported.
PREVIEW_PLACES = 5


def _as_utc(value: datetime) -> datetime:
    """A stored time, naive UTC, as an aware one, so the answer spells its zone."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


UtcTime = Annotated[datetime, AfterValidator(_as_utc)]


class ExitReputationRecord(BaseModel):
    """One exit IP's record on one site as it stands when answered: its counts, its weights decayed to now, its avoid
    and the status it makes."""

    ip: str
    site: str = Field(description="The exact host requests went to, or an IP literal")
    status: ReputationStatus = Field(description="avoided while an avoid is in force, else none")
    avoided_until: UtcTime | None = Field(description="The avoid's end while it is in force")
    good_reports: int
    bad_reports: int
    weighted_good: float = Field(description="The good reports, decayed by the half-life to the time of the answer")
    weighted_bad: float
    weighted_avoids: float = Field(
        description="The avoids begun for the site, decayed the same way: the next one lasts \"Avoid for\" doubled "
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
    ips: list[Annotated[str, Field(max_length=_IP_TEXT_LENGTH)]] = Field(max_length=MAX_SUMMARY_IPS)


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


class PlaceCell(BaseModel):
    """One connector at one place of the site: where its reputation stands there, the value in force, the place's own
    evidence as routing last counted it, and its share of the requests from the place."""

    state: PlaceState = Field(
        description="own: measured at the place; inherited: a dynamic connector's factor from a coarser level; "
        "not_judged: reported, but no level holds enough requests yet; no_reports: nothing reported; takes_none: "
        "routing keeps the connector off these requests"
    )
    reputation: float = Field(description="What the connector's weight is multiplied by here; 1 when nothing says otherwise")
    reputation_kind: ReputationKind | None = Field(
        default=None, description="block_rate for a dynamic connector, usable_ips for one with IPs"
    )
    reputation_place: str | None = Field(
        default=None,
        description="The level of the place a dynamic connector's factor came from; absent when no level has one, and "
        "for a connector with IPs",
    )
    own_factor: float | None = Field(default=None, description="A dynamic connector's factor stored at this place itself")
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
    best_connector_id: str | None = Field(default=None, description="That best connector; absent with best_success")
    blocked: int | None = Field(
        default=None,
        description="A dynamic connector: blocked reports at this place as routing last counted them, not decayed since; "
        "absent without a record",
    )
    requests: int | None = Field(default=None, description="Requests counted the same way; absent without a record")
    counted_at: UtcTime | None = Field(default=None, description="When those counts were last decided; absent without a record")
    usable_ips: int | None = Field(
        default=None, description="A connector with IPs: its eligible rows here not avoided for the site"
    )
    eligible_ips: int | None = Field(
        default=None,
        description="A connector with IPs: its rows eligible for a request to the site from the place, as routing finds them",
    )
    configured_share: float = Field(description="Share of the requests from the place, 0-100, by configured weight")
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
    inherited: bool = Field(description="The value is a dynamic connector's factor from a coarser level")


class SitePlace(BaseModel):
    """One place the site has records for, and every connector there."""

    place: str = Field(description='A LocationTarget key: "" (no place named), US, US/NY, US/NY/new_york or US//austin')
    label: str = Field(description="No place named, United States, New York, United States or New York, NY")
    level: PlaceLevel
    parent: str | None = Field(
        default=None, description="The next coarser level's place; absent for a country and for no place"
    )
    reports: int = Field(
        description="Reports through dynamic connectors at this exact place since its first; a country's include its "
        "cities'"
    )
    lowered: bool = Field(description="Some connector's own value here is under 1")
    not_judged: bool = Field(description="Some connector taking the requests is not judged here yet")
    lowest: PlaceLowest | None = Field(
        default=None,
        description="The lowest value in force among the connectors taking the requests; absent when none takes them",
    )
    cities_lowered: int | None = Field(
        default=None, description="A country with listed cities: those with some own value under 1; absent without any"
    )
    cities_total: int | None = Field(default=None, description="A country with listed cities: how many; absent without any")
    cells: dict[str, PlaceCell] = Field(description="Connector id -> the connector here, for every connector")


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
        default=None, description="A connector with IPs, for a request naming no place: its usable rows"
    )
    eligible_ips: int | None = None
    reputation: float | None = Field(
        default=None, description="A connector with IPs, for a request naming no place: its value"
    )

    @classmethod
    def of(cls, tile: Tile, no_place: ConnectorShare, dynamic: bool) -> ConnectorTile:
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
    dynamic: bool = Field(description="Weighed by its block rate per place; otherwise by its usable exit IPs")
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

    lowered_connectors: int = Field(description="Connectors with an own value under 1 at some listed place")
    serving_connectors: int = Field(description="Connectors taking the requests from some listed place")
    hardest: HardestHit | None = Field(
        default=None,
        description="The lowest own value under 1; on a tie the finer place first, then the more reported; a connector "
        "with IPs is skipped at a state or a city where it has at most 2 eligible rows",
    )


class SitePlacesView(BaseModel):
    """One site across every place its records name."""

    site: str | None = Field(default=None, description="The site shown; absent when the project has no connector records")
    mode: ExitReputationMode
    reputation_applied: bool = Field(description="Routing weighs the connectors by it: the project's mode is avoid")
    reputation_strength: int = Field(
        description="How hard a dynamic connector is weighed against the best one at a place; 1 is the plain trim"
    )
    reputation_floor: float = Field(description="The least factor a dynamic connector's weight is multiplied by, 0.01-1")
    sites: list[ReputationSite] = Field(
        description="Every site with connector records, most reported first; an asked site without any first"
    )
    connectors: list[SiteConnector] = Field(description="By name, as the traffic split lists them")
    places: list[SitePlace] = Field(
        description="No place named first when a dynamic connector has a record there, then each country, the most "
        "reported first, followed by its states and cities"
    )
    summary: SiteSummary

    @classmethod
    def of(
        cls,
        project: Project,
        connectors: Sequence[Connector],
        sites: list[ReputationSite],
        places: SitePlaces | None,
    ) -> SitePlacesView:
        """The view of ``places`` (None: no site) for ``connectors``, sorted by name."""
        mode = project.exit_reputation
        rule = ReputationRule.of(project)
        if places is None:
            return cls(
                site=None,
                mode=mode,
                reputation_applied=mode.avoids,
                reputation_strength=rule.strength,
                reputation_floor=rule.floor,
                sites=sites,
                connectors=[],
                places=[],
                summary=SiteSummary(lowered_connectors=0, serving_connectors=0, hardest=None),
            )
        split = places.split
        hardest = places.hardest
        return cls(
            site=places.site,
            mode=mode,
            reputation_applied=split.applied,
            reputation_strength=rule.strength,
            reputation_floor=rule.floor,
            sites=sites,
            connectors=[
                SiteConnector(
                    connector_id=connector.id,
                    name=connector.name,
                    credential_type=connector.credential_type,
                    enabled=connector.enabled,
                    weight=connector.weight,
                    dynamic=split.dynamic(connector.id),
                    excluded_reason=split.excluded_reason(connector),
                    tile=ConnectorTile.of(
                        places.tile(connector.id), places.no_place[connector.id], split.dynamic(connector.id)
                    ),
                )
                for connector in connectors
            ],
            places=[
                SitePlace(
                    place=row.place,
                    label=place_label(row.place),
                    level=row.level,
                    parent=row.parent,
                    reports=row.reports,
                    lowered=row.lowered,
                    not_judged=row.not_judged,
                    lowest=PlaceLowest(value=lowest[0], inherited=lowest[1]) if (lowest := row.lowest) else None,
                    cities_lowered=places.cities[row.place][0] if row.place in places.cities else None,
                    cities_total=places.cities[row.place][1] if row.place in places.cities else None,
                    cells={connector_id: PlaceCell.of(share) for connector_id, share in row.shares.items()},
                )
                for row in places.rows
            ],
            summary=SiteSummary(
                lowered_connectors=len(places.lowered),
                serving_connectors=len(places.serving),
                hardest=HardestHit(connector_id=hardest[0], place=hardest[1], value=hardest[2]) if hardest else None,
            ),
        )


@router.get("", response_model=ExitReputationPage)
async def list_records(
    request: Request,
    project_id: str,
    session: DbDep,
    ip: Annotated[
        str | None, Query(max_length=_IP_TEXT_LENGTH, description="A whole IP, or the start of one")
    ] = None,
    site: Annotated[str | None, Query(max_length=253, description="Any part of the site")] = None,
    status: Annotated[
        Literal["avoided", "none", "any"],
        Query(description="What holds now: avoided, or not"),
    ] = "any",
    sort: ReputationSort = ReputationSort.LAST_SEEN,
    limit: int = 100,
    offset: int = 0,
) -> ExitReputationPage:
    """One page of the project's records, one per exit IP and site, filtered on the server.

    Sorted by when each was last seen (newest first) or by reports (most first); the total is counted no further than
    10,000 rows past the page.
    """
    project = await require_project(request, project_id)
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    now = utc_now()
    policy = ReputationPolicy.of(project)
    records, total, exact = await ExitReputationRepository(session).page(
        project.id,
        now,
        ip=(ip or "").strip() or None,
        site=(site or "").strip() or None,
        status=None if status == "any" else ReputationStatus(status),
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return ExitReputationPage(
        records=[ExitReputationRecord.of(record, now, policy) for record in records],
        total=total,
        total_exact=exact,
        limit=limit,
        offset=offset,
    )


@router.get("/ips/{ip}", response_model=ExitIpReputation)
async def ip_records(
    request: Request, project_id: str, ip: str, session: DbDep
) -> ExitIpReputation:
    """One exit IP in the project: its sites' records, up to 100 with those in force first, and the project's
    connectors that handed it out.

    Both lists are empty for an IP the project has no record or sighting of; another project's never show.
    """
    project = await require_project(request, project_id)
    canonical = canonical_ip(ip)
    if canonical is None:
        raise HTTPException(status_code=400, detail="Not a valid IP address")
    now = utc_now()
    policy = ReputationPolicy.of(project)
    repository = ExitReputationRepository(session)
    records = await repository.of_ip(project.id, canonical, now, limit=MAX_IP_SITES)
    totals = (await repository.summaries(project.id, [canonical], now)).get(canonical)
    names = {
        connector.id: connector.name
        for connector in await ConnectorRepository(session).get_by_project(project.id)
    }
    # One row per connector at most: the IP is exact.
    seen, _total, _exact = await ObservationRepository(session).exit_ips(
        connector_ids=list(names), ip=canonical, limit=max(1, len(names))
    )
    return ExitIpReputation(
        ip=canonical,
        sites=totals.sites if totals else 0,
        avoided_sites=totals.avoided_sites if totals else 0,
        records=[ExitReputationRecord.of(record, now, policy) for record in records],
        connectors=[
            ExitIpConnector(
                connector_id=row["connector_id"],
                connector_name=names[row["connector_id"]],
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
                sightings=row["sightings"],
            )
            for row in seen
        ],
    )


@router.post("/summary", response_model=ExitReputationSummaryResponse)
async def summary(
    request: Request, project_id: str, body: ExitReputationSummaryRequest, session: DbDep
) -> ExitReputationSummaryResponse:
    """Per IP, its records over every site: reports, sites, and sites avoiding it now; a read, sent as a POST so a
    page of IPs fits. An IP with no record, or that is not an IP, is left out."""
    project = await require_project(request, project_id)
    asked = {text: canonical_ip(text) for text in body.ips}
    found = await ExitReputationRepository(session).summaries(
        project.id, {ip for ip in asked.values() if ip is not None}, utc_now()
    )
    return ExitReputationSummaryResponse(
        ips=[
            ExitIpSummary.of(text, found[ip])
            for text, ip in asked.items()
            if ip is not None and ip in found
        ]
    )


@router.get("/places", response_model=SitePlacesView, response_model_exclude_none=True)
async def site_places(
    request: Request,
    project_id: str,
    session: DbDep,
    site: Annotated[
        str | None,
        Query(
            max_length=2048,
            description="A site (the exact host, or a URL); left out or blank, the most reported one with a place to "
            "list, or the most reported one when none has",
        ),
    ] = None,
) -> SitePlacesView:
    """One site across every place its records name: each connector's reputation and share of the requests from each
    place, its tile, and the hardest hit, as gateways would weigh them.

    Every place is listed, however many: a city's row comes with its state's and its country's. A field with no value
    is left out of the answer rather than sent as null: a city-heavy site is hundreds of cells per poll.
    """
    try:
        wanted = report_site(site) if site is not None and site.strip() else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    project = await require_project(request, project_id)
    preview = await api_runtime_of(request).preview_router(project.id)
    repository = ExitReputationRepository(session)
    sites = [ReputationSite(site=name, reports=reports) for name, reports in await repository.busiest_sites(project.id)]
    connectors = sorted(
        await ConnectorRepository(session).get_by_project(project.id), key=lambda connector: connector.name.lower()
    )
    ids = [connector.id for connector in connectors]
    now = utc_now()
    if wanted is None and sites:
        wanted = await _opening_site(session, project, preview, sites, ids, now)
    elif wanted is not None and all(listed.site != wanted for listed in sites):
        sites.insert(0, ReputationSite(site=wanted, reports=0))
    if wanted is None:
        return SitePlacesView.of(project, connectors, sites, None)
    split = await ReputationSplit.read(
        session, project, preview, sites=[wanted], levels=None, now=now, connector_ids=ids
    )
    # One eligibility pass per place: worked out beside the event loop, on a router built for this request alone.
    return await asyncio.to_thread(
        lambda: SitePlacesView.of(project, connectors, sites, SitePlaces(split, wanted, ids))
    )


async def _opening_site(
    session: AsyncSession,
    project: Project,
    preview: Router,
    sites: Sequence[ReputationSite],
    connector_ids: Sequence[str],
    now: datetime,
) -> str:
    """The site the Reputation tab opens on: the most reported one with a place to list, else the most reported."""
    names = [listed.site for listed in sites]
    listed_first = await ReputationSplit.busiest_listed(
        session, project, preview, sites=names, now=now, connector_ids=connector_ids
    )
    return listed_first or names[0]


class HistoryRule(BaseModel):
    """The project's rule from ``at`` on."""

    at: UtcTime
    applied: bool = Field(description="Routing weighs the connectors by it: the project's mode was avoid")
    strength: int
    floor: float = Field(description="The Lowest factor, 0.01-1")

    @classmethod
    def of(cls, rule: RuleInForce) -> HistoryRule:
        return cls(at=rule.at, applied=rule.applied, strength=rule.rule.strength, floor=rule.rule.floor)


class PlaceHistoryPoint(BaseModel):
    """One connector at one point: an exact moment up to a day, a bucket's time-weighted means over 7 or 30 days."""

    at: UtcTime = Field(description="When the values took hold; with bucket_seconds, the start of the bucket")
    own: float | None = Field(
        default=None,
        description="block_rate: its own factor at the finest level of the place with one; absent when not judged. "
        "usable_ips: the share of its eligible rows whose exit IP was not avoided for the site (1 with none eligible)",
    )
    level: str | None = Field(
        default=None, description="block_rate: the level of the place that factor came from; absent with own"
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
    blocked: int | None = Field(default=None, description="block_rate: blocked reports counted the same way")
    usable: int | float | None = Field(
        default=None,
        description="usable_ips: its eligible rows whose exit IP was not avoided (the bucket's mean to a tenth, with "
        "bucket_seconds)",
    )
    eligible: int | None = Field(
        default=None, description="usable_ips: its rows eligible for the requests now, with the exit IPs they have now"
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
    place: str = Field(description='The place as a canonical LocationTarget key; "" for requests naming none')
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

    @classmethod
    def of(cls, history: FactorHistory) -> PlaceHistoryView:
        return cls(
            site=history.site,
            place=history.place,
            range=history.range,
            since=history.since,
            until=history.until,
            recorded_since=history.recorded_since,
            bucket_seconds=history.bucket_seconds,
            rules=[HistoryRule.of(rule) for rule in history.rules],
            series=[PlaceHistorySeries.of(series) for series in history.series()],
        )


@router.get("/places/history", response_model=PlaceHistoryView, response_model_exclude_none=True)
async def place_history(
    request: Request,
    project_id: str,
    session: DbDep,
    site: Annotated[str, Query(max_length=2048, description="A site (the exact host, or a URL)")],
    place: Annotated[
        str | None,
        Query(
            max_length=256,
            description="The requested place, a LocationTarget key such as US, US/NY or US/NY/new_york; left out or "
            "blank, requests naming none",
        ),
    ] = None,
    range: Annotated[HistoryRange, Query(description="How far back from now")] = HistoryRange.DAY,
) -> PlaceHistoryView:
    """How the project's connectors taking the requests to a site from a place moved over a range: a dynamic one's own
    factor, and the value routing multiplies its weight by under the Strength, Lowest factor and mode in force at each
    moment, with the moments those changed; a connector with IPs' share of its eligible rows whose exit IP the site did
    not avoid.

    Up to a day every move is a point; over a week each point is an hour's time-weighted means, over 30 days three
    hours'. The routed values assume the connectors as they are set up now, and a connector with IPs its rows with the
    exit IPs they have now. A field with no value is left out of the answer.
    """
    try:
        wanted = report_site(site)
        wanted_place = place_key(place)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    project = await require_project(request, project_id)
    preview = await api_runtime_of(request).preview_router(project.id)
    history = await FactorHistory.read(
        session, project, preview, site=wanted, place=wanted_place, range=range, now=utc_now()
    )
    # The points of every moment for every connector: worked out beside the event loop.
    return await asyncio.to_thread(PlaceHistoryView.of, history)


class StrengthPreviewConnector(BaseModel):
    """One connector taking the requests from a place, and its share of them at the saved settings and the draft."""

    connector_id: str
    name: str
    current_share: float = Field(description="Share of the requests from the place, 0-100, as Avoid routes them now")
    draft_share: float = Field(description="The same under the draft Strength and Lowest factor")


class StrengthPreviewPlace(BaseModel):
    """One of the site's most reported places."""

    place: str = Field(description='A LocationTarget key: US, US/NY, US/NY/new_york or US//austin')
    label: str = Field(description="United States, New York, United States or New York, NY")
    reports: int = Field(description="Reports through the project's connectors at this exact place since its first")
    connectors: list[StrengthPreviewConnector] = Field(description="The connectors taking the requests, by name")


class StrengthPreviewResponse(BaseModel):
    """What a draft Strength and Lowest factor would do to the shares of a site's requests, as Avoid routes them."""

    site: str | None = Field(description="The site previewed; null when the project has no connector records")
    places: list[StrengthPreviewPlace] = Field(
        description=f"The site's {PREVIEW_PLACES} most reported places, the most reported first; empty before any"
    )


@router.get("/strength-preview", response_model=StrengthPreviewResponse)
async def strength_preview(
    request: Request,
    project_id: str,
    session: DbDep,
    strength: Annotated[int, Query(ge=1, le=MAX_REPUTATION_STRENGTH, description="The draft Strength")],
    floor: Annotated[int, Query(ge=1, le=MAX_REPUTATION_FLOOR, description="The draft Lowest factor, in percent")],
    site: Annotated[
        str | None,
        Query(
            max_length=2048,
            description="A site (the exact host, or a URL); left out or blank, the one the Reputation tab opens on",
        ),
    ] = None,
) -> StrengthPreviewResponse:
    """Each connector's share of the requests to a site from its most reported places, as Avoid routes them under the
    project's saved Strength and Lowest factor and under the draft ones, whatever the project's mode.

    One read of the connector records at those places: the draft changes the weighing alone, never the evidence.
    """
    try:
        wanted = report_site(site) if site is not None and site.strip() else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    project = await require_project(request, project_id)
    preview = await api_runtime_of(request).preview_router(project.id)
    repository = ExitReputationRepository(session)
    connectors = sorted(
        await ConnectorRepository(session).get_by_project(project.id), key=lambda connector: connector.name.lower()
    )
    ids = [connector.id for connector in connectors]
    now = utc_now()
    if wanted is None:
        sites = [ReputationSite(site=name, reports=reports) for name, reports in await repository.busiest_sites(project.id)]
        if not sites:
            return StrengthPreviewResponse(site=None, places=[])
        wanted = await _opening_site(session, project, preview, sites, ids, now)
    places = await repository.busiest_places(project.id, wanted, limit=PREVIEW_PLACES)
    if not places:
        return StrengthPreviewResponse(site=wanted, places=[])
    current = await ReputationSplit.read(
        session,
        project,
        preview,
        sites=[wanted],
        levels={level for place, _reports in places for level in place_levels(place)},
        now=now,
        connector_ids=ids,
    )
    draft = current.under(
        project.model_copy(update={"exit_reputation_strength": strength, "exit_reputation_floor": floor})
    )
    site_name = wanted

    def previewed() -> list[StrengthPreviewPlace]:
        listed: list[StrengthPreviewPlace] = []
        for place, reports in places:
            now_shares = current.shares(site_name, place, ids)
            draft_shares = draft.shares(site_name, place, ids)
            listed.append(
                StrengthPreviewPlace(
                    place=place,
                    label=place_label(place),
                    reports=reports,
                    connectors=[
                        StrengthPreviewConnector(
                            connector_id=connector.id,
                            name=connector.name,
                            current_share=round(now_shares[connector.id].avoid_share, 2),
                            draft_share=round(draft_shares[connector.id].avoid_share, 2),
                        )
                        for connector in connectors
                        if now_shares[connector.id].takes
                    ],
                )
            )
        return listed

    # One eligibility pass per place, shared by both splits: worked out beside the event loop.
    return StrengthPreviewResponse(site=wanted, places=await asyncio.to_thread(previewed))
