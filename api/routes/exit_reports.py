# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reports (docs/exit-reputation.md, Reporting exits): clients say which of their requests a site blocked or let
through, naming each request's exit by its exit headers or its ``-tag-``.

Any signed-in role may send them, a viewer included, since a report is an observation and changes no setting. The api
only resolves and appends them; the workers apply them to the project's exit reputation records.
"""

from fastapi import APIRouter, HTTPException, Request

from api.core.auth import CurrentUserDep
from api.models.exit_reports import ExitReportsRequest, ExitReportsResponse
from api.reputation.reports import ExitReportIntake, ExitReportsUnavailableError
from api.routes.common import (
    UNAVAILABLE_RETRY_AFTER_SECONDS,
    redis_client_of,
    require_project,
    session_factory_of,
)

router = APIRouter(prefix="/projects/{project_id}/exit-reports")


@router.post("", response_model=ExitReportsResponse, status_code=202)
async def send_exit_reports(
    request: Request, project_id: str, body: ExitReportsRequest, user: CurrentUserDep
) -> ExitReportsResponse:
    """Take up to 1000 exit reports about the project's requests (any role).

    Each report is resolved on its own, and a refused one does not hold the others back: 202 with what became of each.
    409 while the project's exit reputation is off, and 503 when Redis did not answer, after which the reports are sent
    again.
    """
    project = await require_project(request, project_id)
    if not project.exit_reputation.records:
        raise HTTPException(status_code=409, detail="Exit reputation is off for this project")
    intake = ExitReportIntake(session_factory_of(request), redis_client_of(request))
    try:
        return await intake.take(project, body.reports, reporter_id=user.id, token_id=user.token_id)
    except ExitReportsUnavailableError:
        raise HTTPException(
            status_code=503,
            detail="Redis cannot be reached, so the reports were not taken; send them again shortly",
            headers={"Retry-After": str(UNAVAILABLE_RETRY_AFTER_SECONDS)},
        ) from None
