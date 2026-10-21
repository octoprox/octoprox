# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The project failure log (the Failures page), served by the api role: examples from the failure log streams gateways
append to in Redis, and a summary from the ``failure_metrics`` history the workers' metrics flush job writes.
"""

import asyncio
import builtins
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from api.core import utc_now
from api.core.auth import RequireEditorDep
from api.core.stats import FailureKind
from api.db.repository import ConnectorRepository, MetricsRepository
from api.routes.common import redis_client_of, require_project, session_factory_of, settings_of
from api.routes.metrics import HOST_HISTORY_BUCKETS, RANGE_CONFIG

router = APIRouter(prefix="/projects/{project_id}/failures")

# Every kind a failure is logged under, the streams a page reads across.
ALL_KINDS = [kind.value for kind in FailureKind]


class FailureRecord(BaseModel):
    """One example: a failure, or several identical ones folded together within a few seconds."""

    id: str = Field(description="Stream ID and kind, e.g. 1760526000123-0:upstream_timeout; the cursor for older pages")
    kind: str = Field(description="What went wrong, e.g. upstream_timeout, no_proxy_available")
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


class FailureLogResponse(BaseModel):
    """A page of a project's examples, newest first."""

    records: list[FailureRecord]
    next_cursor: str | None = None
    total: int = Field(description="Examples the log holds (of the kind asked for, or of all kinds)")
    appended: int = Field(description="Examples ever appended; compare with /head to know how many are newer")
    enabled: bool
    max_entries: int = Field(description="Examples kept per kind")
    retention_hours: int


class FailureLogHead(BaseModel):
    """Where the log stands now, so a page can say how many examples are newer than the ones it shows."""

    appended: int
    total: int


class UpstreamStatusCount(BaseModel):
    status: int
    count: int


class FailureKindCount(BaseModel):
    kind: str
    count: int
    upstream_statuses: list[UpstreamStatusCount] = Field(description="The vendor's statuses within this kind, most first")


class ConnectorFailureCount(BaseModel):
    connector_id: str | None = Field(description="None for requests refused before a proxy was chosen")
    name: str | None
    count: int


class FailureSeriesPoint(BaseModel):
    timestamp: datetime
    count: int


class FailureSeries(BaseModel):
    kind: str
    points: list[FailureSeriesPoint]


class FailureSummaryResponse(BaseModel):
    """Every failure of the window, counted: by kind, vendor status and connector, and over time by kind."""

    range: str
    since: datetime
    bucket_seconds: int
    total: int
    kinds: list[FailureKindCount] = Field(description="Most first")
    connectors: list[ConnectorFailureCount] = Field(description="Most first")
    series: list[FailureSeries] = Field(description="One per kind seen, most first, each with every bucket of the window")
    enabled: bool


def _int(raw: dict[str, str], key: str) -> int | None:
    value = raw.get(key)
    return int(value) if value else None


def _record(raw: dict[str, str]) -> FailureRecord:
    return FailureRecord(
        id=f"{raw['id']}:{raw['kind']}",
        kind=raw["kind"],
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
    )


@router.get("", response_model=FailureLogResponse)
async def list_failures(
    request: Request,
    project_id: str,
    kind: Annotated[FailureKind | None, Query(description="Only examples of this kind")] = None,
    count: int = Query(200, ge=1, le=1000),
    cursor: str | None = Query(
        None,
        pattern=r"^\d+-\d+:[a-z_]+$",
        description="The id of the last example of the previous page (its next_cursor)",
    ),
) -> FailureLogResponse:
    """A project's failure examples, newest first, across every kind or of one."""
    await require_project(request, project_id)
    redis_client = redis_client_of(request)
    settings = settings_of(request)
    kinds = [kind.value] if kind else ALL_KINDS
    after: tuple[str, str] | None = None
    if cursor:
        stream_id, after_kind = cursor.split(":", 1)
        after = (stream_id, after_kind)

    # Read before the page: an example landing in between is then announced
    # as newer though already shown, rather than counted as seen while missing.
    appended = await redis_client.failures_appended(project_id)
    # One more than asked tells whether there is an older page.
    raw, total = await asyncio.gather(
        redis_client.get_failures(project_id, kinds, count=count + 1, after=after),
        redis_client.count_failures(project_id, kinds),
    )
    page = [_record(r) for r in raw[:count]]
    return FailureLogResponse(
        records=page,
        next_cursor=page[-1].id if len(raw) > count and page else None,
        total=total,
        appended=sum(appended.get(k, 0) for k in kinds),
        enabled=settings.failure_log_enabled,
        max_entries=settings.failure_log_max_entries,
        retention_hours=settings.failure_log_retention_hours,
    )


@router.get("/head", response_model=FailureLogHead)
async def failure_log_head(
    request: Request,
    project_id: str,
    kind: Annotated[FailureKind | None, Query(description="Only examples of this kind")] = None,
) -> FailureLogHead:
    """How many examples have been appended so far: two small reads, cheap enough to poll."""
    await require_project(request, project_id)
    redis_client = redis_client_of(request)
    kinds = [kind.value] if kind else ALL_KINDS
    total, appended = await asyncio.gather(
        redis_client.count_failures(project_id, kinds),
        redis_client.failures_appended(project_id),
    )
    return FailureLogHead(appended=sum(appended.get(k, 0) for k in kinds), total=total)


@router.get("/summary", response_model=FailureSummaryResponse)
async def failure_summary(
    request: Request,
    project_id: str,
    range: Literal["1h", "6h", "24h", "7d", "30d"] = Query("24h", alias="range"),
) -> FailureSummaryResponse:
    """Every failure of the window, from the flushed history (so it lags by up to one flush interval)."""
    await require_project(request, project_id)
    delta, _limit, _bucket = RANGE_CONFIG[range]
    bucket_seconds = HOST_HISTORY_BUCKETS[range]
    now = utc_now()
    since = now - delta
    async with session_factory_of(request)() as session:
        repo = MetricsRepository(session)
        breakdown = await repo.get_failure_breakdown(project_id, since)
        history = await repo.get_failure_history(project_id, since, bucket_seconds)
        names = {c.id: c.name for c in await ConnectorRepository(session).get_by_project(project_id)}

    by_kind: dict[str, int] = {}
    statuses: dict[str, dict[int, int]] = {}
    by_connector: dict[str | None, int] = {}
    for row in breakdown:
        n = row["failure_count"]
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + n
        if row["upstream_status"] is not None:
            per_kind = statuses.setdefault(row["kind"], {})
            per_kind[row["upstream_status"]] = per_kind.get(row["upstream_status"], 0) + n
        by_connector[row["connector_id"]] = by_connector.get(row["connector_id"], 0) + n
    ranked = sorted(by_kind, key=lambda k: (-by_kind[k], k))

    # Every bucket of the window for every kind, zeros where it was quiet, so the series stack.
    first = int(since.replace(tzinfo=UTC).timestamp()) // bucket_seconds * bucket_seconds
    last = int(now.replace(tzinfo=UTC).timestamp())
    buckets = [
        datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None)
        for epoch in builtins.range(first, last + 1, bucket_seconds)  # ``range`` is the query parameter here
    ]
    points: dict[str, dict[datetime, int]] = {}
    for row in history:
        points.setdefault(row["kind"], {})[row["timestamp"]] = row["failure_count"]
    # A row the controller stamped past this process's clock (a few ms of skew at
    # a bucket boundary) gets its bucket too, rather than missing from the chart.
    buckets = sorted({*buckets, *(row["timestamp"] for row in history)})

    return FailureSummaryResponse(
        range=range,
        since=since,
        bucket_seconds=bucket_seconds,
        total=sum(by_kind.values()),
        kinds=[
            FailureKindCount(
                kind=k,
                count=by_kind[k],
                upstream_statuses=[
                    UpstreamStatusCount(status=s, count=n)
                    for s, n in sorted(statuses.get(k, {}).items(), key=lambda item: (-item[1], item[0]))
                ],
            )
            for k in ranked
        ],
        connectors=[
            ConnectorFailureCount(connector_id=cid, name=names.get(cid) if cid else None, count=n)
            for cid, n in sorted(by_connector.items(), key=lambda item: -item[1])
        ],
        series=[
            FailureSeries(
                kind=k,
                points=[FailureSeriesPoint(timestamp=ts, count=points.get(k, {}).get(ts, 0)) for ts in buckets],
            )
            for k in ranked
        ],
        enabled=settings_of(request).failure_log_enabled,
    )


@router.delete("")
async def clear_failures(request: Request, project_id: str, _guard: RequireEditorDep) -> dict[str, str]:
    """Empty a project's failure log (the examples). The counts in history stay; new failures keep being logged."""
    await require_project(request, project_id)
    await redis_client_of(request).clear_failures(project_id, ALL_KINDS)
    return {"status": "ok"}
