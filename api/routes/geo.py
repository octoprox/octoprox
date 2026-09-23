# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""IP attribution admin API: policy, databases, lookups, observations and provider accuracy.

Reads are open to every authenticated user; anything that changes the install
(policy, databases, re-attribution) needs an admin.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.core.auth import CurrentUserDep, RequireAdminDep
from api.core.config import settings
from api.core.event_bus import event_bus
from api.core.signals import geo_settings_changed
from api.db.geo_repository import GeoDatabaseRepository, ObservationRepository
from api.db.session import get_db
from api.geo.models import (
    GeoDatabaseRecord,
    GeoDatabaseSource,
    GeoSettings,
    IpLocation,
    LoadedDatabase,
    LocationCandidate,
    PreflightMode,
    Resolution,
    SourcePolicy,
    normalize_country,
)
from api.geo.readers import GeoDatabaseError, inspect_file, is_ip
from api.geo.updater import DownloadError, validate_bytes
from api.models.connector import Connector

if TYPE_CHECKING:
    from api.core.proxy_manager import ProxyManager
    from api.geo.runtime import GeoRuntime

logger = structlog.get_logger()

router = APIRouter(prefix="/geo")
DbDep = Annotated[AsyncSession, Depends(get_db)]

MAX_UPLOAD_BYTES = 512 * 1024 * 1024


# --- schemas ---------------------------------------------------------------------------------


class GeoSettingsResponse(BaseModel):
    settings: GeoSettings
    from_database: bool
    echo_enabled: bool
    echo_trusted_proxies: list[str]
    databases_loaded: list[LoadedDatabase]


class GeoDatabaseResponse(GeoDatabaseRecord):
    """A database row plus what this instance knows about it."""

    loaded_here: bool = False
    load_error: str | None = None
    has_file: bool = True


class GeoDatabaseListResponse(BaseModel):
    databases: list[GeoDatabaseResponse]


class GeoDatabaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10000)
    update_url: str | None = None
    update_interval_hours: int | None = Field(default=None, ge=0, le=24 * 365)
    update_auth: dict[str, Any] | None = None


class GeoDatabaseFromUrl(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    update_url: str = Field(min_length=8)
    update_interval_hours: int = Field(default=24, ge=0, le=24 * 365)
    update_auth: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=0, le=10000)
    enabled: bool = True


class LookupRequest(BaseModel):
    ip: str
    claimed_country: str | None = None
    # Resolve under this project's source policy instead of the install default.
    project_id: str | None = None


class LookupCandidate(BaseModel):
    source: str
    origin: str
    country: str | None
    location: IpLocation | None = None


class LookupResponse(BaseModel):
    ip: str
    resolution: Resolution
    policy: SourcePolicy
    candidates: list[LookupCandidate]
    databases_loaded: int


class ReattributeRequest(BaseModel):
    connector_id: str | None = None


class ReattributeResponse(BaseModel):
    # Proxies re-attributed, and how many of them ended up different.
    scanned: int
    updated: int


class ObservationsResponse(BaseModel):
    observations: list[dict[str, Any]]
    # Rows matching the filters, so the page can offer real paging.
    total: int
    limit: int
    offset: int


class ClaimBreakdown(BaseModel):
    """A contradicted pair: what the vendor claimed, what attribution resolved, how many exits."""

    claimed_country: str | None
    observed_country: str | None
    exits: int


class ExitCoverage(BaseModel):
    """How much of a dynamic-sessions connector's traffic these numbers can see.

    Absent for connectors whose exits health checks observe. For a dynamic
    connector only preflight sees exits: every client session once, and
    session-less requests at ``sampled_percent``. Below 100 the distinct-exit
    and reuse figures for session-less traffic are undercounts, not
    estimates; with preflight off the project sees the health probe alone.
    """

    dynamic: bool = True
    preflight_on: bool
    sampled_percent: int


def _exit_coverage(manager: ProxyManager, connector: Connector | None) -> ExitCoverage | None:
    if connector is None:
        return None
    target = manager.get_connector_target(connector)
    if target is None or not target.dynamic:
        return None
    project = manager.get_project(connector.project_id)
    preflight_on = project is not None and project.location_preflight != PreflightMode.OFF
    percent = (target.exit_sample_percent or 0) if preflight_on else 0
    return ExitCoverage(preflight_on=preflight_on, sampled_percent=percent)


class ConnectorAccuracy(BaseModel):
    """How a connector's distinct exits, seen in the window, judge the vendor's claims.

    Each exit counts once with the verdict of its latest observation.
    ``accuracy`` is confirmed over claimed; uncertain exits count against it.
    """

    connector_id: str
    connector_name: str | None = None
    project_id: str | None = None
    coverage: ExitCoverage | None = None
    exits: int
    claimed: int  # exits the vendor made a claim for
    confirmed: int
    contradicted: int
    uncertain: int
    accuracy: float | None
    breakdown: list[ClaimBreakdown]


class AccuracyResponse(BaseModel):
    since: datetime
    connectors: list[ConnectorAccuracy]


class ConnectorExits(BaseModel):
    connector_id: str
    connector_name: str | None = None
    project_id: str | None = None
    coverage: ExitCoverage | None = None
    unique_total: int
    unique_in_window: int
    sightings: int
    reused: int  # IPs handed out more than once
    max_sightings: int
    last_seen: datetime | None = None


class ExitsResponse(BaseModel):
    since: datetime
    connectors: list[ConnectorExits]


class ExitIp(BaseModel):
    """One distinct exit of a connector, with the state of its latest observation."""

    connector_id: str
    connector_name: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    ip: str
    first_seen: datetime
    last_seen: datetime
    sightings: int
    country: str | None = None
    proxy_id: str | None = None
    source: str | None = None
    claimed_country: str | None = None
    resolved_source: str | None = None
    conflict: bool = False
    disagreement: bool = False


class ExitIpsResponse(BaseModel):
    ips: list[ExitIp]
    total: int
    limit: int
    offset: int


class GeoStatusResponse(BaseModel):
    databases_loaded: int
    databases_total: int
    load_errors: dict[str, str]
    policy_from_database: bool
    pending_observations: int
    published_observations: int
    dropped_observations: int
    stored_observations: int
    preflight_checks: int
    preflight_rejections: int


# --- helpers ---------------------------------------------------------------------------------


def _runtime(request: Request) -> GeoRuntime:
    return cast("GeoRuntime", request.app.state.geo_runtime)


def _manager(request: Request) -> ProxyManager:
    return cast("ProxyManager", request.app.state.proxy_manager)


def _reattribute_later(request: Request) -> None:
    """Kick off an offline re-attribution without holding the request."""
    runtime = _runtime(request)

    async def run() -> None:
        try:
            await runtime.proxy_attributor.reattribute_all()
        except Exception as exc:
            logger.warning("Re-attribution after database change failed", error=str(exc))

    asyncio.create_task(run())


def _response(
    runtime: GeoRuntime, record: GeoDatabaseRecord, stored_ids: set[str] | None = None
) -> GeoDatabaseResponse:
    """One database row for the API.

    ``stored_ids`` are the rows whose bytes are in the database; a backup
    restored without its database files leaves rows with a checksum but no
    bytes, and those need re-uploading or refreshing. Without it (right after
    a write that stored the bytes) the checksum is proof enough: a scheduled
    download that has never succeeded has none.
    """
    store = runtime.database_store
    has_file = bool(record.sha256) if stored_ids is None else record.id in stored_ids
    return GeoDatabaseResponse(
        **record.model_dump(),
        loaded_here=store.is_loaded(record.id),
        load_error=store.load_errors.get(record.id),
        has_file=has_file,
    )


def _path_records(runtime: GeoRuntime, known: set[str]) -> list[GeoDatabaseResponse]:
    """Config-file databases, which have no row, shown from the store."""
    rows: list[GeoDatabaseResponse] = []
    for loaded in runtime.database_store.loaded:
        if loaded.source != GeoDatabaseSource.PATH or loaded.id in known:
            continue
        rows.append(
            GeoDatabaseResponse(
                id=loaded.id,
                name=loaded.name,
                vendor=loaded.vendor,
                kind=loaded.kind,
                format=loaded.format,
                source=loaded.source,
                enabled=True,
                priority=loaded.priority,
                path=loaded.path,
                size_bytes=loaded.size_bytes,
                database_type=loaded.database_type,
                build_epoch=loaded.build_epoch,
                record_count=loaded.record_count,
                loaded_here=True,
                has_file=True,
            )
        )
    return rows


def _record_from_info(
    info: Any, *, name: str, priority: int, enabled: bool, actor: str
) -> GeoDatabaseRecord:
    return GeoDatabaseRecord(
        name=name,
        vendor=info.vendor,
        kind=info.kind,
        format=info.format,
        source=GeoDatabaseSource.UPLOAD,
        enabled=enabled,
        priority=priority,
        sha256=info.sha256,
        size_bytes=info.size_bytes,
        database_type=info.database_type,
        build_epoch=info.build_epoch,
        record_count=info.record_count,
        ip_version=info.ip_version,
        languages=info.languages,
        description=info.description,
        attribution=info.attribution,
        uploaded_by=actor,
    )


# --- settings --------------------------------------------------------------------------------


@router.get("/settings", response_model=GeoSettingsResponse)
async def get_settings(request: Request, _user: CurrentUserDep) -> GeoSettingsResponse:
    """The live attribution policy and the databases this instance has open."""
    runtime = _runtime(request)
    return GeoSettingsResponse(
        settings=runtime.geo_service.settings,
        from_database=runtime.settings_store.from_database,
        echo_enabled=settings.geo_echo_enabled,
        echo_trusted_proxies=list(settings.geo_echo_trusted_proxies),
        databases_loaded=runtime.database_store.loaded,
    )


@router.put("/settings", response_model=GeoSettingsResponse)
async def put_settings(
    request: Request, body: GeoSettings, admin: RequireAdminDep
) -> GeoSettingsResponse:
    """Replace the install-wide attribution settings (admin). Reaches every instance."""
    runtime = _runtime(request)
    await runtime.settings_store.save(body, updated_by=admin.username)
    await event_bus.publish(geo_settings_changed, None, entity_id="default", op="updated")
    logger.info("Geo settings updated", admin=admin.username)
    return await get_settings(request, admin)


# --- databases -------------------------------------------------------------------------------


@router.get("/databases", response_model=GeoDatabaseListResponse)
async def list_databases(
    request: Request, session: DbDep, _user: CurrentUserDep
) -> GeoDatabaseListResponse:
    """Every database: stored rows plus operator-managed files from the config."""
    runtime = _runtime(request)
    repository = GeoDatabaseRepository(session)
    records = await repository.get_all()
    stored_ids = await repository.blob_ids()
    rows = [_response(runtime, r, stored_ids) for r in records]
    rows.extend(_path_records(runtime, {r.id for r in records}))
    rows.sort(key=lambda r: (r.priority, r.created_at))
    return GeoDatabaseListResponse(databases=rows)


@router.post("/databases", response_model=GeoDatabaseResponse, status_code=201)
async def upload_database(
    request: Request,
    session: DbDep,
    admin: RequireAdminDep,
    file: UploadFile,
    name: Annotated[str | None, Form()] = None,
    priority: Annotated[int, Form()] = 100,
    enabled: Annotated[bool, Form()] = True,
) -> GeoDatabaseResponse:
    """Upload an mmdb or IP2Location BIN file (admin).

    The file is opened before it is stored, so a broken or unrelated file is
    refused. Every proxy with a known exit IP is re-attributed afterwards.
    """
    runtime = _runtime(request)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Database file is too large")
    suffix = Path(file.filename or "").suffix or ".mmdb"
    try:
        info = await asyncio.to_thread(validate_bytes, raw, suffix)
    except GeoDatabaseError as exc:
        raise HTTPException(status_code=422, detail=f"Not a readable IP database: {exc}") from exc

    record = _record_from_info(
        info,
        name=(name or "").strip() or Path(file.filename or "database").stem,
        priority=priority,
        enabled=enabled,
        actor=admin.username,
    )
    await GeoDatabaseRepository(session).create(record, raw)
    await session.commit()
    await runtime.database_store.apply_record(record, raw)
    await runtime.publish_database_changed(record.id, "added")
    _reattribute_later(request)
    logger.info(
        "IP database uploaded",
        database_id=record.id,
        name=record.name,
        vendor=record.vendor.value,
        kind=record.kind.value,
        bytes=len(raw),
        admin=admin.username,
    )
    return _response(runtime, record)


@router.post("/databases/from-url", response_model=GeoDatabaseResponse, status_code=201)
async def add_database_from_url(
    request: Request, session: DbDep, body: GeoDatabaseFromUrl, admin: RequireAdminDep
) -> GeoDatabaseResponse:
    """Register a vendor download URL and fetch it now (admin).

    The row is created first so a failed download leaves it in the list with
    the error on it, ready to retry once the credentials are fixed.
    """
    runtime = _runtime(request)
    record = GeoDatabaseRecord(
        name=body.name.strip(),
        source=GeoDatabaseSource.URL,
        enabled=body.enabled,
        priority=body.priority,
        update_url=body.update_url.strip(),
        update_interval_hours=body.update_interval_hours,
        update_auth=body.update_auth,
        uploaded_by=admin.username,
    )
    await GeoDatabaseRepository(session).create(record, None)
    await session.commit()
    refreshed = await runtime.database_updater.refresh(record)
    if refreshed.last_update_error:
        await runtime.publish_database_changed(record.id, "added")
        raise HTTPException(
            status_code=400, detail=f"Download failed: {refreshed.last_update_error}"
        )
    _reattribute_later(request)
    logger.info(
        "IP database registered from URL",
        database_id=record.id,
        name=record.name,
        admin=admin.username,
    )
    return _response(runtime, refreshed)


@router.patch("/databases/{database_id}", response_model=GeoDatabaseResponse)
async def update_database(
    request: Request,
    session: DbDep,
    database_id: str,
    body: GeoDatabaseUpdate,
    admin: RequireAdminDep,
) -> GeoDatabaseResponse:
    """Change a database's name, priority, enabled flag or download schedule (admin)."""
    runtime = _runtime(request)
    repo = GeoDatabaseRepository(session)
    record = await repo.get_by_id(database_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Database not found")
    if body.name is not None:
        record.name = body.name.strip()
    if body.enabled is not None:
        record.enabled = body.enabled
    if body.priority is not None:
        record.priority = body.priority
    if body.update_url is not None:
        record.update_url = body.update_url.strip() or None
        if record.update_url and record.source == GeoDatabaseSource.UPLOAD:
            record.source = GeoDatabaseSource.URL
    if body.update_interval_hours is not None:
        record.update_interval_hours = body.update_interval_hours
    if body.update_auth is not None:
        record.update_auth = body.update_auth
    await repo.update(record)
    await session.commit()
    await runtime.database_store.apply_record(record)
    await runtime.publish_database_changed(record.id, "updated")
    if body.enabled is not None or body.priority is not None:
        _reattribute_later(request)
    return _response(runtime, record)


@router.post("/databases/{database_id}/refresh", response_model=GeoDatabaseResponse)
async def refresh_database(
    request: Request, session: DbDep, database_id: str, admin: RequireAdminDep
) -> GeoDatabaseResponse:
    """Download a scheduled database now (admin)."""
    runtime = _runtime(request)
    record = await GeoDatabaseRepository(session).get_by_id(database_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Database not found")
    if not record.update_url:
        raise HTTPException(status_code=400, detail="This database has no download URL")
    try:
        refreshed = await runtime.database_updater.refresh(record)
    except DownloadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if refreshed.last_update_error:
        raise HTTPException(status_code=502, detail=refreshed.last_update_error)
    _reattribute_later(request)
    return _response(runtime, refreshed)


@router.delete("/databases/{database_id}", status_code=204)
async def delete_database(
    request: Request, session: DbDep, database_id: str, admin: RequireAdminDep
) -> None:
    """Remove a stored database everywhere (admin). Config-file databases cannot be removed here."""
    runtime = _runtime(request)
    repo = GeoDatabaseRepository(session)
    record = await repo.get_by_id(database_id)
    if record is None:
        if runtime.database_store.is_loaded(database_id):
            raise HTTPException(
                status_code=400,
                detail="Operator-managed databases are removed from the config file",
            )
        raise HTTPException(status_code=404, detail="Database not found")
    await repo.delete(database_id)
    await session.commit()
    runtime.database_store.remove(database_id)
    await runtime.publish_database_changed(database_id, "removed")
    _reattribute_later(request)
    logger.info(
        "IP database deleted", database_id=database_id, name=record.name, admin=admin.username
    )


@router.post("/databases/inspect", response_model=dict[str, Any])
async def inspect_upload(file: UploadFile, _admin: RequireAdminDep) -> dict[str, Any]:
    """Open an uploaded file and report what it is, without storing it (admin)."""
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Database file is too large")
    suffix = Path(file.filename or "").suffix or ".mmdb"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(raw)
        temp = Path(handle.name)
    try:
        info = await asyncio.to_thread(inspect_file, temp)
    except GeoDatabaseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        temp.unlink(missing_ok=True)
    return {
        "format": info.format.value,
        "database_type": info.database_type,
        "vendor": info.vendor.value,
        "kind": info.kind.value,
        "build_epoch": info.build_epoch,
        "record_count": info.record_count,
        "ip_version": info.ip_version,
        "languages": info.languages,
        "description": info.description,
        "size_bytes": info.size_bytes,
        "sha256": info.sha256,
        "attribution": info.attribution,
    }


# --- lookups and re-attribution --------------------------------------------------------------


@router.post("/lookup", response_model=LookupResponse)
async def lookup(request: Request, body: LookupRequest, _user: CurrentUserDep) -> LookupResponse:
    """Resolve one IP with the loaded databases, optionally against a vendor claim."""
    runtime = _runtime(request)
    ip = body.ip.strip()
    if not is_ip(ip):
        raise HTTPException(status_code=400, detail="Not a valid IP address")
    claimed = normalize_country(body.claimed_country) if body.claimed_country else None
    if body.claimed_country and claimed is None:
        raise HTTPException(status_code=400, detail="claimed_country must be a two-letter ISO code")
    policy = runtime.geo_service.default_policy
    if body.project_id:
        project = _manager(request).get_project(body.project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        policy = project.source_policy(policy)
    resolution = runtime.geo_service.explain(ip, claimed, policy)
    return LookupResponse(
        ip=ip,
        resolution=resolution,
        policy=policy,
        candidates=[_lookup_candidate(c) for c in resolution.candidates],
        databases_loaded=len(runtime.database_store.loaded),
    )


def _lookup_candidate(candidate: LocationCandidate) -> LookupCandidate:
    return LookupCandidate(
        source=candidate.source.value,
        origin=candidate.origin,
        country=candidate.country,
        location=candidate.location,
    )


@router.post("/reattribute", response_model=ReattributeResponse)
async def reattribute(
    request: Request, body: ReattributeRequest, admin: RequireAdminDep
) -> ReattributeResponse:
    """Re-run attribution offline for every proxy with a known exit IP (admin)."""
    scanned, updated = await _runtime(request).proxy_attributor.reattribute_all(body.connector_id)
    logger.info(
        "Re-attribution requested",
        admin=admin.username,
        connector_id=body.connector_id,
        scanned=scanned,
        updated=updated,
    )
    return ReattributeResponse(scanned=scanned, updated=updated)


# --- observations and accuracy ---------------------------------------------------------------


@router.get("/observations", response_model=ObservationsResponse)
async def observations(
    request: Request,
    session: DbDep,
    _user: CurrentUserDep,
    connector_id: str | None = None,
    proxy_id: str | None = None,
    project_id: str | None = None,
    source: str | None = None,
    ip: str | None = None,
    claimed_country: str | None = None,
    resolved_country: str | None = None,
    verdict: Literal["contradicted", "uncertain", "confirmed", "no_claim"] | None = None,
    conflicts_only: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> ObservationsResponse:
    """One page of IP observations, newest first, filtered on the server.

    The filters mirror the columns of the history table; ``verdict`` is the
    label shown there (``conflicts_only`` is the older spelling of
    ``verdict=contradicted``).
    """
    filters: dict[str, Any] = {
        "connector_id": connector_id,
        "proxy_id": (proxy_id or "").strip() or None,
        "project_id": project_id,
        "source": source,
        "ip": (ip or "").strip() or None,
        "claimed_country": (claimed_country or "").strip() or None,
        "resolved_country": (resolved_country or "").strip() or None,
        "verdict": verdict,
        "conflicts_only": conflicts_only,
    }
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    rows, total = await ObservationRepository(session).recent(**filters, limit=limit, offset=offset)
    # Names come from the caches, so a deleted connector shows its id only.
    manager = _manager(request)
    connectors = {c.id: c.name for c in manager.connectors}
    projects = {p.id: p.name for p in manager.projects}
    for row in rows:
        row["connector_name"] = connectors.get(row["connector_id"]) if row["connector_id"] else None
        row["project_name"] = projects.get(row["project_id"]) if row["project_id"] else None
    return ObservationsResponse(observations=rows, total=total, limit=limit, offset=offset)


@router.get("/accuracy", response_model=AccuracyResponse)
async def accuracy(
    request: Request,
    session: DbDep,
    _user: CurrentUserDep,
    project_id: str | None = None,
    connector_id: str | None = None,
    days: int = 30,
) -> AccuracyResponse:
    """Per connector: of the distinct exits seen in the window, how the vendor's claims were judged."""
    manager = _manager(request)
    since = utc_now() - timedelta(days=max(1, min(days, 3650)))
    connectors = {c.id: c for c in manager.connectors}
    wanted: list[str] | None = None
    if connector_id:
        wanted = [connector_id]
    elif project_id:
        wanted = [c.id for c in connectors.values() if c.project_id == project_id]
    rows = await ObservationRepository(session).exit_accuracy(connector_ids=wanted, since=since)
    result: list[ConnectorAccuracy] = []
    for row in rows:
        connector = connectors.get(row["connector_id"])
        result.append(
            ConnectorAccuracy(
                connector_name=connector.name if connector else None,
                project_id=connector.project_id if connector else None,
                coverage=_exit_coverage(manager, connector),
                accuracy=round(row["confirmed"] / row["claimed"], 4) if row["claimed"] else None,
                **row,
            )
        )
    result.sort(key=lambda c: (-c.claimed, c.connector_name or ""))
    return AccuracyResponse(since=since, connectors=result)


@router.get("/exits", response_model=ExitsResponse)
async def exits(
    request: Request,
    session: DbDep,
    _user: CurrentUserDep,
    project_id: str | None = None,
    connector_id: str | None = None,
    days: int = 30,
) -> ExitsResponse:
    """Per connector: how many distinct exit IPs it has handed out, and how often they recur."""
    manager = _manager(request)
    since = utc_now() - timedelta(days=max(1, min(days, 3650)))
    connectors = {c.id: c for c in manager.connectors}
    wanted: list[str] | None = None
    if connector_id:
        wanted = [connector_id]
    elif project_id:
        wanted = [c.id for c in connectors.values() if c.project_id == project_id]
    rows = await ObservationRepository(session).exit_summary(connector_ids=wanted, since=since)
    result = []
    for row in rows:
        connector = connectors.get(row["connector_id"])
        result.append(
            ConnectorExits(
                connector_name=connector.name if connector else None,
                project_id=connector.project_id if connector else None,
                coverage=_exit_coverage(manager, connector),
                **row,
            )
        )
    result.sort(key=lambda c: (-c.unique_total, c.connector_name or ""))
    return ExitsResponse(since=since, connectors=result)


@router.get("/exits/ips", response_model=ExitIpsResponse)
async def exit_ips(
    request: Request,
    session: DbDep,
    _user: CurrentUserDep,
    project_id: str | None = None,
    connector_id: str | None = None,
    ip: str | None = None,
    proxy_id: str | None = None,
    country: str | None = None,
    claimed_country: str | None = None,
    verdict: Literal["contradicted", "uncertain", "confirmed", "no_claim"] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> ExitIpsResponse:
    """One page of distinct exit IPs with their latest state, most recently seen first.

    The unique-exits counterpart of the observation log: one row per connector
    and IP instead of one per sighting. Filters apply on the server.
    """
    manager = _manager(request)
    connectors = {c.id: c for c in manager.connectors}
    projects = {p.id: p.name for p in manager.projects}
    wanted: list[str] | None = None
    if connector_id:
        wanted = [connector_id]
    elif project_id:
        wanted = [c.id for c in connectors.values() if c.project_id == project_id]
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    rows, total = await ObservationRepository(session).exit_ips(
        connector_ids=wanted,
        ip=(ip or "").strip() or None,
        proxy_id=(proxy_id or "").strip() or None,
        country=(country or "").strip() or None,
        claimed_country=(claimed_country or "").strip() or None,
        verdict=verdict,
        limit=limit,
        offset=offset,
    )
    ips = []
    for row in rows:
        connector = connectors.get(row["connector_id"])
        ips.append(
            ExitIp(
                connector_name=connector.name if connector else None,
                project_id=connector.project_id if connector else None,
                project_name=projects.get(connector.project_id) if connector else None,
                **row,
            )
        )
    return ExitIpsResponse(ips=ips, total=total, limit=limit, offset=offset)


@router.get("/status", response_model=GeoStatusResponse)
async def status(request: Request, session: DbDep, _user: CurrentUserDep) -> GeoStatusResponse:
    """Pipeline health for the settings page."""
    runtime = _runtime(request)
    records = await GeoDatabaseRepository(session).get_all()
    stored = await ObservationRepository(session).count()
    recorder = runtime.observation_recorder
    return GeoStatusResponse(
        databases_loaded=len(runtime.database_store.loaded),
        databases_total=len(records) + len(_path_records(runtime, {r.id for r in records})),
        load_errors=runtime.database_store.load_errors,
        policy_from_database=runtime.settings_store.from_database,
        pending_observations=recorder.pending,
        published_observations=recorder.published,
        dropped_observations=recorder.dropped,
        stored_observations=stored,
        preflight_checks=runtime.preflight_checker.checks,
        preflight_rejections=runtime.preflight_checker.rejections,
    )
