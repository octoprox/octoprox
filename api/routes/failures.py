# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The project failure log (the Failures page), served by the api role: examples from the failure log gateways add to in
Redis, and a summary from the ``failure_metrics`` history the workers' metrics flush job writes.
"""

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request

from api.core.auth import RequireEditorDep
from api.core.stats import FailureKind
from api.models.failures import (
    FailureLogHead,
    FailureLogResponse,
    FailureRecord,
    FailureSummaryResponse,
)
from api.models.metrics import MetricsRange
from api.routes.common import failure_log_service_of
from api.services.failures import (
    CURSOR_PATTERN,
    DEFAULT_FAILURE_PAGE,
    FAILURE_ID_PATTERN,
    MAX_FAILURE_PAGE,
)

router = APIRouter(prefix="/projects/{project_id}/failures")


@router.get("", response_model=FailureLogResponse)
async def list_failures(
    request: Request,
    project_id: str,
    kind: Annotated[FailureKind | None, Query(description="Only examples of this kind")] = None,
    count: int = Query(DEFAULT_FAILURE_PAGE, ge=1, le=MAX_FAILURE_PAGE),
    cursor: str | None = Query(
        None,
        pattern=CURSOR_PATTERN,
        description="The previous page's next_cursor",
    ),
) -> FailureLogResponse:
    """A project's failure examples, the ones seen last first, across every kind or of one."""
    return await failure_log_service_of(request).page(project_id, kind, count=count, cursor=cursor)


@router.get("/head", response_model=FailureLogHead)
async def failure_log_head(
    request: Request,
    project_id: str,
    kind: Annotated[FailureKind | None, Query(description="Only examples of this kind")] = None,
    since: int = Query(0, ge=0, description="The newest of the page read"),
) -> FailureLogHead:
    """How many examples are new or seen again since the page read at ``since``: small reads, cheap enough to poll."""
    return await failure_log_service_of(request).head(project_id, kind, since=since)


@router.get("/summary", response_model=FailureSummaryResponse)
async def failure_summary(
    request: Request,
    project_id: str,
    range: Annotated[MetricsRange, Query(alias="range")] = "24h",
) -> FailureSummaryResponse:
    """Every failure of the window, from the flushed history (so it lags by up to one flush interval)."""
    return await failure_log_service_of(request).summary(project_id, range)


# After /head and /summary, so neither is read as an example's id.
@router.get("/{failure_id}", response_model=FailureRecord)
async def get_failure(
    request: Request,
    project_id: str,
    failure_id: Annotated[
        str, Path(pattern=FAILURE_ID_PATTERN, description="The example's id, as the list gives it")
    ],
) -> FailureRecord:
    """One example by its id, for a link to it; 404 once the log no longer keeps it."""
    return await failure_log_service_of(request).get(project_id, failure_id)


@router.delete("")
async def clear_failures(request: Request, project_id: str, _guard: RequireEditorDep) -> dict[str, str]:
    """Empty a project's failure log (the examples). The counts in history stay; new failures keep being logged."""
    await failure_log_service_of(request).clear(project_id)
    return {"status": "ok"}
