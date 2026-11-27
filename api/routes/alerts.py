# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Alerts: a project's alert history and its alert cut-offs, and the install's alert history with its retention.

A project's are open to everyone signed in, its cut-offs edited by editors; the install's are for admins. Everything is
read from the alerts the workers store (:class:`~api.attention.history.AlertHistory`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request

from api.attention.history import DEFAULT_ALERT_LIMIT, MAX_ALERT_LIMIT
from api.core.auth import RequireAdminDep, RequireEditorDep
from api.db.alert_repository import AlertFilters
from api.models.alerts import AlertEntry, AlertPage, AlertSettingsResponse, AlertSettingsUpdate
from api.models.attention import Severity
from api.models.audit import InstallAlertRetention, InstallAlertRetentionUpdate
from api.routes.common import (
    actor_of,
    alert_history_of,
    alert_settings_of,
    system_settings_service_of,
)

router = APIRouter()

type _Status = Literal["open", "resolved", "all"]
_StatusQuery = Annotated[_Status, Query(description="open (muted and covered included), resolved, or all")]
_SeverityQuery = Annotated[
    Severity | None, Query(description="The severity now for an open alert, the worst reached for a resolved one")
]
_KindQuery = Annotated[str | None, Query(min_length=1, max_length=48)]
_SinceQuery = Annotated[datetime | None, Query(description="Alerts open at any point since this time")]
_UntilQuery = Annotated[datetime | None, Query(description="Only alerts opened at or before this time; now by default")]
_LimitQuery = Annotated[int, Query(ge=1, le=MAX_ALERT_LIMIT)]
_BeforeQuery = Annotated[str | None, Query(description="The id of the last alert already shown")]


@router.get("/projects/{project_id}/alerts", response_model=AlertPage)
async def list_project_alerts(
    request: Request,
    project_id: str,
    status: _StatusQuery = "all",
    severity: _SeverityQuery = None,
    kind: _KindQuery = None,
    q: Annotated[str | None, Query(min_length=1, max_length=100, description="Text in the subject")] = None,
    since: _SinceQuery = None,
    until: _UntilQuery = None,
    limit: _LimitQuery = DEFAULT_ALERT_LIMIT,
    before: _BeforeQuery = None,
) -> AlertPage:
    """A page of the project's alerts, newest first, with the open and resolved counts under the filters and how many
    opened after ``until``; the next page is read ``before`` the last alert shown."""
    filters = AlertFilters.of(status, severity, kind, q, since)
    return await alert_history_of(request).page(project_id, filters, limit=limit, before=before, until=until)


@router.get("/projects/{project_id}/alerts/{alert_id}", response_model=AlertEntry)
async def get_project_alert(request: Request, project_id: str, alert_id: str) -> AlertEntry:
    """One of the project's alerts, with its mute while it is open."""
    return await alert_history_of(request).one(project_id, alert_id)


@router.get("/projects/{project_id}/alert-settings", response_model=AlertSettingsResponse)
async def get_alert_settings(request: Request, project_id: str) -> AlertSettingsResponse:
    """The project's alert cut-offs in force, and the defaults beside them."""
    return await alert_settings_of(request).get(project_id)


@router.put("/projects/{project_id}/alert-settings", response_model=AlertSettingsResponse)
async def put_alert_settings(
    request: Request, project_id: str, body: AlertSettingsUpdate, user: RequireEditorDep
) -> AlertSettingsResponse:
    """Replace the project's alert cut-offs (editor): a field left out is back at its default. Recorded in the
    project's History, and checked at once."""
    return await alert_settings_of(request).update(project_id, body, actor_of(request, user))


@router.get("/alerts/install", response_model=AlertPage)
async def list_install_alerts(
    request: Request,
    _admin: RequireAdminDep,
    status: _StatusQuery = "all",
    severity: _SeverityQuery = None,
    kind: _KindQuery = None,
    since: _SinceQuery = None,
    until: _UntilQuery = None,
    limit: _LimitQuery = DEFAULT_ALERT_LIMIT,
    before: _BeforeQuery = None,
) -> AlertPage:
    """A page of the install's alerts (admin), newest first, as a project's list reads."""
    filters = AlertFilters.of(status, severity, kind, None, since)
    return await alert_history_of(request).page(None, filters, limit=limit, before=before, until=until)


@router.get("/alerts/install/retention", response_model=InstallAlertRetention)
async def get_install_retention(request: Request, _admin: RequireAdminDep) -> InstallAlertRetention:
    """How many days the install's resolved alerts are kept (admin); 0 keeps them forever."""
    return (await system_settings_service_of(request).get()).install_alert_retention()


@router.put("/alerts/install/retention", response_model=InstallAlertRetention)
async def put_install_retention(
    request: Request, body: InstallAlertRetentionUpdate, admin: RequireAdminDep
) -> InstallAlertRetention:
    """Set how many days the install's resolved alerts are kept (admin), recorded on System settings; the hourly
    compaction applies it."""
    saved = await system_settings_service_of(request).set_install_alert_retention(body.days, actor_of(request, admin))
    return saved.install_alert_retention()


# After /alerts/install/retention, so it is never read as an alert's id.
@router.get("/alerts/install/{alert_id}", response_model=AlertEntry)
async def get_install_alert(request: Request, _admin: RequireAdminDep, alert_id: str) -> AlertEntry:
    """One of the install's alerts (admin)."""
    return await alert_history_of(request).one(None, alert_id)
