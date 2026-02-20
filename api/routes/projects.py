# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Project management endpoints."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api.models.project import (
    Project,
    ProjectCreate,
    ProjectResponse,
    ProjectSummary,
    ProjectUpdate,
)

router = APIRouter(prefix="/projects")


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
        created_at=project.created_at,
        updated_at=project.updated_at,
        credential_count=credential_count,
        connector_count=connector_count,
        proxy_count=proxy_count,
        healthy_proxy_count=healthy_proxy_count,
    )


def _project_to_summary(
    project: Project,
    credential_count: int = 0,
    connector_count: int = 0,
    proxy_count: int = 0,
    healthy_proxy_count: int = 0,
) -> ProjectSummary:
    """Convert a Project to ProjectSummary."""
    return ProjectSummary(
        id=project.id,
        name=project.name,
        description=project.description,
        username=project.username,
        password=project.password,
        routing_strategy=project.routing_strategy,
        credential_count=credential_count,
        connector_count=connector_count,
        proxy_count=proxy_count,
        healthy_proxy_count=healthy_proxy_count,
        created_at=project.created_at,
    )


@router.get("", response_model=ProjectListResponse)
async def list_projects(request: Request) -> ProjectListResponse:
    """List all projects."""
    proxy_manager = request.app.state.proxy_manager
    projects = proxy_manager.projects

    summaries = []
    for project in projects:
        credentials = proxy_manager.get_credentials_for_project(project.id)
        connectors = proxy_manager.get_connectors_for_project(project.id)
        proxies = proxy_manager.get_proxies_for_project(project.id)
        healthy_proxies = proxy_manager.get_healthy_proxies_for_project(project.id)
        summaries.append(_project_to_summary(
            project,
            credential_count=len(credentials),
            connector_count=len(connectors),
            proxy_count=len(proxies),
            healthy_proxy_count=len(healthy_proxies),
        ))

    return ProjectListResponse(
        total=len(summaries),
        projects=summaries,
    )


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(request: Request, project_data: ProjectCreate) -> ProjectResponse:
    """Create a new project."""
    proxy_manager = request.app.state.proxy_manager

    # Check for duplicate username
    existing = proxy_manager.get_project_by_username(project_data.username)
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Project with username '{project_data.username}' already exists",
        )

    project = Project(
        name=project_data.name,
        description=project_data.description,
        username=project_data.username,
        password=project_data.password,
        routing_strategy=project_data.routing_strategy,
        health_check_interval=project_data.health_check_interval,
        health_check_timeout=project_data.health_check_timeout,
        connection_timeout=project_data.connection_timeout,
        max_retries=project_data.max_retries,
    )

    await proxy_manager.add_project(project)
    return _project_to_response(project)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(request: Request, project_id: str) -> ProjectResponse:
    """Get a specific project by ID."""
    proxy_manager = request.app.state.proxy_manager
    project = proxy_manager.get_project(project_id)

    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    credentials = proxy_manager.get_credentials_for_project(project_id)
    connectors = proxy_manager.get_connectors_for_project(project_id)
    proxies = proxy_manager.get_proxies_for_project(project_id)
    healthy_proxies = proxy_manager.get_healthy_proxies_for_project(project_id)

    return _project_to_response(
        project,
        credential_count=len(credentials),
        connector_count=len(connectors),
        proxy_count=len(proxies),
        healthy_proxy_count=len(healthy_proxies),
    )


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    request: Request, project_id: str, project_data: ProjectUpdate
) -> ProjectResponse:
    """Update a project."""
    proxy_manager = request.app.state.proxy_manager
    project = proxy_manager.get_project(project_id)

    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # Check for duplicate username if changing
    if project_data.username is not None and project_data.username != project.username:
        existing = proxy_manager.get_project_by_username(project_data.username)
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Project with username '{project_data.username}' already exists",
            )

    # Update fields
    if project_data.name is not None:
        project.name = project_data.name
    if project_data.description is not None:
        project.description = project_data.description
    if project_data.username is not None:
        project.username = project_data.username
    if project_data.password is not None:
        project.password = project_data.password
    if project_data.routing_strategy is not None:
        project.routing_strategy = project_data.routing_strategy
    if project_data.health_check_interval is not None:
        project.health_check_interval = project_data.health_check_interval
    if project_data.health_check_timeout is not None:
        project.health_check_timeout = project_data.health_check_timeout
    if project_data.connection_timeout is not None:
        project.connection_timeout = project_data.connection_timeout
    if project_data.max_retries is not None:
        project.max_retries = project_data.max_retries

    await proxy_manager.update_project(project)

    credentials = proxy_manager.get_credentials_for_project(project_id)
    connectors = proxy_manager.get_connectors_for_project(project_id)
    proxies = proxy_manager.get_proxies_for_project(project_id)
    healthy_proxies = proxy_manager.get_healthy_proxies_for_project(project_id)

    return _project_to_response(
        project,
        credential_count=len(credentials),
        connector_count=len(connectors),
        proxy_count=len(proxies),
        healthy_proxy_count=len(healthy_proxies),
    )


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    request: Request, project_id: str, body: DeleteConfirmation
) -> None:
    """Delete a project.

    Requires confirmation by typing "permanently delete" in the request body.
    This will cascade delete all sources and proxies associated with the project.
    """
    proxy_manager = request.app.state.proxy_manager

    # Verify confirmation
    if body.confirmation != "permanently delete":
        raise HTTPException(
            status_code=400,
            detail="Confirmation required. Please set confirmation to 'permanently delete'",
        )

    project = proxy_manager.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    if not await proxy_manager.remove_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
