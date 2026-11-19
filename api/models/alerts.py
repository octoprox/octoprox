# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Alerts: a project's alert cut-offs, and what the alerts endpoints answer; see docs/api.md (Alerts).

The cut-offs are stored on the project as the values that differ from the defaults, apart from what the routing feed
compiles, so no gateway ever sees them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from api.models.attention import AttentionMute, AttentionWhen, Severity
from api.models.audit import MAX_RETENTION_DAYS, Audited, AuditEntityType

# The largest count a cut-off takes.
_MOST = 1_000_000
# Pairs of a warning and a problem percent, the warning never above the problem.
_PAIRS = (
    ("refusals_warning_percent", "refusals_problem_percent"),
    ("vendor_rate_limited_warning_percent", "vendor_rate_limited_problem_percent"),
    ("vendor_forbidden_warning_percent", "vendor_forbidden_problem_percent"),
    ("connector_failing_warning_percent", "connector_failing_problem_percent"),
)


class AlertSettings(BaseModel):
    """A project's alert cut-offs; percent fields are whole percent."""

    model_config = ConfigDict(extra="forbid")

    # Success rate.
    success_rate: bool = True
    success_warning_below: int = Field(default=80, ge=1, le=100)
    # 0 never makes a low success rate a problem.
    success_problem_below: int = Field(default=50, ge=0, le=99)
    success_min_requests: int = Field(default=100, ge=1, le=_MOST)
    # Requests turned away before a proxy.
    refusals_min: int = Field(default=10, ge=1, le=_MOST)
    refusals_warning_percent: int = Field(default=5, ge=1, le=100)
    refusals_problem_percent: int = Field(default=25, ge=1, le=100)
    invalid_location_min: int = Field(default=10, ge=1, le=_MOST)
    # Vendor refusals.
    vendor_min: int = Field(default=10, ge=1, le=_MOST)
    vendor_login_problem_percent: int = Field(default=20, ge=1, le=100)
    vendor_rate_limited_warning_percent: int = Field(default=5, ge=1, le=100)
    vendor_rate_limited_problem_percent: int = Field(default=50, ge=1, le=100)
    vendor_forbidden_warning_percent: int = Field(default=5, ge=1, le=100)
    vendor_forbidden_problem_percent: int = Field(default=50, ge=1, le=100)
    vendor_cannot_serve_warning_percent: int = Field(default=30, ge=1, le=100)
    vendor_other_warning_percent: int = Field(default=30, ge=1, le=100)
    # Connectors.
    connector_failing_min_requests: int = Field(default=50, ge=1, le=_MOST)
    connector_failing_warning_percent: int = Field(default=30, ge=1, le=100)
    connector_failing_problem_percent: int = Field(default=80, ge=1, le=100)
    connector_errors_min: int = Field(default=1, ge=1, le=100)
    stuck_deleting_minutes: int = Field(default=30, ge=5, le=10_080)
    # Proxies and exits.
    unhealthy_share_percent: int = Field(default=25, ge=1, le=100)
    unhealthy_min: int = Field(default=3, ge=1, le=_MOST)
    exits_outside_percent: int = Field(default=10, ge=1, le=100)
    exits_outside_min: int = Field(default=3, ge=1, le=_MOST)
    exits_contradicted_percent: int = Field(default=25, ge=1, le=100)
    exits_contradicted_min: int = Field(default=3, ge=1, le=_MOST)
    site_blocked_percent: int = Field(default=50, ge=1, le=100)
    # Days resolved alerts are kept, 0 forever; None follows the project's metrics retention.
    keep_days: int | None = Field(default=None, ge=0, le=MAX_RETENTION_DAYS)

    @model_validator(mode="after")
    def _consistent(self) -> AlertSettings:
        for warning, problem in _PAIRS:
            if getattr(self, warning) > getattr(self, problem):
                raise ValueError(f"{warning} cannot be above {problem}")
        if self.success_problem_below >= self.success_warning_below:
            raise ValueError("success_problem_below must be below success_warning_below")
        return self

    @classmethod
    def read(cls, stored: Mapping[str, Any]) -> AlertSettings:
        """The settings a project stored, each value this release cannot read left at its default; never raises."""
        try:
            return cls.model_validate(stored)
        except ValidationError:
            pass
        kept = {key: value for key, value in stored.items() if key in cls.model_fields and cls._readable(key, value)}
        try:
            return cls.model_validate(kept)
        except ValidationError:
            # The values agree with no rule between them.
            return cls()

    def stored(self) -> dict[str, Any]:
        """What the project stores: the values that differ from the defaults."""
        return self.model_dump(mode="json", exclude_defaults=True)

    @classmethod
    def _readable(cls, key: str, value: Any) -> bool:
        """Whether ``value`` is one ``key`` takes, whatever the rules between fields say of it beside the defaults."""
        try:
            cls.model_validate({key: value})
        except ValidationError as exc:
            return not any(tuple(error["loc"][:1]) == (key,) for error in exc.errors())
        return True


class AlertSettingsUpdate(AlertSettings):
    """Every override a project keeps, replacing the stored ones; a field left out is back at its default."""


class AlertSettingsResponse(BaseModel):
    """A project's alert settings in force, and the defaults beside them."""

    settings: AlertSettings
    defaults: AlertSettings


class ProjectAlertSettings(Audited):
    """A project's alert settings in force as its History records an edit: under the "Alerts" group, a key at a time."""

    audit_type: ClassVar[AuditEntityType] = AuditEntityType.PROJECT
    audit_nested: ClassVar[frozenset[str]] = frozenset({"alerts"})

    id: str
    name: str
    alerts: dict[str, Any]

    def audit_project(self) -> str | None:
        return self.id


class AlertEntry(BaseModel):
    """One alert: a line of a Needs attention card, a project's or the install's, from when it opened until it
    resolved."""

    id: str
    key: str
    kind: str
    severity: Severity = Field(description="Now while open; when it resolved once resolved")
    worst_severity: Severity = Field(description="The worst it reached")
    lead: str
    subject: str
    text: str = Field(description="An install alert's may hold {time}, which a client replaces with the time of when.at")
    detail: str | None
    when: AttentionWhen | None
    link: str = Field(description="Relative to /projects/{project_id}/; an install alert's admin page, system or geo")
    action: str = Field(description="The link's label; empty on an install alert")
    hidden_by: str | None = Field(
        description="The key of the line that stands for it on the card while it is open and covered; null otherwise"
    )
    opened_at: datetime
    reopened_at: datetime | None
    last_seen_at: datetime
    resolved_at: datetime | None
    reopened: int = Field(description="Times it came back within 15 minutes of resolving")
    mute: AttentionMute | None = Field(description="Only on an open project alert")


class AlertPage(BaseModel):
    """A page of alerts, newest first, with the counts under the filters and how many opened after ``until``."""

    alerts: list[AlertEntry]
    more: bool
    open: int = Field(description="Open alerts under every filter but the status, until and the cursor")
    resolved: int = Field(description="Resolved alerts under every filter but the status, until and the cursor")
    until: datetime = Field(description="The latest opening the page covers: the value asked for, or now")
    newer: int = Field(description="Alerts under every filter that opened after until")
