# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""IP attribution admin API: policy, databases, lookups, re-attribution, observations and provider accuracy.

Reads are open to every signed-in user and writes need an admin; a write that changes how exits are judged re-judges all.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile

from api.core.auth import CurrentUserDep, RequireAdminDep
from api.geo.models import GeoSettings
from api.geo.store import MAX_BLOB_BYTES
from api.models.geo import (
    AccuracyResponse,
    CountResponse,
    ExitIpFilters,
    ExitIpsResponse,
    ExitsResponse,
    GeoDatabaseFromUrl,
    GeoDatabaseListResponse,
    GeoDatabaseResponse,
    GeoDatabaseUpdate,
    GeoSettingsResponse,
    GeoStatusResponse,
    LookupRequest,
    LookupResponse,
    ObservationFilters,
    ObservationsResponse,
    ReattributeQueued,
    ReattributeRequest,
    SharingResponse,
    Verdict,
)
from api.routes.common import (
    actor_of,
    geo_database_service_of,
    geo_settings_service_of,
    geo_status_service_of,
    observation_history_of,
    reattribution_service_of,
)

router = APIRouter(prefix="/geo")


# --- settings --------------------------------------------------------------------------------


@router.get("/settings", response_model=GeoSettingsResponse)
async def get_settings(request: Request, _user: CurrentUserDep) -> GeoSettingsResponse:
    """The attribution policy as stored, and the databases the answering api process has open.

    The row is read and adopted first (diff-only, so an unchanged row costs
    the one read); while Postgres cannot be read, the settings in memory.
    """
    return await geo_settings_service_of(request).get()


@router.put("/settings", response_model=GeoSettingsResponse)
async def put_settings(
    request: Request, body: GeoSettings, admin: RequireAdminDep
) -> GeoSettingsResponse:
    """Replace the install-wide attribution settings (admin); every process adopts them on their change entry.

    When the policy projects inherit (the conflict rule and the city radius) changed, every proxy is re-judged.
    """
    return await geo_settings_service_of(request).replace(body, actor_of(request, admin))


# --- databases -------------------------------------------------------------------------------


@router.get("/databases", response_model=GeoDatabaseListResponse)
async def list_databases(request: Request, _user: CurrentUserDep) -> GeoDatabaseListResponse:
    """Every database: stored rows plus operator-managed files from the config, with where each is open."""
    return await geo_database_service_of(request).every()


@router.post("/databases", response_model=GeoDatabaseResponse, status_code=201)
async def upload_database(
    request: Request,
    admin: RequireAdminDep,
    file: UploadFile,
    name: Annotated[str | None, Form()] = None,
    priority: Annotated[int, Form()] = 100,
    enabled: Annotated[bool, Form()] = True,
) -> GeoDatabaseResponse:
    """Upload an mmdb, IP2Location BIN or GeoNames file (admin), refused when it does not open.

    An enabled database is opened here at once, reaches the other processes with its change entry, and every proxy is
    re-judged with it.
    """
    raw = await file.read()
    if len(raw) > MAX_BLOB_BYTES:
        raise HTTPException(status_code=413, detail="Database file is too large")
    return await geo_database_service_of(request).upload(
        raw, file.filename, name, priority, enabled, actor_of(request, admin)
    )


@router.post("/databases/from-url", response_model=GeoDatabaseResponse, status_code=201)
async def add_database_from_url(
    request: Request, body: GeoDatabaseFromUrl, admin: RequireAdminDep
) -> GeoDatabaseResponse:
    """Register a vendor download URL and fetch it now (admin).

    The row is created and announced first, so a failed download leaves it listed with its error, ready to retry; the
    download's own write, and the re-judging a new file asks for, are the updater's.
    """
    return await geo_database_service_of(request).add_from_url(body, actor_of(request, admin))


@router.patch("/databases/{database_id}", response_model=GeoDatabaseResponse)
async def update_database(
    request: Request,
    database_id: str,
    body: GeoDatabaseUpdate,
    admin: RequireAdminDep,
) -> GeoDatabaseResponse:
    """Change a database's name, priority, enabled flag or download schedule (admin).

    Turning a database on or off, or moving an enabled one in the lookup order, re-judges every proxy; a download URL
    sent back masked, as the answers show it, keeps the stored one.
    """
    return await geo_database_service_of(request).update(database_id, body, actor_of(request, admin))


@router.post("/databases/{database_id}/refresh", response_model=GeoDatabaseResponse)
async def refresh_database(
    request: Request, database_id: str, _admin: RequireAdminDep
) -> GeoDatabaseResponse:
    """Download a scheduled database now (admin), paused or disabled as it may be, and answer when it is done.

    A new file of an enabled database is opened here and asks for every proxy to be re-judged (the updater's hook); an
    unchanged one asks for nothing.
    """
    return await geo_database_service_of(request).refresh(database_id)


@router.delete("/databases/{database_id}", status_code=204)
async def delete_database(request: Request, database_id: str, admin: RequireAdminDep) -> None:
    """Remove a stored database everywhere (admin). Config-file databases cannot be removed here."""
    await geo_database_service_of(request).delete(database_id, actor_of(request, admin))


@router.post("/databases/inspect", response_model=dict[str, Any])
async def inspect_upload(request: Request, file: UploadFile, _admin: RequireAdminDep) -> dict[str, Any]:
    """Open an uploaded file and report what it is, without storing it (admin)."""
    raw = await file.read()
    if len(raw) > MAX_BLOB_BYTES:
        raise HTTPException(status_code=413, detail="Database file is too large")
    return await geo_database_service_of(request).inspect(raw, file.filename)


# --- lookups and re-attribution --------------------------------------------------------------


@router.post("/lookup", response_model=LookupResponse)
async def lookup(request: Request, body: LookupRequest, _user: CurrentUserDep) -> LookupResponse:
    """Resolve one IP with the databases the answering api process has open, optionally against a vendor claim."""
    return await geo_settings_service_of(request).lookup(body)


@router.post("/reattribute", response_model=ReattributeQueued, status_code=202)
async def reattribute(
    request: Request, body: ReattributeRequest, admin: RequireAdminDep
) -> ReattributeQueued:
    """Ask a worker to re-judge every proxy with a known exit IP, or one connector's (admin).

    Answers 202 with the job's key once it is queued, or 503 when Redis did not take it within the request deadline.
    """
    return await reattribution_service_of(request).request(body.connector_id, actor_of(request, admin))


# --- observations and accuracy ---------------------------------------------------------------


def _observation_filters(
    connector_id: str | None = None,
    proxy_id: str | None = None,
    project_id: str | None = None,
    source: str | None = None,
    ip: str | None = None,
    claimed_country: str | None = None,
    resolved_country: str | None = None,
    verdict: Verdict | None = None,
    conflicts_only: bool = False,
    until: datetime | None = None,
) -> ObservationFilters:
    """The observation log's filters, from the query string, mirroring the columns of the history table.

    Args:
        verdict: The country badge: ``contradicted``, ``confirmed``, ``open`` (a claim with no verdict) or ``no_claim``.
        conflicts_only: The older spelling of ``verdict=contradicted``.
        until: Where the log starts instead of now; a time without an offset is UTC.
    """
    return ObservationFilters(
        connector_id=connector_id,
        proxy_id=proxy_id,
        project_id=project_id,
        source=source,
        ip=ip,
        claimed_country=claimed_country,
        resolved_country=resolved_country,
        verdict=verdict,
        conflicts_only=conflicts_only,
        until=until,
    )


ObservationFiltersDep = Annotated[ObservationFilters, Depends(_observation_filters)]


@router.get("/observations", response_model=ObservationsResponse)
async def observations(
    request: Request,
    _user: CurrentUserDep,
    filters: ObservationFiltersDep,
    limit: int = 100,
    offset: int = 0,
) -> ObservationsResponse:
    """One page of IP observations, newest first, filtered on the server (:func:`_observation_filters`).

    The total is counted no further than 10,000 rows past the page; ``/observations/count`` is the exact count. A page
    that reads past the time limit, as one under a value with no recent rows can, is called off with a 504.
    """
    return await observation_history_of(request).page(filters, limit, offset)


@router.get("/observations/count", response_model=CountResponse)
async def count_observations(request: Request, _user: CurrentUserDep, filters: ObservationFiltersDep) -> CountResponse:
    """Every observation under the log's filters, counted outright: asked for explicitly, never on the page's refresh.

    Under a project, connector, proxy or IP it scans that filter's index, under the other filters alone the whole
    table; past the time limit the count is called off with a 504.
    """
    return await observation_history_of(request).count(filters)


@router.get("/accuracy", response_model=AccuracyResponse)
async def accuracy(
    request: Request,
    _user: CurrentUserDep,
    project_id: str | None = None,
    connector_id: str | None = None,
    days: int = 30,
) -> AccuracyResponse:
    """Per connector: of the distinct exits seen in the window, how the vendor's claims were judged."""
    return await observation_history_of(request).accuracy(project_id, connector_id, days)


@router.get("/exits", response_model=ExitsResponse)
async def exits(
    request: Request,
    _user: CurrentUserDep,
    project_id: str | None = None,
    connector_id: str | None = None,
    days: int = 30,
) -> ExitsResponse:
    """Per connector: how many distinct exit IPs it has handed out, and how often they recur."""
    return await observation_history_of(request).exits(project_id, connector_id, days)


@router.get("/exits/sharing", response_model=SharingResponse)
async def exit_sharing(request: Request, _user: CurrentUserDep, project_id: str, days: int = 30) -> SharingResponse:
    """How a project's connectors' exits seen in the window overlap: per connector, per pair and across the project."""
    return await observation_history_of(request).sharing(project_id, days)


def _exit_ip_filters(
    project_id: str | None = None,
    connector_id: str | None = None,
    ip: str | None = None,
    proxy_id: str | None = None,
    country: str | None = None,
    claimed_country: str | None = None,
    verdict: Verdict | None = None,
) -> ExitIpFilters:
    """The exit IPs view's filters, from the query string; a project becomes its connectors."""
    return ExitIpFilters(
        project_id=project_id,
        connector_id=connector_id,
        ip=ip,
        proxy_id=proxy_id,
        country=country,
        claimed_country=claimed_country,
        verdict=verdict,
    )


ExitIpFiltersDep = Annotated[ExitIpFilters, Depends(_exit_ip_filters)]


@router.get("/exits/ips", response_model=ExitIpsResponse)
async def exit_ips(
    request: Request,
    _user: CurrentUserDep,
    filters: ExitIpFiltersDep,
    limit: int = 100,
    offset: int = 0,
) -> ExitIpsResponse:
    """One page of distinct exit IPs with their latest state, most recently seen first: a row per connector and IP.

    Filters apply on the server, ``verdict`` is the country verdict, and the total is counted as for observations.
    """
    return await observation_history_of(request).exit_ips(filters, limit, offset)


@router.get("/exits/ips/count", response_model=CountResponse)
async def count_exit_ips(request: Request, _user: CurrentUserDep, filters: ExitIpFiltersDep) -> CountResponse:
    """Every exit IP under the view's filters, counted outright, as for observations."""
    return await observation_history_of(request).count_exit_ips(filters)


@router.get("/status", response_model=GeoStatusResponse)
async def status(request: Request, _user: CurrentUserDep) -> GeoStatusResponse:
    """Pipeline health for the settings page: this api process, the observations stream, and every live gateway."""
    return await geo_status_service_of(request).get()
