# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The project failure log: why requests failed or were refused (the Failures page)."""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.core.auth import RequireEditorDep
from api.core.config import settings
from api.routes.common import proxy_manager_of, redis_client_of

router = APIRouter(prefix="/projects/{project_id}/failures")


class FailureRecord(BaseModel):
    """One failure log entry: one failure, or several identical ones folded together."""

    id: str
    kind: str = Field(description="What went wrong, e.g. upstream_timeout, no_proxy_available; 'suppressed' for a tally")
    status: int = Field(description="The status the client was answered (a tunnel device's connection is closed instead)")
    message: str
    error_type: str | None = None
    method: str | None = None
    target: str | None = Field(None, description="host:port, or the URL without its query for an intercepted request")
    session_id: str | None = None
    location: str | None = None
    peer_id: str | None = None
    peer_name: str | None = None
    proxy_id: str | None = None
    connector_id: str | None = None
    connector_name: str | None = None
    proxy: str | None = Field(None, description="host:port of the upstream proxy")
    upstream_status: int | None = Field(None, description="The vendor's HTTP status when it refused the request")
    latency_ms: float | None = Field(None, description="Average time until the failure")
    count: int = Field(description="How many failures this entry stands for")
    first_at: str
    last_at: str
    breakdown: dict[str, int] | None = Field(None, description="For a 'suppressed' entry: the failures not logged, by kind")


class FailureLogResponse(BaseModel):
    """A page of a project's failure log, newest first."""

    records: list[FailureRecord]
    next_cursor: str | None = None
    total: int = Field(description="Entries the log holds")
    enabled: bool
    max_entries: int
    retention_hours: int


def _int(raw: dict[str, str], key: str) -> int | None:
    value = raw.get(key)
    return int(value) if value else None


def _record(raw: dict[str, str]) -> FailureRecord:
    return FailureRecord(
        id=raw["id"],
        kind=raw.get("kind", ""),
        status=_int(raw, "status") or 0,
        message=raw.get("message", ""),
        error_type=raw.get("error_type"),
        method=raw.get("method"),
        target=raw.get("target"),
        session_id=raw.get("session_id"),
        location=raw.get("location"),
        peer_id=raw.get("peer_id"),
        peer_name=raw.get("peer_name"),
        proxy_id=raw.get("proxy_id"),
        connector_id=raw.get("connector_id"),
        connector_name=raw.get("connector_name"),
        proxy=raw.get("proxy"),
        upstream_status=_int(raw, "upstream_status"),
        latency_ms=float(raw["latency_ms"]) if raw.get("latency_ms") else None,
        count=_int(raw, "count") or 1,
        first_at=raw.get("first_at", ""),
        last_at=raw.get("last_at", ""),
        breakdown=json.loads(raw["breakdown"]) if raw.get("breakdown") else None,
    )


@router.get("", response_model=FailureLogResponse)
async def list_failures(
    request: Request,
    project_id: str,
    count: int = Query(200, ge=1, le=1000),
    cursor: str | None = Query(
        None,
        pattern=r"^\d+-\d+$",
        description="Entry ID to page back from (the previous page's next_cursor)",
    ),
) -> FailureLogResponse:
    """List a project's failure log, newest first."""
    if not proxy_manager_of(request).get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    redis_client = redis_client_of(request)

    # One more than asked tells whether there is an older page.
    raw, total = await asyncio.gather(
        redis_client.get_failures(project_id, count=count + 1, before_id=cursor),
        redis_client.count_failures(project_id),
    )
    page = raw[:count]
    return FailureLogResponse(
        records=[_record(r) for r in page],
        next_cursor=page[-1]["id"] if len(raw) > count and page else None,
        total=total,
        enabled=settings.failure_log_enabled,
        max_entries=settings.failure_log_max_entries,
        retention_hours=settings.failure_log_retention_hours,
    )


@router.delete("")
async def clear_failures(request: Request, project_id: str, _guard: RequireEditorDep) -> dict[str, str]:
    """Empty a project's failure log. New failures keep being logged."""
    if not proxy_manager_of(request).get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    await redis_client_of(request).clear_failures(project_id)
    return {"status": "ok"}
