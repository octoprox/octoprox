# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The stored alerts (``alerts``), in the caller's session; the caller commits.

The alerts job writes them in one statement a run (:meth:`AlertRepository.reconcile`); the api reads them, and the
hourly compaction deletes the resolved ones past their retention.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import ColumnElement, case, delete, func, literal, or_, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from api.attention.alert import Alert, FoundAlert
from api.db.models import AlertModel
from api.models.attention import AttentionWhen, Severity

# One statement for a whole run: open alerts found again are refreshed, those resolved within the reopen window come
# back, new conditions open, and the open alerts of a checked scope not found now resolve, unless the check could not
# judge them. The parts touch disjoint rows, and every guard compares with :now, so a run repeated changes nothing.
_RECONCILE = text("""
WITH found AS (
  SELECT * FROM unnest(
    CAST(:ids AS text[]), CAST(:scopes AS text[]), CAST(:keys AS text[]), CAST(:kinds AS text[]),
    CAST(:severities AS text[]), CAST(:leads AS text[]), CAST(:subjects AS text[]), CAST(:bodies AS text[]),
    CAST(:details AS text[]), CAST(:when_kinds AS text[]), CAST(:when_ats AS timestamp[]), CAST(:links AS text[]),
    CAST(:actions AS text[]), CAST(:sort_names AS text[]), CAST(:hidden_bys AS text[])
  ) AS f(id, scope, key, kind, severity, lead, subject, body, detail, when_kind, when_at, link, action, sort_name,
         hidden_by)),
checked AS (SELECT unnest(CAST(:checked AS text[])) AS scope),
held AS (SELECT * FROM unnest(CAST(:held_scopes AS text[]), CAST(:held_keys AS text[])) AS h(scope, key)),
-- open alerts found again: refreshed (wording, severity, worst severity, hidden, last seen)
kept AS (
  UPDATE alerts a SET kind = f.kind, severity = f.severity,
         worst_severity = CASE WHEN f.severity = 'problem' THEN 'problem' ELSE a.worst_severity END,
         lead = f.lead, subject = f.subject, body = f.body, detail = f.detail, when_kind = f.when_kind,
         when_at = f.when_at, link = f.link, action = f.action, sort_name = f.sort_name, hidden_by = f.hidden_by,
         last_seen_at = CAST(:now AS timestamp)
  FROM found f
  WHERE a.resolved_at IS NULL AND coalesce(a.project_id, '') = f.scope AND a.key = f.key
    AND a.last_seen_at < CAST(:now AS timestamp)
  RETURNING a.id),
-- the newest alert of each found key resolved within the reopen window
recent AS (
  SELECT DISTINCT ON (f.scope, f.key) a.id, f.scope, f.key
  FROM found f JOIN alerts a ON coalesce(a.project_id, '') = f.scope AND a.key = f.key
  WHERE a.resolved_at >= CAST(:reopen_since AS timestamp)
  ORDER BY f.scope, f.key, a.resolved_at DESC),
reopened AS (
  UPDATE alerts a SET resolved_at = NULL, reopened_at = CAST(:now AS timestamp),
         reopened_count = a.reopened_count + 1, kind = f.kind, severity = f.severity,
         worst_severity = CASE WHEN f.severity = 'problem' THEN 'problem' ELSE a.worst_severity END,
         lead = f.lead, subject = f.subject, body = f.body, detail = f.detail, when_kind = f.when_kind,
         when_at = f.when_at, link = f.link, action = f.action, sort_name = f.sort_name, hidden_by = f.hidden_by,
         last_seen_at = CAST(:now AS timestamp)
  FROM recent r JOIN found f ON f.scope = r.scope AND f.key = r.key
  WHERE a.id = r.id AND a.resolved_at < CAST(:now AS timestamp)
    AND NOT EXISTS (SELECT 1 FROM alerts o
                    WHERE o.resolved_at IS NULL AND coalesce(o.project_id, '') = r.scope AND o.key = r.key)
  RETURNING a.id),
-- new conditions: nothing open, nothing resolved within the window, and the project still there
opened AS (
  INSERT INTO alerts (id, project_id, key, kind, severity, worst_severity, lead, subject, body, detail, when_kind,
                      when_at, link, action, sort_name, hidden_by, opened_at, last_seen_at, reopened_count)
  SELECT f.id, nullif(f.scope, ''), f.key, f.kind, f.severity, f.severity, f.lead, f.subject, f.body, f.detail,
         f.when_kind, f.when_at, f.link, f.action, f.sort_name, f.hidden_by, CAST(:now AS timestamp),
         CAST(:now AS timestamp), 0
  FROM found f
  WHERE (f.scope = '' OR EXISTS (SELECT 1 FROM projects p WHERE p.id = f.scope))
    AND NOT EXISTS (SELECT 1 FROM alerts x
                    WHERE coalesce(x.project_id, '') = f.scope AND x.key = f.key
                      AND (x.resolved_at IS NULL OR x.resolved_at >= CAST(:reopen_since AS timestamp)))
  ON CONFLICT ((coalesce(project_id, '')), key) WHERE resolved_at IS NULL DO NOTHING
  RETURNING id),
-- open alerts of a checked scope not found now, unless the check could not judge them
resolved AS (
  UPDATE alerts a SET resolved_at = CAST(:now AS timestamp)
  FROM checked c
  WHERE a.resolved_at IS NULL AND coalesce(a.project_id, '') = c.scope AND a.last_seen_at < CAST(:now AS timestamp)
    AND NOT EXISTS (SELECT 1 FROM found f WHERE f.scope = c.scope AND f.key = a.key)
    AND NOT EXISTS (SELECT 1 FROM held h WHERE h.scope = c.scope AND h.key = a.key)
  RETURNING a.id)
SELECT (SELECT count(*) FROM kept), (SELECT count(*) FROM reopened), (SELECT count(*) FROM opened),
       (SELECT count(*) FROM resolved)
""")


@dataclass(frozen=True)
class AlertFilters:
    """What a list of alerts is narrowed to; a filter left None does not narrow."""

    # "open" (muted and covered included), "resolved", or None for both.
    status: str | None = None
    # Matched against the severity now for an open alert, the worst reached for a resolved one.
    severity: Severity | None = None
    kind: str | None = None
    # Text in the subject, any case.
    q: str | None = None
    # Alerts open at any point since then.
    since: datetime | None = None


@dataclass(frozen=True)
class AlertRows:
    """A page of alerts newest first, whether more follow, and the counts under the filters."""

    alerts: list[Alert]
    more: bool
    # Under every filter but the status, the latest opening and the cursor.
    open: int
    resolved: int
    # Under every filter, opened after the latest opening.
    newer: int


@dataclass(frozen=True)
class ReconcileCounts:
    """What one reconcile did: alerts kept open, reopened, opened and resolved."""

    kept: int = 0
    reopened: int = 0
    opened: int = 0
    resolved: int = 0

    @property
    def changed(self) -> bool:
        """Whether any alert opened, reopened or resolved."""
        return bool(self.reopened or self.opened or self.resolved)


class AlertRepository:
    """The stored alerts: the alerts job's one write a run, and the reads of the card, the lists and the history."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def reconcile(
        self,
        found: Sequence[FoundAlert],
        checked: Collection[str],
        held: Collection[tuple[str, str]],
        now: datetime,
        *,
        reopen: timedelta,
    ) -> ReconcileCounts:
        """Store what a run found in the scopes it checked (a project id, '' for the install), in one statement.

        A found alert open already is refreshed, one resolved within ``reopen`` reopens, any other opens; an open alert
        of a checked scope not found resolves, unless ``held`` names it, as (scope, key), for a check that could not
        judge it. Safe to run again, and any number at once (see the statement).
        """
        # One row per scope and key, the first found.
        unique: dict[tuple[str, str], FoundAlert] = {}
        for alert in found:
            unique.setdefault((alert.scope, alert.finding.key), alert)
        rows = list(unique.values())
        when = [alert.finding.when for alert in rows]
        result = await self._session.execute(
            _RECONCILE,
            {
                "ids": [str(uuid4()) for _ in rows],
                "scopes": [alert.scope for alert in rows],
                "keys": [alert.finding.key for alert in rows],
                "kinds": [str(alert.finding.kind) for alert in rows],
                "severities": [alert.finding.severity.value for alert in rows],
                "leads": [alert.finding.lead for alert in rows],
                "subjects": [alert.finding.subject for alert in rows],
                "bodies": [alert.finding.text for alert in rows],
                "details": [alert.finding.detail for alert in rows],
                "when_kinds": [at.kind if at is not None else None for at in when],
                "when_ats": [at.at if at is not None else None for at in when],
                "links": [alert.finding.link for alert in rows],
                "actions": [alert.finding.action for alert in rows],
                "sort_names": [alert.finding.sort_name for alert in rows],
                "hidden_bys": [alert.hidden_by for alert in rows],
                "checked": sorted(set(checked)),
                "held_scopes": [scope for scope, _key in held],
                "held_keys": [key for _scope, key in held],
                "now": now,
                "reopen_since": now - reopen,
            },
        )
        kept, reopened, opened, resolved = result.one()
        return ReconcileCounts(kept=kept, reopened=reopened, opened=opened, resolved=resolved)

    # --- The card and the counts -----------------------------------------------------------------

    async def open_of(self, project_id: str, *, shown: bool = True) -> list[Alert]:
        """The project's open alerts: those its card shows, or with ``shown`` False every one, the covered ones too."""
        query = select(AlertModel).where(AlertModel.project_id == project_id, AlertModel.resolved_at.is_(None))
        if shown:
            query = query.where(AlertModel.hidden_by == "")
        return [_alert(model) for model in (await self._session.execute(query)).scalars()]

    async def open_problems(self) -> list[tuple[str, str]]:
        """Every project's open problem alerts its card shows, as (project id, key)."""
        result = await self._session.execute(
            select(AlertModel.project_id, AlertModel.key).where(
                AlertModel.resolved_at.is_(None),
                AlertModel.project_id.is_not(None),
                AlertModel.severity == Severity.PROBLEM.value,
                AlertModel.hidden_by == "",
            )
        )
        return [(str(project_id), key) for project_id, key in result.all()]

    async def open_install(self) -> list[Alert]:
        """The install's open alerts, problems first, then in the order their lines show."""
        result = await self._session.execute(
            select(AlertModel)
            .where(AlertModel.project_id.is_(None), AlertModel.resolved_at.is_(None))
            .order_by((AlertModel.severity != Severity.PROBLEM.value), AlertModel.sort_name, AlertModel.key)
        )
        return [_alert(model) for model in result.scalars()]

    async def open_one(self, project_id: str, key: str) -> Alert | None:
        """The project's open alert of ``key``, shown or covered; None when there is none."""
        result = await self._session.execute(
            select(AlertModel).where(
                AlertModel.project_id == project_id, AlertModel.key == key, AlertModel.resolved_at.is_(None)
            )
        )
        model = result.scalar_one_or_none()
        return _alert(model) if model is not None else None

    # --- History ---------------------------------------------------------------------------------

    async def page(
        self,
        project_id: str | None,
        filters: AlertFilters,
        *,
        until: datetime,
        limit: int,
        before: str | None = None,
    ) -> AlertRows:
        """A project's alerts (the install's for None) newest first, opened at or before ``until``: ``limit`` of them,
        older than the alert ``before`` names when given, with the counts under the filters. Two statements.

        The cursor is the alert's ``(opened_at, id)``, so alerts opened while someone pages never shift a page.
        """
        scope = AlertModel.project_id == project_id if project_id is not None else AlertModel.project_id.is_(None)
        narrowed = [scope, *_narrowed(filters)]
        resolved = AlertModel.resolved_at.is_not(None)
        status = _status(filters.status)
        query = (
            select(AlertModel)
            .where(*narrowed, *status, AlertModel.opened_at <= until)
            .order_by(AlertModel.opened_at.desc(), AlertModel.id.desc())
            .limit(limit + 1)
        )
        if before:
            # An alert retention removed is older than every alert left: nothing comes after it.
            anchor = aliased(AlertModel)
            anchor_time = select(anchor.opened_at).where(anchor.id == before).scalar_subquery()
            query = query.where(tuple_(AlertModel.opened_at, AlertModel.id) < tuple_(anchor_time, literal(before)))
        models = list((await self._session.execute(query)).scalars())
        newer = AlertModel.opened_at > until
        counts = (
            await self._session.execute(
                select(
                    func.count().filter(~resolved),
                    func.count().filter(resolved),
                    func.count().filter(newer, *status),
                ).where(*narrowed)
            )
        ).one()
        return AlertRows(
            alerts=[_alert(model) for model in models[:limit]],
            more=len(models) > limit,
            open=int(counts[0]),
            resolved=int(counts[1]),
            newer=int(counts[2]),
        )

    async def one(self, project_id: str | None, alert_id: str) -> Alert | None:
        """One alert of the project (the install's for None); None when it has none of that id."""
        scope = AlertModel.project_id == project_id if project_id is not None else AlertModel.project_id.is_(None)
        result = await self._session.execute(select(AlertModel).where(AlertModel.id == alert_id, scope))
        model = result.scalar_one_or_none()
        return _alert(model) if model is not None else None

    # --- Retention -------------------------------------------------------------------------------

    async def delete_resolved_of_projects(self, now: datetime) -> int:
        """Delete each project's alerts resolved before its alerts setting's days, its metrics retention's when unset,
        none at 0 days; how many went."""
        result = await self._session.execute(_DELETE_RESOLVED_OF_PROJECTS, {"now": now})
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def delete_resolved_of_install(self, before: datetime) -> int:
        """Delete the install's alerts resolved before ``before``; how many went."""
        result = await self._session.execute(
            delete(AlertModel).where(AlertModel.project_id.is_(None), AlertModel.resolved_at < before)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


# A project's resolved alerts, kept as long as its alerts setting says, its metrics' retention when unset; 0 forever.
_DELETE_RESOLVED_OF_PROJECTS = text("""
DELETE FROM alerts a USING projects p
WHERE a.project_id = p.id AND a.resolved_at IS NOT NULL
  AND coalesce((p.alert_settings->>'keep_days')::int, p.metrics_retention_days) > 0
  AND a.resolved_at < CAST(:now AS timestamp)
      - make_interval(days => coalesce((p.alert_settings->>'keep_days')::int, p.metrics_retention_days))
""")


def _narrowed(filters: AlertFilters) -> list[ColumnElement[bool]]:
    """The conditions of every filter but the status."""
    conditions: list[ColumnElement[bool]] = []
    if filters.severity is not None:
        shown = case((AlertModel.resolved_at.is_(None), AlertModel.severity), else_=AlertModel.worst_severity)
        conditions.append(shown == filters.severity.value)
    if filters.kind is not None:
        conditions.append(AlertModel.kind == filters.kind)
    if filters.q:
        escaped = filters.q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(AlertModel.subject.ilike(f"%{escaped}%", escape="\\"))
    if filters.since is not None:
        conditions.append(or_(AlertModel.resolved_at.is_(None), AlertModel.resolved_at >= filters.since))
    return conditions


def _status(status: str | None) -> list[ColumnElement[bool]]:
    """The condition of the status filter: open, resolved, or none for both."""
    if status == "open":
        return [AlertModel.resolved_at.is_(None)]
    if status == "resolved":
        return [AlertModel.resolved_at.is_not(None)]
    return []


def _alert(model: AlertModel) -> Alert:
    """A stored row as the domain reads it."""
    return Alert(
        id=model.id,
        project_id=model.project_id,
        key=model.key,
        kind=model.kind,
        severity=Severity(model.severity),
        worst_severity=Severity(model.worst_severity),
        lead=model.lead,
        subject=model.subject,
        text=model.body,
        detail=model.detail,
        when=_when(model.when_kind, model.when_at),
        link=model.link,
        action=model.action,
        sort_name=model.sort_name,
        hidden_by=model.hidden_by,
        opened_at=model.opened_at,
        last_seen_at=model.last_seen_at,
        resolved_at=model.resolved_at,
        reopened_at=model.reopened_at,
        reopened=model.reopened_count,
    )


def _when(kind: str | None, at: datetime | None) -> AttentionWhen | None:
    """A stored line's one time; None when it has none, or one this release cannot read."""
    if kind is None or at is None or kind not in ("last_at", "last_failed", "resets", "at"):
        return None
    return AttentionWhen(kind=kind, at=at)  # type: ignore[arg-type]
