# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""IP attribution admin API: policy, databases, lookups, re-attribution, observations and provider accuracy.

Reads are open to every signed-in user and writes need an admin; a write that changes how exits are judged re-judges all.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from api.core import utc_now
from api.core.auth import CurrentUserDep, DbDep, RequireAdminDep
from api.core.logging import error_text
from api.core.roles import JobKind
from api.db.geo_repository import (
    EXACT_COUNT_TIMEOUT_MS,
    GeoDatabaseRepository,
    ObservationRepository,
)
from api.db.repository import ConnectorRepository, ProjectRepository
from api.db.streams import ChangeKind
from api.geo.models import (
    ExitCoverage,
    GeoDatabaseInfo,
    GeoDatabaseKind,
    GeoDatabaseRecord,
    GeoDatabaseSource,
    GeoSettings,
    IpLocation,
    LoadedDatabase,
    LocationCandidate,
    Resolution,
    SourcePolicy,
    normalize_country,
)
from api.geo.readers import GeoDatabaseError, inspect_file, is_ip
from api.geo.store import MAX_BLOB_BYTES
from api.geo.updater import DownloadError, next_update_at, validate_bytes
from api.jobs.attribution import REATTRIBUTE_ALL, ReattributeScope
from api.models.location import LocationTarget
from api.routes.common import (
    UNAVAILABLE_RETRY_AFTER_SECONDS,
    api_runtime_of,
    change_log_of,
    geo_database_store_of,
    geo_service_of,
    geo_settings_store_of,
    geo_updater_of,
    jobs_of,
    observation_recorder_of,
    require_project,
    runtime_view_of,
    session_factory_of,
    settings_of,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.geo.store import GeoDatabaseStore
    from api.models.connector import Connector
    from api.models.system import GatewaySnapshot

logger = structlog.get_logger()

router = APIRouter(prefix="/geo")

# What a masked part of a download URL reads as in an answer (``_shown_url``).
_MASKED = "***"


# --- schemas ---------------------------------------------------------------------------------


class GeoSettingsResponse(BaseModel):
    settings: GeoSettings
    from_database: bool
    echo_enabled: bool
    echo_trusted_proxies: list[str]
    # The databases the api process that answered has open, in lookup order.
    databases_loaded: list[LoadedDatabase]


class GeoDatabaseResponse(GeoDatabaseInfo):
    """A database row, with where it is open: the api process that answered, and each live gateway.

    Never the download credentials: any signed-in user may list databases, so the URL shows with its secrets masked.
    """

    has_update_auth: bool = False

    # Whether the api process that answered has it open (lookups and /echo
    # use its copy), and why not when opening it failed there.
    loaded_here: bool = False
    load_error: str | None = None
    has_file: bool = True
    # When the schedule is next due; null without one, or while it is paused or disabled.
    next_update_at: datetime | None = None
    # How many of the live gateways have it open, out of how many, as their
    # last heartbeats (a few seconds old) say; and why it is not open on the
    # gateways that report a reason, by gateway instance id.
    gateways_loaded: int = 0
    gateways_total: int = 0
    gateway_errors: dict[str, str] = Field(default_factory=dict)


class GeoDatabaseListResponse(BaseModel):
    databases: list[GeoDatabaseResponse]


class GeoDatabaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10000)
    update_url: str | None = None
    update_interval_hours: int | None = Field(default=None, ge=0, le=24 * 365)
    update_auth: dict[str, Any] | None = None
    update_paused: bool | None = None


class GeoDatabaseFromUrl(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    update_url: str = Field(min_length=8)
    update_interval_hours: int = Field(default=24, ge=0, le=24 * 365)
    update_auth: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=0, le=10000)
    enabled: bool = True


class LookupRequest(BaseModel):
    ip: str
    # What to judge the databases' answer against, as a vendor's claim would be.
    claimed_country: str | None = None
    claimed_state: str | None = None
    claimed_city: str | None = None
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
    # One connector's proxies; every proxy when absent.
    connector_id: str | None = None


class ReattributeQueued(BaseModel):
    """A re-attribution a worker will run: the job's kind, and the key it is queued under (``all`` or ``connector:<id>``)."""

    kind: Literal["reattribute"] = "reattribute"
    key: str


class ObservationsResponse(BaseModel):
    observations: list[dict[str, Any]]
    # Rows matching the filters, counted no further than 10,000 past this
    # page; total_exact says whether counting reached the end. Short of it
    # the total is a lower bound, and /count gives the exact one.
    total: int
    total_exact: bool
    limit: int
    offset: int


class CountResponse(BaseModel):
    # Every row matching the filters, counted outright.
    total: int


class ClaimBreakdown(BaseModel):
    """A contradicted pair at one level: what was claimed, what attribution resolved, how many exits."""

    level: Literal["country", "state", "city"] = "country"
    claimed: str | None
    observed: str | None
    exits: int


class LevelAccuracy(BaseModel):
    """How a connector's exits with a claim at one level were judged, each exit once."""

    claimed: int = 0
    confirmed: int = 0
    contradicted: int = 0
    # A claim without a verdict: nothing independent answered at that level (no
    # database knows the IP, or none knows its city), or the independent sources
    # disagreed under ``consensus``. Neither the vendor's credit nor its fault.
    open: int = 0
    # Confirmed over confirmed plus contradicted, at every level; None before any verdict.
    accuracy: float | None = None

    @classmethod
    def from_counts(cls, counts: dict[str, int]) -> LevelAccuracy:
        judged = counts.get("confirmed", 0) + counts.get("contradicted", 0)
        return cls(accuracy=round(counts.get("confirmed", 0) / judged, 4) if judged else None, **counts)


class ConnectorAccuracy(BaseModel):
    """How a connector's distinct exits, seen in the window, judge the vendor's claims.

    Each exit counts once with the verdict of its latest observation, at
    every level a claim was made: the country the vendor promised, and the
    state and city requests asked for.
    """

    connector_id: str
    connector_name: str | None = None
    project_id: str | None = None
    coverage: ExitCoverage | None = None
    exits: int
    country: LevelAccuracy = Field(default_factory=LevelAccuracy)
    state: LevelAccuracy = Field(default_factory=LevelAccuracy)
    city: LevelAccuracy = Field(default_factory=LevelAccuracy)
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
    country_conflict: bool | None = None
    claimed_state: str | None = None
    claimed_city: str | None = None
    resolved_state: str | None = None
    resolved_city: str | None = None
    state_conflict: bool | None = None
    city_conflict: bool | None = None


class ExitIpsResponse(BaseModel):
    ips: list[ExitIp]
    # Counted as for observations: total_exact false makes total a lower bound.
    total: int
    total_exact: bool
    limit: int
    offset: int


class GatewayGeoStatus(BaseModel):
    """One live gateway's attribution, as its last heartbeat (a few seconds old) reports it."""

    instance_id: str
    # The IP databases it has open, the ones it still waits for (their ids),
    # and why the others are not open, by database id.
    databases_loaded: int
    databases_missing: list[str]
    load_errors: dict[str, str]
    # Echoes preflight sent through an upstream, and verdicts that placed the
    # exit elsewhere, since the gateway started.
    preflight_checks: int
    preflight_rejections: int
    # Observations and preflight sightings waiting for Redis, and those
    # dropped since the gateway started because Redis would not take them.
    observations_pending: int
    observations_dropped: int
    sightings_pending: int
    sightings_dropped: int


class GeoStatusResponse(BaseModel):
    # The api process that answered: the databases it has open, out of
    # every database listed, and why the others are not open here.
    databases_loaded: int
    databases_total: int
    load_errors: dict[str, str]
    policy_from_database: bool
    # This api process's own observation buffer (Detect records into it);
    # each gateway's is in ``gateways``.
    pending_observations: int
    published_observations: int
    dropped_observations: int
    # Published by every process and not yet ingested by a worker (the
    # observations stream: sightings, and verdict rewrites after a
    # re-attribution); exit IPs and accuracy lag until they are. None when
    # Redis cannot say.
    queued_observations: int | None = None
    # The planner's row estimate, within a few percent: an exact count reads the table.
    stored_observations: int
    # Preflight runs on gateways: summed over every live gateway, since each started.
    preflight_checks: int
    preflight_rejections: int
    # Every live gateway, by instance id; empty when the heartbeats cannot be read.
    gateways: list[GatewayGeoStatus] = Field(default_factory=list)


# --- helpers ---------------------------------------------------------------------------------


async def _gateways(request: Request) -> dict[str, GatewaySnapshot]:
    """Every live gateway's part of its last heartbeat, by instance id; none when Redis cannot be read."""
    try:
        snapshots = await api_runtime_of(request).instance_snapshots()
    except (RedisError, OSError) as exc:
        logger.warning("Gateway heartbeats unavailable, shown without them", error=error_text(exc))
        return {}
    return {
        instance_id: snapshot.gateway
        for instance_id, snapshot in sorted(snapshots.items())
        if snapshot.gateway is not None
    }


def _response(
    store: GeoDatabaseStore,
    gateways: Mapping[str, GatewaySnapshot],
    record: GeoDatabaseRecord,
    stored_ids: set[str] | None = None,
) -> GeoDatabaseResponse:
    """One database row for the API, with where it is open.

    Args:
        stored_ids: The rows whose bytes are stored; None right after a write that stored them.
    """
    # A backup restored without its files leaves rows with a checksum and no
    # bytes, to re-upload or refresh; right after a write that stored them the
    # checksum is proof enough, since a download never yet done has none.
    has_file = bool(record.sha256) if stored_ids is None else record.id in stored_ids
    return GeoDatabaseResponse(
        **record.model_dump(exclude={"update_auth", "update_url"}),
        update_url=_shown_url(record.update_url),
        has_update_auth=bool(record.update_auth),
        loaded_here=store.is_loaded(record.id),
        load_error=store.load_errors.get(record.id),
        has_file=has_file,
        next_update_at=next_update_at(record),
        gateways_loaded=sum(1 for gateway in gateways.values() if record.id in gateway.geo_databases_loaded),
        gateways_total=len(gateways),
        gateway_errors={
            instance_id: gateway.geo_database_errors[record.id]
            for instance_id, gateway in gateways.items()
            if record.id in gateway.geo_database_errors
        },
    )


def _shown_url(url: str | None) -> str | None:
    """A download URL as anyone signed in may see it: its user part and every query value masked, where vendors take tokens.

    What stays shows which file is downloaded from where; a PATCH that sends it back as shown keeps the stored URL.
    """
    # Vendors take a token there as often as in ``update_auth``: IPinfo's
    # ``?token=``, MaxMind's older ``license_key=``, a key as the URL's user.
    if not url:
        return url
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{_MASKED}@{netloc.rpartition('@')[2]}"
    query = "&".join(
        f"{name}={_MASKED if value else ''}" if equals else _MASKED
        for name, equals, value in (param.partition("=") for param in parts.query.split("&") if param)
    )
    fragment = _MASKED if parts.fragment else ""
    return urlunsplit(parts._replace(netloc=netloc, query=query, fragment=fragment))


async def _row(request: Request, record: GeoDatabaseRecord) -> GeoDatabaseResponse:
    """The answer to a write of one database: the row as written, with where it is open now."""
    return _response(geo_database_store_of(request), await _gateways(request), record)


def _path_databases(store: GeoDatabaseStore, known: set[str]) -> list[LoadedDatabase]:
    """Config-file databases open in this process that have no row: they exist only in the config."""
    return [loaded for loaded in store.loaded if loaded.source == GeoDatabaseSource.PATH and loaded.id not in known]


def _path_records(
    store: GeoDatabaseStore, gateways: Mapping[str, GatewaySnapshot], known: set[str]
) -> list[GeoDatabaseResponse]:
    """Config-file databases, which have no row, shown from the store."""
    rows: list[GeoDatabaseResponse] = []
    for loaded in _path_databases(store, known):
        record = GeoDatabaseRecord(
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
        )
        # The file is the operator's, on disk: there are no stored bytes to miss.
        rows.append(_response(store, gateways, record, stored_ids={record.id}))
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


async def _reattribute_all(request: Request, reason: str) -> None:
    """Ask a worker to re-judge every proxy, after a committed write that changed how exits are judged.

    A request Redis does not take is logged and dropped: the write stands, and the next change, or the button, asks again.
    """
    if await jobs_of(request).request_now(JobKind.REATTRIBUTE, [REATTRIBUTE_ALL]):
        logger.info("Re-attribution requested", key=REATTRIBUTE_ALL, reason=reason)


# --- settings --------------------------------------------------------------------------------


def _settings_response(request: Request) -> GeoSettingsResponse:
    settings = settings_of(request)
    store = geo_settings_store_of(request)
    return GeoSettingsResponse(
        settings=store.settings,
        from_database=store.from_database,
        echo_enabled=settings.geo_echo_enabled,
        echo_trusted_proxies=list(settings.geo_echo_trusted_proxies),
        databases_loaded=geo_database_store_of(request).loaded,
    )


@router.get("/settings", response_model=GeoSettingsResponse)
async def get_settings(request: Request, _user: CurrentUserDep) -> GeoSettingsResponse:
    """The attribution policy as stored, and the databases the answering api process has open.

    The row is read and adopted first (diff-only, so an unchanged row costs
    the one read); while Postgres cannot be read, the settings in memory.
    """
    await geo_settings_store_of(request).load()
    return _settings_response(request)


@router.put("/settings", response_model=GeoSettingsResponse)
async def put_settings(
    request: Request, body: GeoSettings, admin: RequireAdminDep
) -> GeoSettingsResponse:
    """Replace the install-wide attribution settings (admin); every process adopts them on their change entry.

    When the policy projects inherit (the conflict rule and the city radius) changed, every proxy is re-judged.
    """
    store = geo_settings_store_of(request)
    # Compared with the stored row, read first: this process's copy may lag a
    # save made through another api, and comparing with it could skip the
    # re-judging this save calls for.
    await store.load()
    inherited = store.settings.default_policy
    await store.save(body, updated_by=admin.username)
    await change_log_of(request).record(ChangeKind.GEO_SETTINGS)
    if body.default_policy != inherited:
        await _reattribute_all(request, "default policy changed")
    return _settings_response(request)


# --- databases -------------------------------------------------------------------------------


@router.get("/databases", response_model=GeoDatabaseListResponse)
async def list_databases(
    request: Request, session: DbDep, _user: CurrentUserDep
) -> GeoDatabaseListResponse:
    """Every database: stored rows plus operator-managed files from the config, with where each is open."""
    store = geo_database_store_of(request)
    repository = GeoDatabaseRepository(session)
    records = await repository.get_all()
    stored_ids = await repository.blob_ids()
    gateways = await _gateways(request)
    rows = [_response(store, gateways, r, stored_ids) for r in records]
    rows.extend(_path_records(store, gateways, {r.id for r in records}))
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
    """Upload an mmdb, IP2Location BIN or GeoNames file (admin), refused when it does not open.

    An enabled database is opened here at once, reaches the other processes with its change entry, and every proxy is
    re-judged with it.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > MAX_BLOB_BYTES:
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
    await change_log_of(request).record(ChangeKind.GEO_DATABASE, record.id)
    await geo_database_store_of(request).apply_record(record, raw)
    logger.info(
        "IP database uploaded",
        database_id=record.id,
        name=record.name,
        vendor=record.vendor.value,
        kind=record.kind.value,
        bytes=len(raw),
        admin=admin.username,
    )
    # A disabled database is consulted by nothing, so no verdict is stale.
    if record.enabled:
        await _reattribute_all(request, "database added")
    return await _row(request, record)


@router.post("/databases/from-url", response_model=GeoDatabaseResponse, status_code=201)
async def add_database_from_url(
    request: Request, session: DbDep, body: GeoDatabaseFromUrl, admin: RequireAdminDep
) -> GeoDatabaseResponse:
    """Register a vendor download URL and fetch it now (admin).

    The row is created and announced first, so a failed download leaves it listed with its error, ready to retry; the
    download's own write, and the re-judging a new file asks for, are the updater's.
    """
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
    await change_log_of(request).record(ChangeKind.GEO_DATABASE, record.id)
    refreshed = await geo_updater_of(request).refresh(record)
    if refreshed.last_update_error:
        raise HTTPException(
            status_code=400, detail=f"Download failed: {refreshed.last_update_error}"
        )
    logger.info(
        "IP database registered from URL",
        database_id=record.id,
        name=record.name,
        admin=admin.username,
    )
    return await _row(request, refreshed)


@router.patch("/databases/{database_id}", response_model=GeoDatabaseResponse)
async def update_database(
    request: Request,
    session: DbDep,
    database_id: str,
    body: GeoDatabaseUpdate,
    admin: RequireAdminDep,
) -> GeoDatabaseResponse:
    """Change a database's name, priority, enabled flag or download schedule (admin).

    Turning a database on or off, or moving an enabled one in the lookup order, re-judges every proxy; a download URL
    sent back masked, as the answers show it, keeps the stored one.
    """
    repo = GeoDatabaseRepository(session)
    record = await repo.get_by_id(database_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Database not found")
    was_enabled, was_priority = record.enabled, record.priority
    if body.name is not None:
        record.name = body.name.strip()
    if body.enabled is not None:
        record.enabled = body.enabled
    if body.priority is not None:
        record.priority = body.priority
    if body.update_url is not None and body.update_url.strip() != _shown_url(record.update_url):
        record.update_url = body.update_url.strip() or None
        if record.update_url and record.source == GeoDatabaseSource.UPLOAD:
            record.source = GeoDatabaseSource.URL
    if body.update_interval_hours is not None:
        record.update_interval_hours = body.update_interval_hours
    if body.update_auth is not None:
        record.update_auth = body.update_auth
    if body.update_paused is not None:
        record.update_paused = body.update_paused
    await repo.update(record)
    await session.commit()
    await change_log_of(request).record(ChangeKind.GEO_DATABASE, database_id)
    await geo_database_store_of(request).apply_record(record)
    if record.enabled != was_enabled or (record.enabled and record.priority != was_priority):
        await _reattribute_all(request, "database order changed")
    return await _row(request, record)


@router.post("/databases/{database_id}/refresh", response_model=GeoDatabaseResponse)
async def refresh_database(
    request: Request, database_id: str, _admin: RequireAdminDep
) -> GeoDatabaseResponse:
    """Download a scheduled database now (admin), paused or disabled as it may be, and answer when it is done.

    A new file of an enabled database is opened here and asks for every proxy to be re-judged (the updater's hook); an
    unchanged one asks for nothing.
    """
    # A short session of its own: no connection is held for the download.
    async with session_factory_of(request)() as session:
        record = await GeoDatabaseRepository(session).get_by_id(database_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Database not found")
    if not record.update_url:
        raise HTTPException(status_code=400, detail="This database has no download URL")
    try:
        refreshed = await geo_updater_of(request).refresh(record)
    except DownloadError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if refreshed.last_update_error:
        raise HTTPException(status_code=502, detail=refreshed.last_update_error)
    return await _row(request, refreshed)


@router.delete("/databases/{database_id}", status_code=204)
async def delete_database(
    request: Request, session: DbDep, database_id: str, admin: RequireAdminDep
) -> None:
    """Remove a stored database everywhere (admin). Config-file databases cannot be removed here."""
    store = geo_database_store_of(request)
    repo = GeoDatabaseRepository(session)
    record = await repo.get_by_id(database_id)
    if record is None:
        if store.is_loaded_from_config(database_id):
            raise HTTPException(
                status_code=400,
                detail="Operator-managed databases are removed from the config file",
            )
        raise HTTPException(status_code=404, detail="Database not found")
    await repo.delete(database_id)
    await session.commit()
    await change_log_of(request).record(ChangeKind.GEO_DATABASE, database_id)
    store.remove(database_id)
    logger.info(
        "IP database deleted", database_id=database_id, name=record.name, admin=admin.username
    )
    if record.enabled:
        await _reattribute_all(request, "database removed")


@router.post("/databases/inspect", response_model=dict[str, Any])
async def inspect_upload(file: UploadFile, _admin: RequireAdminDep) -> dict[str, Any]:
    """Open an uploaded file and report what it is, without storing it (admin)."""
    raw = await file.read()
    if len(raw) > MAX_BLOB_BYTES:
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
    """Resolve one IP with the databases the answering api process has open, optionally against a vendor claim."""
    ip = body.ip.strip()
    if not is_ip(ip):
        raise HTTPException(status_code=400, detail="Not a valid IP address")
    claimed_country = normalize_country(body.claimed_country) if body.claimed_country else None
    if body.claimed_country and claimed_country is None:
        raise HTTPException(status_code=400, detail="claimed_country must be a two-letter ISO code")
    claimed = LocationTarget.reported(claimed_country, body.claimed_state, body.claimed_city)
    service = geo_service_of(request)
    policy = service.default_policy
    if body.project_id:
        project = await require_project(request, body.project_id)
        policy = project.source_policy(policy)
    resolution = service.explain(ip, claimed, policy)
    return LookupResponse(
        ip=ip,
        resolution=resolution,
        policy=policy,
        candidates=[_lookup_candidate(c) for c in resolution.candidates],
        # IP databases only: a GeoNames file answers no IP.
        databases_loaded=sum(
            1 for d in geo_database_store_of(request).loaded if d.kind != GeoDatabaseKind.PLACES
        ),
    )


def _lookup_candidate(candidate: LocationCandidate) -> LookupCandidate:
    return LookupCandidate(
        source=candidate.source.value,
        origin=candidate.origin,
        country=candidate.country,
        location=candidate.location,
    )


@router.post("/reattribute", response_model=ReattributeQueued, status_code=202)
async def reattribute(
    request: Request, body: ReattributeRequest, admin: RequireAdminDep
) -> ReattributeQueued:
    """Ask a worker to re-judge every proxy with a known exit IP, or one connector's (admin).

    Answers 202 with the job's key once it is queued, or 503 when Redis did not take it within the request deadline.
    """
    connector_id = body.connector_id or None
    if connector_id is not None:
        async with session_factory_of(request)() as session:
            if await ConnectorRepository(session).get_by_id(connector_id) is None:
                raise HTTPException(status_code=404, detail="Connector not found")
    key = ReattributeScope(connector_id=connector_id).key
    if not await jobs_of(request).request_now(JobKind.REATTRIBUTE, [key]):
        raise HTTPException(
            status_code=503,
            detail="The job queue cannot be reached; try again",
            headers={"Retry-After": str(UNAVAILABLE_RETRY_AFTER_SECONDS)},
        )
    logger.info("Re-attribution requested", key=key, admin=admin.username)
    return ReattributeQueued(key=key)


# --- observations and accuracy ---------------------------------------------------------------

Verdict = Literal["contradicted", "open", "confirmed", "no_claim"]


def _naive_utc(value: datetime | None) -> datetime | None:
    """A query-string time as the tables store it: UTC without an offset. One without an offset already is."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _count_timed_out() -> HTTPException:
    return HTTPException(
        status_code=504,
        detail=f"Counting took longer than {EXACT_COUNT_TIMEOUT_MS // 1000} s. Narrow the filters first.",
    )


async def _connectors_of(session: AsyncSession, connector_ids: Iterable[str | None]) -> dict[str, Connector]:
    """The connectors that name a page's rows, by id, in one read; a deleted one is absent (its rows show the id)."""
    wanted = {connector_id for connector_id in connector_ids if connector_id}
    return {connector.id: connector for connector in await ConnectorRepository(session).get_many(wanted)}


async def _project_names(session: AsyncSession, project_ids: Iterable[str | None]) -> dict[str, str]:
    """The names of the projects that name a page's rows, by id, in one read; a deleted one is absent."""
    wanted = {project_id for project_id in project_ids if project_id}
    return {project.id: project.name for project in await ProjectRepository(session).get_many(wanted)}


async def _scope(session: AsyncSession, project_id: str | None, connector_id: str | None) -> list[str] | None:
    """The connectors a view is limited to: the one named, a project's, or None for every connector."""
    if connector_id:
        return [connector_id]
    if project_id:
        return [connector.id for connector in await ConnectorRepository(session).get_by_project(project_id)]
    return None


async def _coverage(request: Request, connector_ids: Iterable[str]) -> dict[str, ExitCoverage]:
    """What exit statistics can see of each dynamic-sessions connector among ``connector_ids``; others are absent."""
    return await runtime_view_of(request).exit_coverage(connector_ids)


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
) -> dict[str, Any]:
    """The observation log's filters, from the query string, mirroring the columns of the history table.

    Args:
        verdict: The country badge: ``contradicted``, ``confirmed``, ``open`` (a claim with no verdict) or ``no_claim``.
        conflicts_only: The older spelling of ``verdict=contradicted``.
        until: Where the log starts instead of now; a time without an offset is UTC.
    """
    return {
        "connector_id": connector_id,
        "proxy_id": (proxy_id or "").strip() or None,
        "project_id": project_id,
        "source": source,
        "ip": (ip or "").strip() or None,
        "claimed_country": (claimed_country or "").strip() or None,
        "resolved_country": (resolved_country or "").strip() or None,
        # The country's: the state and city verdicts come per row and move no badge.
        "verdict": verdict,
        "conflicts_only": conflicts_only,
        "until": _naive_utc(until),
    }


ObservationFiltersDep = Annotated[dict[str, Any], Depends(_observation_filters)]


@router.get("/observations", response_model=ObservationsResponse)
async def observations(
    session: DbDep,
    _user: CurrentUserDep,
    filters: ObservationFiltersDep,
    limit: int = 100,
    offset: int = 0,
) -> ObservationsResponse:
    """One page of IP observations, newest first, filtered on the server (:func:`_observation_filters`).

    The total is counted no further than 10,000 rows past the page, so the page stays a short read at any table size;
    ``/observations/count`` is the exact count.
    """
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    rows, total, exact = await ObservationRepository(session).recent(**filters, limit=limit, offset=offset)
    # A deleted connector or project shows its id only.
    connectors = await _connectors_of(session, (row["connector_id"] for row in rows))
    projects = await _project_names(session, (row["project_id"] for row in rows))
    for row in rows:
        connector = connectors.get(row["connector_id"]) if row["connector_id"] else None
        row["connector_name"] = connector.name if connector else None
        row["project_name"] = projects.get(row["project_id"]) if row["project_id"] else None
    return ObservationsResponse(observations=rows, total=total, total_exact=exact, limit=limit, offset=offset)


@router.get("/observations/count", response_model=CountResponse)
async def count_observations(session: DbDep, _user: CurrentUserDep, filters: ObservationFiltersDep) -> CountResponse:
    """Every observation under the log's filters, counted outright: asked for explicitly, never on the page's refresh.

    Under a project, connector, proxy or IP it scans that filter's index, under the other filters alone the whole
    table; past the time limit the count is called off with a 504.
    """
    total = await ObservationRepository(session).count_matching(**filters)
    if total is None:
        raise _count_timed_out()
    return CountResponse(total=total)


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
    since = utc_now() - timedelta(days=max(1, min(days, 3650)))
    rows = await ObservationRepository(session).exit_accuracy(
        connector_ids=await _scope(session, project_id, connector_id), since=since
    )
    connectors = await _connectors_of(session, (row["connector_id"] for row in rows))
    coverage = await _coverage(request, (row["connector_id"] for row in rows))
    result: list[ConnectorAccuracy] = []
    for row in rows:
        connector = connectors.get(row["connector_id"])
        result.append(
            ConnectorAccuracy(
                connector_id=row["connector_id"],
                connector_name=connector.name if connector else None,
                project_id=connector.project_id if connector else None,
                coverage=coverage.get(row["connector_id"]),
                exits=row["exits"],
                country=LevelAccuracy.from_counts(row["country"]),
                state=LevelAccuracy.from_counts(row["state"]),
                city=LevelAccuracy.from_counts(row["city"]),
                breakdown=row["breakdown"],
            )
        )
    result.sort(key=lambda c: (-c.country.claimed, c.connector_name or ""))
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
    since = utc_now() - timedelta(days=max(1, min(days, 3650)))
    rows = await ObservationRepository(session).exit_summary(
        connector_ids=await _scope(session, project_id, connector_id), since=since
    )
    connectors = await _connectors_of(session, (row["connector_id"] for row in rows))
    coverage = await _coverage(request, (row["connector_id"] for row in rows))
    result = []
    for row in rows:
        connector = connectors.get(row["connector_id"])
        result.append(
            ConnectorExits(
                connector_name=connector.name if connector else None,
                project_id=connector.project_id if connector else None,
                coverage=coverage.get(row["connector_id"]),
                **row,
            )
        )
    result.sort(key=lambda c: (-c.unique_total, c.connector_name or ""))
    return ExitsResponse(since=since, connectors=result)


async def _exit_ip_filters(
    session: DbDep,
    project_id: str | None = None,
    connector_id: str | None = None,
    ip: str | None = None,
    proxy_id: str | None = None,
    country: str | None = None,
    claimed_country: str | None = None,
    verdict: Verdict | None = None,
) -> dict[str, Any]:
    """The exit IPs view's filters, from the query string; a project becomes its connectors."""
    return {
        "connector_ids": await _scope(session, project_id, connector_id),
        "ip": (ip or "").strip() or None,
        "proxy_id": (proxy_id or "").strip() or None,
        "country": (country or "").strip() or None,
        "claimed_country": (claimed_country or "").strip() or None,
        "verdict": verdict,
    }


ExitIpFiltersDep = Annotated[dict[str, Any], Depends(_exit_ip_filters)]


@router.get("/exits/ips", response_model=ExitIpsResponse)
async def exit_ips(
    session: DbDep,
    _user: CurrentUserDep,
    filters: ExitIpFiltersDep,
    limit: int = 100,
    offset: int = 0,
) -> ExitIpsResponse:
    """One page of distinct exit IPs with their latest state, most recently seen first: a row per connector and IP.

    Filters apply on the server, ``verdict`` is the country verdict, and the total is counted as for observations.
    """
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    rows, total, exact = await ObservationRepository(session).exit_ips(**filters, limit=limit, offset=offset)
    connectors = await _connectors_of(session, (row["connector_id"] for row in rows))
    projects = await _project_names(session, (connector.project_id for connector in connectors.values()))
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
    return ExitIpsResponse(ips=ips, total=total, total_exact=exact, limit=limit, offset=offset)


@router.get("/exits/ips/count", response_model=CountResponse)
async def count_exit_ips(session: DbDep, _user: CurrentUserDep, filters: ExitIpFiltersDep) -> CountResponse:
    """Every exit IP under the view's filters, counted outright, as for observations."""
    total = await ObservationRepository(session).count_exit_ips(**filters)
    if total is None:
        raise _count_timed_out()
    return CountResponse(total=total)


@router.get("/status", response_model=GeoStatusResponse)
async def status(request: Request, session: DbDep, _user: CurrentUserDep) -> GeoStatusResponse:
    """Pipeline health for the settings page: this api process, the observations stream, and every live gateway."""
    store = geo_database_store_of(request)
    records = await GeoDatabaseRepository(session).get_all()
    stored = await ObservationRepository(session).estimated_count()
    recorder = observation_recorder_of(request)
    gateways = [_gateway_status(instance_id, gateway) for instance_id, gateway in (await _gateways(request)).items()]
    return GeoStatusResponse(
        databases_loaded=len(store.loaded),
        databases_total=len(records) + len(_path_databases(store, {r.id for r in records})),
        load_errors=store.load_errors,
        policy_from_database=geo_settings_store_of(request).from_database,
        pending_observations=recorder.pending,
        published_observations=recorder.published,
        dropped_observations=recorder.dropped,
        queued_observations=await recorder.queued(),
        stored_observations=stored,
        preflight_checks=sum(g.preflight_checks for g in gateways),
        preflight_rejections=sum(g.preflight_rejections for g in gateways),
        gateways=gateways,
    )


def _gateway_status(instance_id: str, gateway: GatewaySnapshot) -> GatewayGeoStatus:
    """One gateway's attribution, from its heartbeat."""
    return GatewayGeoStatus(
        instance_id=instance_id,
        databases_loaded=len(gateway.geo_databases_loaded),
        databases_missing=gateway.geo_databases_missing,
        load_errors=gateway.geo_database_errors,
        preflight_checks=gateway.preflight_checks,
        preflight_rejections=gateway.preflight_rejections,
        observations_pending=gateway.observations_pending,
        observations_dropped=gateway.observations_dropped,
        sightings_pending=gateway.sightings_pending,
        sightings_dropped=gateway.sightings_dropped,
    )
