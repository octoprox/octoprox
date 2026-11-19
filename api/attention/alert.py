# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Alerts: the lines the alerts job found, stored with their history, and read back by the api.

A condition present now opens its alert, or keeps it open; gone, the alert resolves at once; back within
``REOPEN_WINDOW`` of resolving, the same alert reopens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from api.models.alerts import AlertEntry
from api.models.attention import AttentionLine, AttentionWhen, InstallAttentionLine, Severity

if TYPE_CHECKING:
    from api.attention.checks import Finding
    from api.models.attention import AttentionMute

# A condition back within this long of resolving reopens the same alert.
REOPEN_WINDOW = timedelta(minutes=15)
# An install condition becomes an alert once two checks this far apart have seen it: a failover or a rolling
# deploy opens none.
INSTALL_SETTLE = timedelta(seconds=45)
# The most job entries one alerts run takes: a burst of run-now requests is one run.
ALERTS_BATCH_SIZE = 500
# A scope last checked longer ago than this is out of date (the card and the install lines say so).
CHECKS_STALE = timedelta(minutes=3)


@dataclass(frozen=True)
class FoundAlert:
    """A finding as a run stores it: its scope (a project id, '' for the install) and the key of the shown line that
    makes it redundant ('' while it shows)."""

    scope: str
    finding: Finding
    hidden_by: str = ""


@dataclass(frozen=True)
class Alert:
    """A stored alert, read: a project's (``project_id`` set) or the install's."""

    id: str
    project_id: str | None
    key: str
    kind: str
    # Now while open, when it resolved once resolved.
    severity: Severity
    worst_severity: Severity
    lead: str
    subject: str
    text: str
    detail: str | None
    when: AttentionWhen | None
    link: str
    action: str
    sort_name: str
    # The key of the shown line that stands for it on the card; '' while it shows.
    hidden_by: str
    opened_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    reopened_at: datetime | None
    reopened: int

    @property
    def sentence(self) -> str:
        """The whole line, as History records it."""
        return self.lead + self.subject + self.text

    @property
    def since(self) -> datetime:
        """When its current spell began: its last reopen, else its opening."""
        return self.reopened_at or self.opened_at

    def line(self, mute: AttentionMute | None) -> AttentionLine:
        """The open alert as a line of its project's card, with its mute."""
        return AttentionLine(
            key=self.key,
            kind=self.kind,
            severity=self.severity,
            lead=self.lead,
            subject=self.subject,
            text=self.text,
            detail=self.detail,
            when=self.when,
            link=self.link,
            action=self.action,
            mute=mute,
            alert_id=self.id,
            since=self.since,
            opened_at=self.opened_at,
            reopened=self.reopened,
        )

    def entry(self, mute: AttentionMute | None) -> AlertEntry:
        """The alert as its history lists it, with its mute while it is an open project alert."""
        return AlertEntry(
            id=self.id,
            key=self.key,
            kind=self.kind,
            severity=self.severity,
            worst_severity=self.worst_severity,
            lead=self.lead,
            subject=self.subject,
            text=self.text,
            detail=self.detail,
            when=self.when,
            link=self.link,
            action=self.action,
            hidden_by=(self.hidden_by or None) if self.resolved_at is None else None,
            opened_at=self.opened_at,
            reopened_at=self.reopened_at,
            last_seen_at=self.last_seen_at,
            resolved_at=self.resolved_at,
            reopened=self.reopened,
            mute=mute if self.project_id is not None and self.resolved_at is None else None,
        )

    def install_line(self) -> InstallAttentionLine:
        """The open install alert as a line of the Projects page."""
        return InstallAttentionLine(
            key=self.key,
            severity=self.severity,
            text=self.text,
            at=self.when.at if self.when is not None and self.when.kind == "at" else None,
            admin_page="geo" if self.link == "geo" else "system",
            since=self.since,
        )
