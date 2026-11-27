# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""MITM traffic inspection endpoints (the Inspector page), served by the api role.

They page through, newest first, and clear the project's capped capture stream
in Redis (``mitm:requests:{project_id}``), which the intercepting gateways
append to; clearing is an edit, so it needs an editor. Thin over
:class:`~api.services.mitm.MitmService`.
"""

from fastapi import APIRouter, Query, Request

from api.core.auth import RequireEditorDep
from api.models.mitm import MitmRequestsResponse
from api.routes.common import mitm_service_of

router = APIRouter(prefix="/projects/{project_id}/mitm")


@router.get("/requests", response_model=MitmRequestsResponse)
async def list_mitm_requests(
    request: Request,
    project_id: str,
    count: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, description="Stream entry ID for cursor pagination"),
) -> MitmRequestsResponse:
    """List recent MITM-intercepted requests for a project (newest first)."""
    return await mitm_service_of(request).requests(project_id, count, cursor)


@router.delete("/requests")
async def clear_mitm_requests(
    request: Request,
    project_id: str,
    _guard: RequireEditorDep,
) -> dict[str, str]:
    """Clear all MITM request records for a project (editors and admins)."""
    await mitm_service_of(request).clear(project_id)
    return {"status": "ok"}
