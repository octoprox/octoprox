# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy management endpoints, served by the api role: writes through :class:`~api.services.proxies.ProxyService`, reads
from :class:`~api.services.read_model.RuntimeView`, and Detect (``/locate``) through the proxy from this process
(:class:`~api.services.detect.DetectService`).
"""

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from api.core.auth import RequireEditorDep
from api.models.proxy import (
    ProxyCreate,
    ProxyListResponse,
    ProxyResponse,
    ProxyUpdate,
    ProxyUploadResponse,
)
from api.routes.common import (
    actor_of,
    detect_service_of,
    proxy_service_of,
    require_project,
    runtime_view_of,
)

router = APIRouter(prefix="/projects/{project_id}/proxies")


async def _describe(request: Request, project_id: str, proxy_id: str) -> ProxyResponse:
    """A proxy of the project as the API shows it; 404 when the project has no such proxy."""
    view = await runtime_view_of(request).proxy(project_id, proxy_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Proxy not found")
    return ProxyResponse.of(view)


@router.get("", response_model=ProxyListResponse)
async def list_proxies(request: Request, project_id: str) -> ProxyListResponse:
    """Every proxy of the project, disabled connectors' included, in the stored order ``(created_at, id)``, the same
    on every api.

    ``healthy`` counts what requests can be routed to: healthy and not
    quarantined, on an enabled connector its traffic limit does not hold back.
    """
    await require_project(request, project_id)
    return ProxyListResponse.of(await runtime_view_of(request).proxies(project_id))


@router.post("", response_model=ProxyResponse, status_code=201)
async def create_proxy(
    request: Request, proxy_data: ProxyCreate, project_id: str, user: RequireEditorDep
) -> ProxyResponse:
    """Add a new proxy to the pool of a STATIC_PROXY_PROVIDER connector, the only kind that takes one.

    Its first health check is asked for at once and, unless ``country`` is
    given, a lookup of its exit IP and country through it (the ``enrich``
    job, with the ``proxy.geo_lookup`` settings and the attribution echo
    endpoint); the response waits for neither.
    """
    proxy = await proxy_service_of(request).create(project_id, proxy_data, actor_of(request, user))
    return await _describe(request, project_id, proxy.id)


@router.post("/upload", response_model=ProxyUploadResponse, status_code=201)
async def upload_proxies(
    request: Request,
    project_id: str,
    file: Annotated[UploadFile, File(...)],
    connector_id: Annotated[str, Form(...)],
    user: RequireEditorDep,
) -> ProxyUploadResponse:
    """Upload proxies from a file of one URL per line, ``protocol://[username:password@]host:port`` with protocol http, https, socks4 or socks5.

    Lines that do not parse are reported and skipped; the rest are added in one transaction, so a refused upload (a
    connector that is not static, say) adds nothing.
    """
    try:
        text = (await file.read()).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded") from exc
    return ProxyUploadResponse.of(
        await proxy_service_of(request).upload(project_id, connector_id, text, actor_of(request, user))
    )


@router.get("/{proxy_id}", response_model=ProxyResponse)
async def get_proxy(request: Request, project_id: str, proxy_id: str) -> ProxyResponse:
    """Get a specific proxy of the project by ID."""
    return await _describe(request, project_id, proxy_id)


@router.patch("/{proxy_id}", response_model=ProxyResponse)
async def update_proxy(
    request: Request, project_id: str, proxy_id: str, proxy_data: ProxyUpdate, user: RequireEditorDep
) -> ProxyResponse:
    """Update a proxy's definition; its status is the health checks' to move, never an edit's.

    A country, state or city set here is pinned as what routing matches the
    proxy on (an empty string unpins it), ahead of what attribution finds.
    """
    await proxy_service_of(request).update(project_id, proxy_id, proxy_data, actor_of(request, user))
    return await _describe(request, project_id, proxy_id)


@router.delete("/{proxy_id}", status_code=204)
async def delete_proxy(request: Request, project_id: str, proxy_id: str, user: RequireEditorDep) -> None:
    """Remove a proxy from the pool: a cloud provider's (AWS, GCP, Azure) is marked TERMINATING and the scale job
    terminates its instance, then deletes the row.

    Any other proxy is removed at once, and a sync of its connector brings a vendor slot back: a session slot as a
    fresh session, a list entry, port or gateway under its old ID, with what is still kept against that ID.
    """
    await proxy_service_of(request).remove(project_id, proxy_id, actor_of(request, user))


@router.post("/{proxy_id}/locate", response_model=ProxyResponse)
async def locate_proxy(
    request: Request, project_id: str, proxy_id: str, _guard: RequireEditorDep
) -> ProxyResponse:
    """Look up a proxy's exit IP and country now by requesting through it, record it on the proxy and return it; 502
    when the request fails.

    A session slot the project's location rules refuse is rotated, as any
    sighting of it would; the answer still shows what was found.
    """
    return ProxyResponse.of(await detect_service_of(request).locate(project_id, proxy_id))


@router.post("/{proxy_id}/unquarantine", status_code=200)
async def unquarantine_proxy(
    request: Request, project_id: str, proxy_id: str, _guard: RequireEditorDep
) -> dict[str, str]:
    """Forcefully remove a proxy from quarantine and empty its rate-limit window.

    Gateways route to it again once the controller publishes the change,
    within a couple of seconds.
    """
    await proxy_service_of(request).unquarantine(project_id, proxy_id)
    return {"status": "ok", "proxy_id": proxy_id}
