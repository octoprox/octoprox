# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Project management endpoints.

Writes go through :class:`~api.services.projects.ProjectService`, reads
through :class:`~api.services.read_model.RuntimeView`, the Connect page
through :class:`~api.services.connect.ConnectService`, which previews routing
with the gateway's own router over a compile of the project, and the CA
certificate through :class:`~api.services.mitm.MitmService`.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.core.auth import RequireEditorDep
from api.models.connect import ConnectInfo
from api.models.project import (
    LOAD_RANGES,
    ProjectCreate,
    ProjectListResponse,
    ProjectLoadResponse,
    ProjectResponse,
    ProjectUpdate,
    ProjectView,
)
from api.routes.common import (
    actor_of,
    connect_service_of,
    mitm_service_of,
    project_service_of,
    runtime_view_of,
)

router = APIRouter(prefix="/projects")

# What a delete must be confirmed with, typed by the user.
_DELETE_CONFIRMATION = "permanently delete"


class DeleteConfirmation(BaseModel):
    """Request body for delete confirmation."""
    confirmation: str


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
    return FileResponse(
        path=mitm_service_of(request).ca_certificate(),
        media_type="application/x-pem-file",
        filename="octoprox-ca.crt",
    )


@router.get("", response_model=ProjectListResponse)
async def list_projects(request: Request) -> ProjectListResponse:
    """List all projects, oldest first."""
    return ProjectListResponse.of(await runtime_view_of(request).projects())


@router.get("/load", response_model=ProjectLoadResponse)
async def get_projects_load(
    request: Request,
    range: Literal["1h", "6h", "24h", "7d", "30d"] = Query("24h", alias="range"),
) -> ProjectLoadResponse:
    """Every project's request rate now and its requests, outcomes and bytes over ``range``, oldest first."""
    span, bucket_seconds = LOAD_RANGES[range]
    loads = await runtime_view_of(request).load(int(span.total_seconds()) // bucket_seconds, bucket_seconds)
    return ProjectLoadResponse.of(range, bucket_seconds, loads)


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(
    request: Request, project_data: ProjectCreate, user: RequireEditorDep
) -> ProjectResponse:
    """Create a new project; a name or username another project holds is a 400."""
    project = await project_service_of(request).create(project_data, actor_of(request, user))
    return ProjectResponse.of(project)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(request: Request, project_id: str) -> ProjectResponse:
    """Get a specific project by ID."""
    return ProjectResponse.of_view(await _project_with_counts(request, project_id))


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
    return await connect_service_of(request).info(project_id, country, state, city, sessid)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    request: Request, project_id: str, project_data: ProjectUpdate, user: RequireEditorDep
) -> ProjectResponse:
    """Update a project.

    An edit to how locations are judged asks a worker to re-judge the
    project's proxies; the response does not wait for that.
    """
    await project_service_of(request).update(project_id, project_data, actor_of(request, user))
    return ProjectResponse.of_view(await _project_with_counts(request, project_id))


@router.delete("/{project_id}", status_code=204, responses={202: {"description": "Deleting: its cloud instances go first"}})
async def delete_project(
    request: Request, project_id: str, body: DeleteConfirmation, user: RequireEditorDep
) -> Response:
    """Delete a project with everything it holds: 204 when it is gone, 202 while its cloud instances are terminated first.

    Requires confirmation by typing "permanently delete" in the request body. Until it goes, the project is listed with
    ``pending_deletion`` set, its clients are turned away and it takes no new connector.
    """
    if body.confirmation != _DELETE_CONFIRMATION:
        raise HTTPException(
            status_code=400,
            detail="Confirmation required. Please set confirmation to 'permanently delete'",
        )
    gone = await project_service_of(request).delete(project_id, actor_of(request, user))
    return Response(status_code=204 if gone else 202)
