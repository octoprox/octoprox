# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reputation as the Exit locations page reads it: a project's records, one IP's, per-IP totals for the exit IPs
view, and its connectors across its busiest sites. Open to every signed-in user; times in answers are UTC with a ``Z``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AfterValidator, BaseModel, Field

from api.core import utc_now
from api.core.auth import DbDep
from api.db.geo_repository import ObservationRepository
from api.db.repository import ConnectorRepository
from api.db.reputation_repository import ExitReputationRepository, ReputationSort, ReputationSummary
from api.reputation.record import ExitIpReputation as IpRecord
from api.reputation.record import ReputationPolicy, ReputationStatus
from api.reputation.site import canonical_ip
from api.reputation.split import MAX_LISTED, ConnectorShare, ReputationSplit, place_key, place_label
from api.reputation.weight import ReputationKind
from api.routes.common import api_runtime_of, require_project

router = APIRouter(prefix="/projects/{project_id}/exit-reputation")

# The most IPs one summary answers: a page of the exit IPs view at its largest.
MAX_SUMMARY_IPS = 1000
# The most sites one IP's answer lists, those in force first: an IP that served a wide crawl stays one quick read.
MAX_IP_SITES = 100
# The longest text an IP can be: a full IPv6 address with an IPv4 tail.
_IP_TEXT_LENGTH = 45
# The most sites the connectors view lays side by side, the most reported first.
MAX_TABLE_SITES = 8


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


class ConnectorSiteCell(BaseModel):
    """One connector on one site: its reputation there, what measures it, the counts behind it, and its share of the
    site's requests."""

    reputation: float = Field(description="What the connector's weight is multiplied by for the site; 1 when nothing says otherwise")
    reputation_kind: ReputationKind | None = Field(description="block_rate for a dynamic connector, usable_ips for one with IPs")
    reputation_place: str | None = Field(
        description="The level of the place a dynamic connector's factor came from; null when no level has one, and for a "
        "connector with IPs"
    )
    blocked: int | None = Field(
        description="Recent blocked reports (decayed by Connector memory, and by half every hour); null without a record"
    )
    requests: int | None = Field(description="Recent requests, decayed the same way; null without a record")
    usable_ips: int | None = Field(description="A connector with IPs: its eligible rows not avoided for the site")
    eligible_ips: int | None = Field(
        description="A connector with IPs: its rows eligible for a request to the site from the place, as routing finds them"
    )
    effective_share: float = Field(
        description="Share of the requests to the site from the place, 0-100, by effective weight where the project avoids, "
        "else by weight among the connectors serving them"
    )

    @classmethod
    def of(cls, share: ConnectorShare) -> ConnectorSiteCell:
        """``share`` as the view lays it out."""
        reputation = share.reputation
        return cls(
            reputation=reputation.value,
            reputation_kind=reputation.kind,
            reputation_place=reputation.place,
            blocked=share.blocked,
            requests=share.requests,
            usable_ips=reputation.usable_ips,
            eligible_ips=reputation.eligible_ips,
            effective_share=round(share.effective_share, 2),
        )


class ConnectorSites(BaseModel):
    """One connector of the project across the sites of the view."""

    connector_id: str
    name: str
    credential_type: str
    dynamic: bool = Field(description="Weighed by its block rate per site and place; otherwise by its usable exit IPs")
    weight: int
    cells: dict[str, ConnectorSiteCell] = Field(description="Site -> the connector there, for every site of the view")


class ReputationPlace(BaseModel):
    """A requested place with connector records, how it reads, and the reports made for it."""

    place: str = Field(description="A LocationTarget key: US, US/NY, US/NY/new_york or US//austin")
    label: str = Field(description="United States, US, NY or US, NY, new_york")
    reports: int


class ConnectorReputationView(BaseModel):
    """The project's connectors across its busiest sites, for a request naming one place."""

    place: str | None = Field(description="The place asked about, as a LocationTarget key; null for none")
    places: list[ReputationPlace] = Field(
        description=f"Places with connector records, most reported first, at most {MAX_LISTED}"
    )
    sites: list[str] = Field(description=f"The sites with the most reports, at most {MAX_TABLE_SITES}")
    reputation_applied: bool = Field(description="Routing weighs the connectors by it: the project's mode is avoid")
    connectors: list[ConnectorSites]


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


@router.get("/connectors", response_model=ConnectorReputationView)
async def connectors_by_site(
    request: Request,
    project_id: str,
    session: DbDep,
    place: Annotated[
        str | None,
        Query(max_length=256, description="The requested place, a LocationTarget key such as US, US/NY or US/NY/new_york"),
    ] = None,
) -> ConnectorReputationView:
    """Every connector of the project on each of its busiest sites: its reputation there for a request naming
    ``place`` (none: a request naming no place), and its share of the site's requests, as the traffic split shows one
    site."""
    try:
        wanted = place_key(place)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    project = await require_project(request, project_id)
    preview = await api_runtime_of(request).preview_router(project.id)
    repository = ExitReputationRepository(session)
    sites = [site for site, _reports in await repository.busiest_sites(project.id, limit=MAX_TABLE_SITES)]
    places = await repository.busiest_places(project.id, None, limit=MAX_LISTED)
    split = await ReputationSplit.read(session, project, preview, sites=sites, place=wanted, now=utc_now())
    connectors = sorted(
        await ConnectorRepository(session).get_by_project(project.id), key=lambda connector: connector.name.lower()
    )
    ids = [connector.id for connector in connectors]
    by_site = {site: split.shares(site, ids) for site in sites}
    return ConnectorReputationView(
        place=wanted or None,
        places=[ReputationPlace(place=key, label=place_label(key), reports=reports) for key, reports in places],
        sites=sites,
        reputation_applied=split.applied,
        connectors=[
            ConnectorSites(
                connector_id=connector.id,
                name=connector.name,
                credential_type=connector.credential_type,
                dynamic=split.dynamic(connector.id),
                weight=connector.weight,
                cells={site: ConnectorSiteCell.of(by_site[site][connector.id]) for site in sites},
            )
            for connector in connectors
        ],
    )
