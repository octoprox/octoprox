# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy management endpoints, served by the api role: writes through :class:`~api.services.proxies.ProxyService`, reads
from :class:`~api.services.read_model.RuntimeView`, and Detect (``/locate``) through the proxy from this process.
"""

from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from api.core.auth import RequireEditorDep
from api.core.stats import MetricDelta
from api.db.repository import ConnectorRepository
from api.geo.models import (
    META_CITY_VERDICT,
    META_COUNTRY_SOURCE,
    META_LOCATION,
    META_LOCATION_CONFLICT,
    META_LOCATION_VERDICT,
    META_STATE_VERDICT,
    META_VENDOR_COUNTRY,
    ObservationSource,
)
from api.geo.service import GeoService
from api.models.connector import Connector
from api.models.proxy import Proxy, ProxyCreate, ProxyResponse, ProxyUpdate
from api.routes.common import (
    attribution_of,
    proxy_service_of,
    require_project,
    runtime_view_of,
    session_factory_of,
)
from api.routes.metrics import success_rate
from api.services.read_model import ProxyView

router = APIRouter(prefix="/projects/{project_id}/proxies")


class ProxyListResponse(BaseModel):
    """Response for listing proxies."""
    total: int
    healthy: int
    proxies: list[ProxyResponse]


class ProxyUploadError(BaseModel):
    """Error details for a failed proxy upload line."""
    line_number: int
    line: str
    error: str


class ProxyUploadResponse(BaseModel):
    """Response for proxy upload endpoint."""
    total_lines: int
    successful: int
    failed: int
    proxies: list[ProxyResponse]
    errors: list[ProxyUploadError]


def _proxy_to_response(view: ProxyView) -> ProxyResponse:
    """A proxy as the API shows it: the stored row and its connector, its counters and its quarantine."""
    proxy, connector, counters = view.proxy, view.connector, view.counters
    location = proxy.metadata.get(META_LOCATION)
    return ProxyResponse(
        id=proxy.id,
        host=proxy.host,
        port=proxy.port,
        protocol=proxy.protocol.value,
        username=proxy.username,
        password=proxy.password,
        display_host=proxy.effective_display_host,
        connector_id=proxy.connector_id,
        connector_name=connector.name if connector else None,
        connector_enabled=connector.enabled if connector else True,
        status=proxy.status.value,
        request_count=counters.request_count,
        success_count=counters.success_count,
        failure_count=counters.failure_count,
        success_rate=success_rate(counters),
        avg_latency_ms=counters.avg_latency_ms,
        bytes_sent=counters.bytes_sent,
        bytes_received=counters.bytes_received,
        quarantined=view.quarantined,
        quarantine_remaining_seconds=round(view.quarantine_remaining_seconds, 1),
        country=proxy.country,
        country_source=proxy.metadata.get(META_COUNTRY_SOURCE),
        vendor_country=proxy.metadata.get(META_VENDOR_COUNTRY),
        location_conflict=bool(proxy.metadata.get(META_LOCATION_CONFLICT)),
        location_verdict=proxy.metadata.get(META_LOCATION_VERDICT),
        state_verdict=proxy.metadata.get(META_STATE_VERDICT),
        city_verdict=proxy.metadata.get(META_CITY_VERDICT),
        outside_declared_countries=GeoService.outside_declared_countries(
            proxy, connector.countries if connector else []
        ),
        location=location if isinstance(location, dict) else None,
        state_code=proxy.state_code,
        state_source=proxy.state_source,
        city=proxy.city_slug,
        city_source=proxy.city_source,
        tags=proxy.tags,
        created_at=proxy.created_at,
    )


def _fresh(proxy: Proxy, connector: Connector | None) -> ProxyView:
    """A row added a moment ago as the read model would show it: nothing counted, no quarantine, never checked."""
    return ProxyView(
        proxy=proxy, connector=connector, counters=MetricDelta(), health=None, quarantine_remaining_seconds=0.0
    )


async def _describe(request: Request, project_id: str, proxy_id: str) -> ProxyResponse:
    """A proxy of the project as the API shows it; 404 when the project has no such proxy."""
    view = await runtime_view_of(request).proxy(project_id, proxy_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Proxy not found")
    return _proxy_to_response(view)


@router.get("", response_model=ProxyListResponse)
async def list_proxies(request: Request, project_id: str) -> ProxyListResponse:
    """Every proxy of the project, disabled connectors' included, in the stored order ``(created_at, id)``, the same
    on every api.

    ``healthy`` counts what requests can be routed to: healthy and not
    quarantined, on an enabled connector its traffic limit does not hold back.
    """
    await require_project(request, project_id)
    listing = await runtime_view_of(request).proxies(project_id)
    return ProxyListResponse(
        total=len(listing.proxies),
        healthy=listing.healthy,
        proxies=[_proxy_to_response(view) for view in listing.proxies],
    )


@router.post("", response_model=ProxyResponse, status_code=201)
async def create_proxy(
    request: Request, proxy_data: ProxyCreate, project_id: str, _guard: RequireEditorDep
) -> ProxyResponse:
    """Add a new proxy to the pool of a STATIC_PROXY_PROVIDER connector, the only kind that takes one.

    Its first health check is asked for at once and, unless ``country`` is
    given, a lookup of its exit IP and country through it (the ``enrich``
    job, with the ``proxy.geo_lookup`` settings and the attribution echo
    endpoint); the response waits for neither.
    """
    proxy = await proxy_service_of(request).create(project_id, proxy_data)
    return await _describe(request, project_id, proxy.id)


@router.post("/upload", response_model=ProxyUploadResponse, status_code=201)
async def upload_proxies(
    request: Request,
    project_id: str,
    file: Annotated[UploadFile, File(...)],
    connector_id: Annotated[str, Form(...)],
    _guard: RequireEditorDep,
) -> ProxyUploadResponse:
    """Upload proxies from a file of one URL per line, ``protocol://[username:password@]host:port`` with protocol http, https, socks4 or socks5.

    Lines that do not parse are reported and skipped; the rest are added in one transaction, so a refused upload (a
    connector that is not static, say) adds nothing.
    """
    try:
        text = (await file.read()).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded") from exc
    upload = await proxy_service_of(request).upload(project_id, connector_id, text)
    async with session_factory_of(request)() as session:
        connector = await ConnectorRepository(session).get_in_project(project_id, connector_id)
    return ProxyUploadResponse(
        total_lines=upload.total_lines,
        successful=len(upload.added),
        failed=len(upload.errors),
        proxies=[_proxy_to_response(_fresh(proxy, connector)) for proxy in upload.added],
        errors=[
            ProxyUploadError(line_number=error.line_number, line=error.line, error=error.error)
            for error in upload.errors
        ],
    )


@router.get("/{proxy_id}", response_model=ProxyResponse)
async def get_proxy(request: Request, project_id: str, proxy_id: str) -> ProxyResponse:
    """Get a specific proxy of the project by ID."""
    return await _describe(request, project_id, proxy_id)


@router.patch("/{proxy_id}", response_model=ProxyResponse)
async def update_proxy(
    request: Request, project_id: str, proxy_id: str, proxy_data: ProxyUpdate, _guard: RequireEditorDep
) -> ProxyResponse:
    """Update a proxy's definition; its status is the health checks' to move, never an edit's.

    A country, state or city set here is pinned as what routing matches the
    proxy on (an empty string unpins it), ahead of what attribution finds.
    """
    await proxy_service_of(request).update(project_id, proxy_id, proxy_data)
    return await _describe(request, project_id, proxy_id)


@router.delete("/{proxy_id}", status_code=204)
async def delete_proxy(request: Request, project_id: str, proxy_id: str, _guard: RequireEditorDep) -> None:
    """Remove a proxy from the pool: a cloud provider's (AWS, GCP, Azure) is marked TERMINATING and the scale job
    terminates its instance, then deletes the row.

    Any other proxy is removed at once; a vendor slot is then brought back
    fresh by a sync of its connector.
    """
    await proxy_service_of(request).remove(project_id, proxy_id)


@router.post("/{proxy_id}/locate", response_model=ProxyResponse)
async def locate_proxy(
    request: Request, project_id: str, proxy_id: str, _guard: RequireEditorDep
) -> ProxyResponse:
    """Look up a proxy's exit IP and country now by requesting through it, record it on the proxy and return it; 502
    when the request fails.

    A session slot the project's location rules refuse is rotated, as any
    sighting of it would; the answer still shows what was found.
    """
    view = await runtime_view_of(request).proxy(project_id, proxy_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Proxy not found")
    attribution = attribution_of(request)
    store = attribution.store()
    await store.load([proxy_id])
    judged = await attribution.attributor.enrich(store, proxy_id, source=ObservationSource.MANUAL)
    await store.flush()
    if judged is None:
        raise HTTPException(status_code=502, detail="Could not determine the exit location through this proxy")
    located = await runtime_view_of(request).proxy(project_id, proxy_id)
    if located is None:
        # Rotated: the stored row is gone, so show the judgement on the row as it was
        # (the judged copy carries its credentials filled in, never shown).
        located = replace(view, proxy=view.proxy.model_copy(update={"metadata": judged.metadata}))
    return _proxy_to_response(located)


@router.post("/{proxy_id}/unquarantine", status_code=200)
async def unquarantine_proxy(
    request: Request, project_id: str, proxy_id: str, _guard: RequireEditorDep
) -> dict[str, str]:
    """Forcefully remove a proxy from quarantine and empty its rate-limit window.

    Gateways route to it again once the controller publishes the change,
    within a couple of seconds.
    """
    if not await proxy_service_of(request).unquarantine(project_id, proxy_id):
        raise HTTPException(status_code=400, detail="Proxy is not quarantined")
    return {"status": "ok", "proxy_id": proxy_id}
