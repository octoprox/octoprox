# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reputation as the Exit locations page reads it: a project's records, one IP's, per-IP totals for the exit IPs
view, one site across every place, and how its connectors' values moved at one place over time; and for the project
form, what a draft Strength and Lowest factor would do. Open to every signed-in user; times in answers are UTC with a
``Z``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request

from api.db.reputation_repository import ReputationSort
from api.models.exit_reputation import (
    IP_TEXT_LENGTH,
    ExitIpReputation,
    ExitReputationFilters,
    ExitReputationPage,
    ExitReputationSummaryRequest,
    ExitReputationSummaryResponse,
    HistoryRange,
    PlaceHistoryView,
    SitePlacesView,
    StrengthPreviewResponse,
)
from api.models.project import MAX_REPUTATION_FLOOR, MAX_REPUTATION_STRENGTH
from api.routes.common import exit_ip_service_of, site_reputation_service_of

router = APIRouter(prefix="/projects/{project_id}/exit-reputation")


@router.get("", response_model=ExitReputationPage)
async def list_records(
    request: Request,
    project_id: str,
    ip: Annotated[
        str | None, Query(max_length=IP_TEXT_LENGTH, description="A whole IP, or the start of one")
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
    filters = ExitReputationFilters(ip=ip, site=site, status=status)
    return await exit_ip_service_of(request).of_project(project_id, filters, sort=sort, limit=limit, offset=offset)


@router.get("/ips/{ip}", response_model=ExitIpReputation)
async def ip_records(request: Request, project_id: str, ip: str) -> ExitIpReputation:
    """One exit IP in the project: its sites' records, up to 100 with those in force first, and the project's
    connectors that handed it out.

    Both lists are empty for an IP the project has no record or sighting of; another project's never show.
    """
    return await exit_ip_service_of(request).get(project_id, ip)


@router.post("/summary", response_model=ExitReputationSummaryResponse)
async def summary(
    request: Request, project_id: str, body: ExitReputationSummaryRequest
) -> ExitReputationSummaryResponse:
    """Per IP, its records over every site: reports, sites, and sites avoiding it now; a read, sent as a POST so a
    page of IPs fits. An IP with no record, or that is not an IP, is left out."""
    return await exit_ip_service_of(request).summaries(project_id, body.ips)


@router.get("/places", response_model=SitePlacesView, response_model_exclude_none=True)
async def site_places(
    request: Request,
    project_id: str,
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
    return await site_reputation_service_of(request).places(project_id, site)


@router.get("/places/history", response_model=PlaceHistoryView, response_model_exclude_none=True)
async def place_history(
    request: Request,
    project_id: str,
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
    return await site_reputation_service_of(request).history(project_id, site, place, range)


@router.get("/strength-preview", response_model=StrengthPreviewResponse)
async def strength_preview(
    request: Request,
    project_id: str,
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
    return await site_reputation_service_of(request).preview_strength(
        project_id, site, strength=strength, floor=floor
    )
