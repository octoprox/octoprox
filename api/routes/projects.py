# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Project management endpoints.

Writes go through :class:`~api.services.projects.ProjectService`, reads
through :class:`~api.services.read_model.RuntimeView`, and the Connect page
previews routing with the gateway's own router over a compile of the project
(``ApiRuntime.preview_router``).
"""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.core.auth import RequireEditorDep
from api.core.username_params import (
    CITY_SEPARATOR,
    COUNTRY_SEPARATOR,
    SESSID_SEPARATOR,
    STATE_SEPARATOR,
    format_username_params,
    parse_username_params,
)
from api.models.connect import ConnectEndpoint, ConnectInfo, ConnectLocation
from api.models.project import (
    Project,
    ProjectCreate,
    ProjectResponse,
    ProjectSummary,
    ProjectUpdate,
)
from api.routes.common import (
    api_runtime_of,
    project_service_of,
    require_project,
    runtime_view_of,
    settings_of,
)
from api.services.read_model import ProjectView

router = APIRouter(prefix="/projects")

_SEPARATORS = (COUNTRY_SEPARATOR, STATE_SEPARATOR, CITY_SEPARATOR, SESSID_SEPARATOR)
# Every reserved delimiter in a string, overlapping ones included: "-sessid-st-ny" holds two.
_DELIMITERS = re.compile("(?=" + "|".join(re.escape(separator) for separator in _SEPARATORS) + ")")
# What a delete must be confirmed with, typed by the user.
_DELETE_CONFIRMATION = "permanently delete"


class ProjectListResponse(BaseModel):
    """Response for listing projects."""
    total: int
    projects: list[ProjectSummary]


class DeleteConfirmation(BaseModel):
    """Request body for delete confirmation."""
    confirmation: str


def _project_to_response(
    project: Project,
    credential_count: int = 0,
    connector_count: int = 0,
    proxy_count: int = 0,
    healthy_proxy_count: int = 0,
) -> ProjectResponse:
    """Convert a Project to ProjectResponse."""
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        username=project.username,
        password=project.password,
        routing_strategy=project.routing_strategy,
        health_check_interval=project.health_check_interval,
        health_check_timeout=project.health_check_timeout,
        connection_timeout=project.connection_timeout,
        max_retries=project.max_retries,
        tls_mitm_mode=project.tls_mitm_mode,
        tls_mitm_engine=project.tls_mitm_engine,
        tls_mitm_browser=project.tls_mitm_browser,
        metrics_retention_days=project.metrics_retention_days,
        location_contradicted=project.location_contradicted,
        location_unknown=project.location_unknown,
        location_preflight=project.location_preflight,
        location_conflict_rule=project.location_conflict_rule,
        location_city_radius_km=project.location_city_radius_km,
        exit_headers=project.exit_headers,
        created_at=project.created_at,
        updated_at=project.updated_at,
        credential_count=credential_count,
        connector_count=connector_count,
        proxy_count=proxy_count,
        healthy_proxy_count=healthy_proxy_count,
    )


def _view_to_response(view: ProjectView) -> ProjectResponse:
    """A project with its counts; the proxy counts are its pool's (enabled connectors no traffic limit blocks)."""
    return _project_to_response(
        view.project,
        credential_count=view.credentials,
        connector_count=view.connectors,
        proxy_count=view.pool.total,
        healthy_proxy_count=view.pool.healthy,
    )


def _view_to_summary(view: ProjectView) -> ProjectSummary:
    """Convert a project with its counts to ProjectSummary."""
    project = view.project
    return ProjectSummary(
        id=project.id,
        name=project.name,
        description=project.description,
        username=project.username,
        password=project.password,
        routing_strategy=project.routing_strategy,
        tls_mitm_mode=project.tls_mitm_mode,
        tls_mitm_engine=project.tls_mitm_engine,
        tls_mitm_browser=project.tls_mitm_browser,
        location_contradicted=project.location_contradicted,
        location_unknown=project.location_unknown,
        location_preflight=project.location_preflight,
        location_conflict_rule=project.location_conflict_rule,
        location_city_radius_km=project.location_city_radius_km,
        exit_headers=project.exit_headers,
        credential_count=view.credentials,
        connector_count=view.connectors,
        proxy_count=view.pool.total,
        healthy_proxy_count=view.pool.healthy,
        created_at=project.created_at,
    )


async def _project_with_counts(request: Request, project_id: str) -> ProjectView:
    """The project with its counts; 404 when there is no such project."""
    view = await runtime_view_of(request).project(project_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return view


@router.get("/ca-certificate")
async def download_ca_certificate(request: Request) -> FileResponse:
    """Download the MITM CA certificate for client trust store installation.

    The api reads only the certificate, from the CA volume the gateways
    share; until a gateway has generated it there is nothing to download.
    """
    ca_cert_path = Path(settings_of(request).tls_mitm_ca_cert_path)
    if not ca_cert_path.exists():
        raise HTTPException(status_code=404, detail="CA certificate not found")

    return FileResponse(
        path=ca_cert_path,
        media_type="application/x-pem-file",
        filename="octoprox-ca.crt",
    )


@router.get("", response_model=ProjectListResponse)
async def list_projects(request: Request) -> ProjectListResponse:
    """List all projects, oldest first."""
    summaries = [_view_to_summary(view) for view in await runtime_view_of(request).projects()]
    return ProjectListResponse(total=len(summaries), projects=summaries)


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(
    request: Request, project_data: ProjectCreate, _guard: RequireEditorDep
) -> ProjectResponse:
    """Create a new project; a name or username another project holds is a 400."""
    project = await project_service_of(request).create(project_data)
    return _project_to_response(project)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(request: Request, project_id: str) -> ProjectResponse:
    """Get a specific project by ID."""
    return _view_to_response(await _project_with_counts(request, project_id))


@router.get("/{project_id}/connect", response_model=ConnectInfo)
async def get_connect_info(
    request: Request,
    project_id: str,
    country: str | None = None,
    state: str | None = None,
    city: str | None = None,
    sessid: str | None = None,
) -> ConnectInfo:
    """How a client reaches the project's proxy, for the place and session it asks for.

    The username composed from the project's and the parameters is read by the
    parser the proxy listener uses, so the normalised form and any refusal are
    exactly what a client would get, and ``routes`` lists the connectors a
    gateway routing on the project's current rows would spread it over.
    """
    project = await require_project(request, project_id)

    values = [
        (separator, value.strip())
        for separator, value in zip(_SEPARATORS, (country, state, city, sessid), strict=True)
        if value and value.strip()
    ]
    composed = project.username + "".join(f"{separator}{value}" for separator, value in values)
    params = parse_username_params(composed)
    error = params.location_error
    if params.username != project.username:
        error = "The project username contains a reserved delimiter (-cc-, -st-, -city- or -sessid-), so the proxy cannot read it"
    elif len(_DELIMITERS.findall(composed, len(project.username))) != len(values):
        # Only the delimiters put there may follow the username: a value holding one, or
        # forming one with the dash before or after it ("st-ny" after -sessid- reads as
        # -st-ny), would be read as another parameter.
        error = "A value cannot contain -cc-, -st-, -city- or -sessid-, including one formed with the dash before or after it"
    elif params.sessid and ":" in params.sessid:
        # Basic auth splits user and password at the first colon.
        error = "A session ID cannot contain a colon"
    location = params.location
    preview = await api_runtime_of(request).preview_router(project_id)
    countries, any_country, places = preview.served_places(project_id)

    scheme, host, port = settings_of(request).proxy_endpoint

    return ConnectInfo(
        endpoint=ConnectEndpoint(scheme=scheme, host=host, port=port),
        username=composed if error else format_username_params(project.username, params.sessid, location),
        sessid=params.sessid,
        location=ConnectLocation(country=location.country, state=location.state, city=location.city) if location else None,
        error=error,
        routes=[] if error else preview.route_preview(project_id, location),
        countries=countries,
        any_country=any_country,
        places=places,
    )


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    request: Request, project_id: str, project_data: ProjectUpdate, _guard: RequireEditorDep
) -> ProjectResponse:
    """Update a project.

    An edit to how locations are judged asks a worker to re-judge the
    project's proxies; the response does not wait for that.
    """
    await project_service_of(request).update(project_id, project_data)
    return _view_to_response(await _project_with_counts(request, project_id))


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    request: Request, project_id: str, body: DeleteConfirmation, _guard: RequireEditorDep
) -> None:
    """Delete a project.

    Requires confirmation by typing "permanently delete" in the request body.
    This will cascade delete all sources and proxies associated with the project.
    """
    if body.confirmation != _DELETE_CONFIRMATION:
        raise HTTPException(
            status_code=400,
            detail="Confirmation required. Please set confirmation to 'permanently delete'",
        )
    await project_service_of(request).delete(project_id)
