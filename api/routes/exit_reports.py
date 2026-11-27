# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Exit reports (docs/exit-reputation.md, Reporting exits): clients say which of their requests a site blocked or let
through, naming each request's exit by its exit headers or its ``-tag-``.

Any signed-in role may send them, a viewer included, since a report is an observation and changes no setting. The api
takes them without Postgres, its project from the process's project map; the workers check and apply them to the
project's exit reputation records.
"""

from fastapi import APIRouter, Request

from api.core.auth import CurrentUserDep
from api.models.exit_reports import ExitReportsRequest, ExitReportsResponse
from api.routes.common import exit_report_service_of

router = APIRouter(prefix="/projects/{project_id}/exit-reports")


@router.post("", response_model=ExitReportsResponse, status_code=202)
async def send_exit_reports(
    request: Request, project_id: str, body: ExitReportsRequest, user: CurrentUserDep
) -> ExitReportsResponse:
    """Take up to 1000 exit reports about the project's requests (any role).

    Each report is named on its own, and a refused one does not hold the others back: 202 with what became of each.
    404 for a project this process's project map does not hold, 409 while its exit reputation is off, and 503 when
    Redis did not answer, after which the reports are sent again.
    """
    return await exit_report_service_of(request).take(project_id, body.reports, user)
