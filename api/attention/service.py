# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The api's Needs attention: a project's lines with their mutes, every project's problem count, and the mutes editors
set, each recorded in the project's History.

Every line is read off stored state on each request; a mute is written on the project apart from what the routing feed
compiles, so no gateway ever sees one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

import structlog

from api.attention.checks import CHECKS, AttentionCheck, Report, build_report
from api.attention.data import ProjectData, ProjectDataLoader
from api.attention.mutes import MuteBook
from api.core import utc_now
from api.db.repository import ProjectRepository
from api.models.attention import (
    AttentionLine,
    MuteDuration,
    ProjectAttention,
    ProjectProblems,
    ProjectsAttention,
    Severity,
)
from api.models.audit import AuditAction, AuditEntityType
from api.services.errors import NotFoundError

if TYPE_CHECKING:
    from api.core.audit import Actor
    from api.db.session import SessionFactory
    from api.providers.registry import ProviderRegistry
    from api.routing.router import Router
    from api.services.read_model import RuntimeView

logger = structlog.get_logger()

# The checks the project list runs: only those that can find a problem.
PROBLEM_CHECKS: tuple[AttentionCheck, ...] = tuple(check for check in CHECKS if check.can_be_problem)


class AttentionService:
    """The api's Needs attention: a project's lines, every project's problem count, and the mutes editors set."""

    def __init__(
        self,
        session_factory: SessionFactory,
        runtime_view: RuntimeView,
        registry: ProviderRegistry,
        *,
        preview: Callable[[str], Awaitable[Router]],
        preview_all: Callable[[], Awaitable[Router]],
    ) -> None:
        self._session_factory = session_factory
        self._loader = ProjectDataLoader(
            session_factory, runtime_view, registry, preview=preview, preview_all=preview_all
        )

    async def project(self, project_id: str) -> ProjectAttention | None:
        """The project's lines, muted or not, in display order; None when there is no such project, and none while it
        is pending deletion.

        An until-clear mute whose line is gone is removed here: no person did it, so History records nothing.
        """
        report = await self._report(project_id)
        if report is None:
            return None
        data, found = report
        book = MuteBook(data.mutes, data.now)
        lines = [finding.line(book.mute_of(finding)) for finding in found.findings]
        # A project pending deletion has no lines, which clears nothing.
        cleared = [] if data.project.pending_deletion else book.cleared(found.present)
        if cleared:
            async with self._session_factory() as session:
                removed = await ProjectRepository(session).clear_attention_mutes(project_id, cleared)
                await session.commit()
            if removed:
                logger.info("Needs attention mutes ended: their lines cleared", project_id=project_id, keys=cleared)
        return ProjectAttention(
            generated_at=data.now,
            lines=lines,
            problems=_shown(lines, Severity.PROBLEM),
            warnings=_shown(lines, Severity.WARNING),
            muted=sum(1 for line in lines if line.mute is not None and line.mute.active),
        )

    async def problems(self) -> ProjectsAttention:
        """Every project's problem lines whose mute is not active, as :meth:`project` counts them, oldest project first."""
        counts: list[ProjectProblems] = []
        for data in await self._loader.projects():
            book = MuteBook(data.mutes, data.now)
            lines = [finding.line(book.mute_of(finding)) for finding in build_report(data, PROBLEM_CHECKS).findings]
            counts.append(ProjectProblems(project_id=data.project.id, problems=_shown(lines, Severity.PROBLEM)))
        return ProjectsAttention(projects=counts)

    async def mute(self, project_id: str, key: str, duration: MuteDuration, actor: Actor) -> None:
        """Mute the project's line ``key`` for everyone in it, replacing its mute if it has one, and record it.

        Raises NotFoundError when there is no such project, or no such line now.
        """
        report = await self._report(project_id)
        if report is None:
            raise NotFoundError("Project not found")
        data, found = report
        finding = next((finding for finding in found.findings if finding.key == key), None)
        if finding is None:
            raise NotFoundError("Nothing with that key needs attention now")
        async with self._session_factory() as session:
            repository = ProjectRepository(session)
            stored = await repository.lock_attention_mutes(project_id)
            if stored is None:
                raise NotFoundError("Project not found")
            book = MuteBook(stored, utc_now())
            mutes = book.written(key, finding, duration, actor)
            await repository.write_attention_mutes(project_id, mutes)
            until = mutes[key]["until"]
            await actor.acted(
                session,
                AuditAction.MUTED,
                AuditEntityType.PROJECT,
                entity_id=data.project.id,
                name=data.project.name,
                project_id=data.project.id,
                details={"line": finding.sentence, "muted_until": until if until is not None else "it clears"},
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

    async def _report(self, project_id: str) -> tuple[ProjectData, Report] | None:
        """The project's data and its findings in display order; None when there is no such project, and no findings
        while it is pending deletion."""
        data = await self._loader.project(project_id)
        if data is None:
            return None
        if data.project.pending_deletion:
            return data, Report([], frozenset())
        return data, build_report(data, CHECKS)


def _shown(lines: Sequence[AttentionLine], severity: Severity) -> int:
    """The lines of ``severity`` that show: those whose mute does not hide them."""
    return sum(1 for line in lines if line.severity == severity and (line.mute is None or not line.mute.active))
