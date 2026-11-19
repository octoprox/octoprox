# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The alerts job: what needs attention in every project and the install, or in one project after a change, stored as
alerts that open, stay, resolve and reopen.

The reads are a fixed number of statements however many projects there are; the router build and the checks run off
the event loop, and the alerts are written in one statement with the until-clear mutes their lines' clearing ends.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, cast

import structlog

from api.attention.alert import ALERTS_BATCH_SIZE, INSTALL_SETTLE, REOPEN_WINDOW, FoundAlert
from api.attention.checks import CHECKS, Finding, Report, build_report
from api.core import utc_now
from api.core.logging import error_text
from api.core.roles import JobKind
from api.db.alert_repository import AlertRepository
from api.db.redis import ALERT_CHECK_TTL_SECONDS, ALERT_INSTALL_SEEN_KEY, INSTALL_SCOPE
from api.db.repository import ProjectRepository
from api.jobs.job import Job

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from api.attention.data import ProjectData, ProjectDataLoader
    from api.attention.install import InstallChecks, InstallReport
    from api.attention.requests import AlertChecks
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.jobs.job import JobBatch

logger = structlog.get_logger()

# The key the controller publishes every minute: every project and the install.
CHECK_ALL = "all"


class AlertsJob(Job):
    """Checks every project and the install, or one project after a change, and stores the alerts each check opens,
    keeps, resolves and reopens; safe to run again, and any number at once."""

    kind = JobKind.ALERTS
    # A burst of run-now entries (a bulk delete asks once per proxy) is one run.
    batch_size = ALERTS_BATCH_SIZE

    def __init__(
        self,
        session_factory: SessionFactory,
        loader: ProjectDataLoader,
        install: InstallChecks,
        checks: AlertChecks,
        redis_client: RedisClient,
    ) -> None:
        self._session_factory = session_factory
        self._loader = loader
        self._install = install
        self._checks = checks
        self._redis = redis_client

    async def run(self, batch: JobBatch) -> None:
        """Load, check, then store the findings and end the cleared until-clear mutes in one transaction; mark the
        scopes checked once it commits.

        A batch naming one project checks that project; any other (``all``, or several projects) checks every project
        and the install.
        """
        started = time.monotonic()
        now = utc_now()
        project_id = batch.keys[0] if len(batch.keys) == 1 and batch.keys[0] != CHECK_ALL else None
        loaded = await self._loader.load(project_id, now)
        reports = await asyncio.to_thread(self._reports, loaded.datas)
        install: InstallReport | None = None
        settled: list[Finding] = []
        if project_id is None:
            install = await self._install.report(now)
            if install is not None:
                settled = await self._settle(install, now)

        found = [
            *(FoundAlert(scope, finding) for scope, report in reports.items() for finding in report.findings),
            *(FoundAlert(scope, finding, by) for scope, report in reports.items() for finding, by in report.hidden),
            *(FoundAlert("", finding) for finding in settled),
        ]
        checked = [*reports, ""] if install is not None else [*reports]
        held = [("", key) for key in install.held] if install is not None else []
        async with self._session_factory() as session:
            counts = await AlertRepository(session).reconcile(found, checked, held, now, reopen=REOPEN_WINDOW)
            ended = await ProjectRepository(session).end_cleared_mutes(list(reports), now, reopen=REOPEN_WINDOW)
            await session.commit()
        if ended:
            logger.info("Needs attention mutes ended: their lines cleared", projects=ended)

        scopes = [*reports, INSTALL_SCOPE] if install is not None else [*reports]
        try:
            await self._checks.mark_checked(scopes, now)
        except Exception as exc:
            # It only drives the card's re-check spinner and the staleness lines; the next run marks again.
            logger.warning("Alert checks not marked checked", scopes=len(scopes), error=error_text(exc))

        log = logger.info if counts.changed else logger.debug
        log(
            "Alerts checked",
            scope=project_id or CHECK_ALL,
            projects=len(reports),
            failed=len(loaded.datas) - len(reports) + len(loaded.failed),
            install=install is not None,
            opened=counts.opened,
            reopened=counts.reopened,
            resolved=counts.resolved,
            took_ms=round((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _reports(datas: Sequence[ProjectData]) -> dict[str, Report]:
        """Each project's report by id, oldest project first: none for one pending deletion, so its alerts resolve. A
        project whose checks raise is logged and left out, its alerts left as they are.

        Run in a thread: the countries check asks the router once per country a connector names.
        """
        reports: dict[str, Report] = {}
        for data in datas:
            if data.project.pending_deletion:
                reports[data.project.id] = Report([], [])
                continue
            try:
                reports[data.project.id] = build_report(data, CHECKS)
            except Exception as exc:
                logger.error("Alert checks skipped for a project", project_id=data.project.id, error=error_text(exc))
        return reports

    async def _settle(self, install: InstallReport, now: datetime) -> list[Finding]:
        """The install findings two checks at least ``INSTALL_SETTLE`` apart have seen, so a blip opens nothing.

        Each finding's first sighting is kept in a hash; one this run neither sees nor holds leaves it.
        """
        seen = await cast("Awaitable[dict[str, str]]", self._redis.client.hgetall(ALERT_INSTALL_SEEN_KEY))
        keys = {finding.key for finding in install.findings}
        pipe = self._redis.client.pipeline(transaction=False)
        for key in keys:
            pipe.hsetnx(ALERT_INSTALL_SEEN_KEY, key, now.isoformat(timespec="microseconds"))
        gone = [key for key in seen if key not in keys and key not in install.held]
        if gone:
            pipe.hdel(ALERT_INSTALL_SEEN_KEY, *gone)
        pipe.expire(ALERT_INSTALL_SEEN_KEY, ALERT_CHECK_TTL_SECONDS)
        await pipe.execute()
        return [finding for finding in install.findings if _settled(seen, finding.key, now)]


def _settled(seen: Mapping[str, str], key: str, now: datetime) -> bool:
    """Whether ``key`` was first seen at least ``INSTALL_SETTLE`` before ``now``."""
    stored = seen.get(key)
    if not stored:
        return False
    try:
        return datetime.fromisoformat(stored) <= now - INSTALL_SETTLE
    except ValueError:
        return False
