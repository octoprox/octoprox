# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The api's Needs attention: a project's lines with their mutes, every project's problem count, the mutes editors set,
each recorded in the project's History, and the install-wide lines of the Projects page.

Every line is read from the alerts the workers store; the api computes none. A mute is written on the project apart
from what the routing feed compiles, so no gateway ever sees one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

import structlog
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from api.attention.alert import CHECKS_STALE, Alert
from api.attention.checks import KIND_ORDER
from api.attention.install import TEXTS
from api.attention.mutes import MuteBook
from api.core import utc_now
from api.core.logging import error_text
from api.core.roles import JobKind
from api.db.alert_repository import AlertRepository
from api.db.redis import INSTALL_SCOPE
from api.db.repository import ProjectRepository
from api.models.attention import (
    AttentionLine,
    InstallAttention,
    InstallAttentionLine,
    MuteDuration,
    ProjectAttention,
    ProjectProblems,
    ProjectsAttention,
    Severity,
)
from api.models.audit import AuditAction, AuditEntityType
from api.services.errors import NotFoundError

if TYPE_CHECKING:
    from api.attention.requests import AlertChecks
    from api.core.audit import Actor
    from api.core.system_stats import ClusterReader
    from api.db.session import SessionFactory

logger = structlog.get_logger()

# What the api says itself when the checks have stopped or fall behind.
_CHECKS_BEHIND = "Octoprox last checked for problems at {time}, so this list may be out of date."
_CHECKS_NOT_RUNNING = (
    "Octoprox is not checking for problems, so this list may be incomplete. Check that a worker runs the alerts job."
)
_ALERTS_STREAM = f"jobs.{JobKind.ALERTS}"


class AttentionService:
    """The api's Needs attention: a project's lines, every project's problem count, and the mutes editors set."""

    def __init__(self, session_factory: SessionFactory, checks: AlertChecks) -> None:
        self._session_factory = session_factory
        self._checks = checks

    async def project(self, project_id: str) -> ProjectAttention:
        """The project's lines, muted or not, in display order, and when they were checked; no lines while it is
        pending deletion, and not found when there is no such project."""
        now = utc_now()
        # The check state first: a run commits its alerts before it marks the check, so lines read after a state that
        # is no longer pending include that check.
        state = await self._checks.state(project_id, now)
        async with self._session_factory() as session:
            project = await ProjectRepository(session).get_by_id(project_id)
            if project is None:
                raise NotFoundError("Project not found")
            alerts = [] if project.pending_deletion else await AlertRepository(session).open_of(project_id)
            mutes = await ProjectRepository(session).attention_mutes([project_id])
        book = MuteBook(mutes.get(project_id, {}), now)
        lines = [alert.line(book.mute_of(alert)) for alert in sorted(alerts, key=_display_order)]
        return ProjectAttention(
            generated_at=now,
            lines=lines,
            problems=_shown(lines, Severity.PROBLEM),
            warnings=_shown(lines, Severity.WARNING),
            muted=sum(1 for line in lines if line.mute is not None and line.mute.active),
            checked_at=state.checked_at,
            pending=state.pending,
            stale=state.stale,
        )

    async def problems(self) -> ProjectsAttention:
        """Every project's problem lines whose mute is not active, as its card counts them, oldest project first; one
        pending deletion is left out. Three statements."""
        now = utc_now()
        async with self._session_factory() as session:
            projects = await ProjectRepository(session).get_all()
            problems = await AlertRepository(session).open_problems()
            mutes = await ProjectRepository(session).attention_mutes(None)
        books = {project_id: MuteBook(stored, now) for project_id, stored in mutes.items()}
        counts: dict[str, int] = {}
        for project_id, key in problems:
            book = books.get(project_id)
            if book is None or not book.hides(key, Severity.PROBLEM):
                counts[project_id] = counts.get(project_id, 0) + 1
        return ProjectsAttention(
            projects=[
                ProjectProblems(project_id=project.id, problems=counts.get(project.id, 0))
                for project in sorted(projects, key=lambda p: (p.created_at, p.id))
                if not project.pending_deletion
            ]
        )

    async def mute(self, project_id: str, key: str, duration: MuteDuration, actor: Actor) -> None:
        """Mute the project's line ``key`` for everyone in it, replacing its mute if it has one, and record it.

        Raises NotFoundError when there is no such project, or no open alert of that key, shown or covered.
        """
        async with self._session_factory() as session:
            repository = ProjectRepository(session)
            project = await repository.get_by_id(project_id)
            if project is None:
                raise NotFoundError("Project not found")
            alert = await AlertRepository(session).open_one(project_id, key)
            if alert is None:
                raise NotFoundError("Nothing with that key needs attention now")
            stored = await repository.lock_attention_mutes(project_id)
            if stored is None:
                raise NotFoundError("Project not found")
            book = MuteBook(stored, utc_now())
            mutes = book.written(key, alert, duration, actor)
            await repository.write_attention_mutes(project_id, mutes)
            until = mutes[key]["until"]
            await actor.acted(
                session,
                AuditAction.MUTED,
                AuditEntityType.PROJECT,
                entity_id=project.id,
                name=project.name,
                project_id=project.id,
                details={"line": alert.sentence, "muted_until": until if until is not None else "it clears"},
            )
            await session.commit()

    async def unmute(self, project_id: str, key: str, actor: Actor) -> None:
        """Unmute the project's line ``key``, and record it when a mute was in force; nothing when none was.

        Raises NotFoundError when there is no such project.
        """
        async with self._session_factory() as session:
            repository = ProjectRepository(session)
            stored = await repository.lock_attention_mutes(project_id)
            project = await repository.get_by_id(project_id) if stored is not None else None
            if stored is None or project is None:
                raise NotFoundError("Project not found")
            book = MuteBook(stored, utc_now())
            if not book.holds(key):
                return
            await repository.write_attention_mutes(project_id, book.without(key))
            await actor.acted(
                session,
                AuditAction.UNMUTED,
                AuditEntityType.PROJECT,
                entity_id=project.id,
                name=project.name,
                project_id=project.id,
                details={"line": book.text_of(key)},
            )
            await session.commit()


class InstallAttentionService:
    """The install-wide lines every signed-in user sees on the Projects page: the open install alerts, and the lines the
    api answers itself when it cannot read them or the checks have stopped."""

    def __init__(self, session_factory: SessionFactory, checks: AlertChecks, cluster: ClusterReader) -> None:
        self._session_factory = session_factory
        self._checks = checks
        self._cluster = cluster

    async def read(self) -> InstallAttention:
        """The lines now: the stored ones, problems first, then the api's own; always an answer, the database out of
        reach answered alone."""
        now = utc_now()
        try:
            async with self._session_factory() as session:
                alerts = await AlertRepository(session).open_install()
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            logger.warning("Install alerts unavailable: Postgres not answering", error=error_text(exc))
            return InstallAttention(
                generated_at=now, lines=[_live("store_unreachable:database", TEXTS["database"])], checked_at=None
            )
        lines = [alert.install_line() for alert in alerts]
        try:
            state = await self._checks.read(INSTALL_SCOPE, now)
        except (RedisError, OSError) as exc:
            logger.warning("Install alert checks unavailable: Redis not answering", error=error_text(exc))
            lines.append(_live("store_unreachable:live_data", TEXTS["live_data"]))
            return InstallAttention(generated_at=now, lines=lines, checked_at=None)
        if state.checked_at is not None and state.stale:
            lines.append(_live("checks_behind", _CHECKS_BEHIND, at=state.checked_at))
        elif state.checked_at is None and await self._not_running():
            lines.append(_live("checks_not_running", _CHECKS_NOT_RUNNING))
        return InstallAttention(generated_at=now, lines=lines, checked_at=state.checked_at)

    async def _not_running(self) -> bool:
        """Whether the oldest entry waiting on the alerts job stream is ``CHECKS_STALE`` old: no worker takes it."""
        streams = await self._cluster.streams()
        stream = streams.by_name.get(_ALERTS_STREAM)
        return (
            stream is not None
            and stream.oldest_seconds is not None
            and stream.oldest_seconds >= CHECKS_STALE.total_seconds()
        )


def _display_order(alert: Alert) -> tuple[int, int, str, str]:
    """Problems first, then warnings, each by kind, then by name and key: a refresh only changes words in place."""
    return (
        0 if alert.severity == Severity.PROBLEM else 1,
        KIND_ORDER.get(alert.kind, len(KIND_ORDER)),
        alert.sort_name.casefold(),
        alert.key,
    )


def _shown(lines: Sequence[AttentionLine], severity: Severity) -> int:
    """The lines of ``severity`` that show: those whose mute does not hide them."""
    return sum(1 for line in lines if line.severity == severity and (line.mute is None or not line.mute.active))


def _live(key: str, text: str, *, at: datetime | None = None) -> InstallAttentionLine:
    """A line the api answers itself, never stored, so no ``since``."""
    return InstallAttentionLine(
        key=key, severity=Severity.WARNING, text=text, at=at, admin_page="system", since=None
    )
