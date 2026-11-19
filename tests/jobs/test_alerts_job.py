# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The alerts job over the real Postgres and Redis: what a run opens, keeps, resolves and reopens, project by project
and for the install, the until-clear mutes it ends, and what it leaves alone when it cannot judge."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.attention import checks as checks_module
from api.attention.alert import ALERTS_BATCH_SIZE, INSTALL_SETTLE, REOPEN_WINDOW
from api.attention.checks import Finding
from api.attention.data import ProjectDataLoader
from api.attention.install import InstallChecks, InstallKind, InstallReport
from api.attention.requests import AlertChecks
from api.core import utc_now
from api.core.demand_tracker import DemandTracker
from api.core.roles import JobKind
from api.core.system_stats import ClusterReader
from api.db.models import AlertModel, AuditLogModel
from api.db.redis import INSTALL_SCOPE, JOB_STREAM_KEY, RedisClient
from api.db.repository import ConnectorRepository, ProjectRepository
from api.db.streams import StreamEntry
from api.feed.compiler import FeedCompiler
from api.feed.preview import RoutingPreview
from api.feed.state import RuntimeStateReader
from api.geo.models import GeoSettings
from api.jobs import alerts as alerts_module
from api.jobs.alerts import AlertsJob
from api.jobs.job import JobBatch
from api.jobs.queue import JobQueue
from api.models.attention import Severity
from api.models.connector import Connector
from api.models.project import Project
from api.providers.registry import ProviderRegistry
from api.services.read_model import RuntimeView
from tests.jobs.conftest import Rows, batch_of


class ScriptedInstall(InstallChecks):
    """The install check, its answer set by the test: what it finds, what it holds, or None for a failed read."""

    def __init__(self, redis_client: RedisClient, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(redis_client, sessions, ClusterReader(redis_client), geo_updater_interval=3600)
        self.answer: InstallReport | None = InstallReport(findings=[], held=frozenset())
        self.asked = 0

    async def report(self, now: datetime) -> InstallReport | None:
        self.asked += 1
        return self.answer

    def finds(self, *kinds: InstallKind, held: frozenset[str] = frozenset()) -> None:
        self.answer = InstallReport(findings=[_install(kind) for kind in kinds], held=held)


def _install(kind: InstallKind) -> Finding:
    return Finding(
        key=kind.value,
        kind=kind,
        severity=Severity.PROBLEM if kind == InstallKind.NO_GATEWAY_SERVING else Severity.WARNING,
        lead="",
        subject="",
        text="Something install-wide.",
        link="system",
        action="",
    )


class Harness:
    """The alerts job on the test stores, on a clock the test moves, and the alerts it leaves."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        registry: ProviderRegistry,
        rows: Rows,
    ) -> None:
        self.sessions = sessions
        self.redis = redis_client
        self.rows = rows
        self.now = utc_now()
        view = RuntimeView(sessions, RuntimeStateReader(redis_client), registry, DemandTracker(redis_client), strict=True)
        compiler = FeedCompiler(sessions, registry, geo_defaults=GeoSettings(), default_strategy="round_robin")
        preview = RoutingPreview(compiler, view, registry, default_strategy="round_robin", off_loop=True)
        self.install = ScriptedInstall(redis_client, sessions)
        self.checks = AlertChecks(redis_client, JobQueue(redis_client))
        self.job = AlertsJob(
            sessions, ProjectDataLoader(sessions, view, registry, preview), self.install, self.checks, redis_client
        )

    async def run(self, *keys: str, after: timedelta = timedelta(minutes=1)) -> None:
        """One run of the batch ``keys`` (``all`` when none), ``after`` the last."""
        self.now += after
        await self.job.run(batch_of(*(keys or ("all",))))

    async def alerts(self, scope: str | None = None) -> dict[str, AlertModel]:
        """Each alert of a project (the install's for ''), by key; the newest when a key has several."""
        async with self.sessions() as session:
            query = select(AlertModel).order_by(AlertModel.opened_at)
            query = query.where(
                AlertModel.project_id.is_(None) if scope == "" else AlertModel.project_id == scope
            )
            return {row.key: row for row in (await session.execute(query)).scalars()}

    async def open(self, scope: str) -> set[str]:
        return {key for key, row in (await self.alerts(scope)).items() if row.resolved_at is None}

    async def enable(self, connector: Connector, enabled: bool) -> None:
        async with self.sessions() as session:
            await ConnectorRepository(session).update(connector.model_copy(update={"enabled": enabled}))
            await session.commit()

    async def mutes(self, project: Project) -> dict[str, Any]:
        async with self.sessions() as session:
            return (await ProjectRepository(session).attention_mutes([project.id]))[project.id]

    async def checked(self, scope: str) -> datetime | None:
        return (await self.checks.state(scope, self.now)).checked_at


@pytest.fixture
def harness(
    sessions: async_sessionmaker[AsyncSession],
    redis_client: RedisClient,
    registry: ProviderRegistry,
    rows: Rows,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Harness]:
    made = Harness(sessions, redis_client, registry, rows)
    monkeypatch.setattr(alerts_module, "utc_now", lambda: made.now)
    yield made


class TestProjects:
    async def test_an_all_run_opens_each_project_s_alerts_and_marks_every_scope_checked(
        self, harness: Harness, rows: Rows
    ) -> None:
        empty = await rows.project()
        served = await rows.project()
        await rows.connector(served)

        await harness.run()

        assert await harness.open(empty.id) == {"connectors_off"}
        row = (await harness.alerts(empty.id))["connectors_off"]
        assert (row.severity, row.body, row.opened_at) == ("problem", "Add a connector to start serving requests.", harness.now)
        assert await harness.open(served.id) == set()
        for scope in (empty.id, served.id, INSTALL_SCOPE):
            assert await harness.checked(scope) == harness.now
        assert harness.install.asked == 1

    async def test_a_fix_resolves_at_once_and_its_return_within_the_window_reopens_the_same_alert(
        self, harness: Harness, rows: Rows
    ) -> None:
        project = await rows.project()
        connector = await rows.connector(project, enabled=False)
        await harness.run()
        opened = (await harness.alerts(project.id))["connectors_off"]

        await harness.enable(connector, True)
        await harness.run(project.id)
        assert (await harness.alerts(project.id))["connectors_off"].resolved_at == harness.now

        await harness.enable(connector, False)
        await harness.run(project.id, after=REOPEN_WINDOW - timedelta(minutes=1))
        back = (await harness.alerts(project.id))["connectors_off"]
        assert (back.id, back.opened_at, back.reopened_at, back.reopened_count, back.resolved_at) == (
            opened.id, opened.opened_at, harness.now, 1, None
        )

    async def test_a_project_run_touches_that_project_alone(self, harness: Harness, rows: Rows) -> None:
        one, two = await rows.project(), await rows.project()
        await harness.run()
        await rows.connector(one)
        await rows.connector(two)

        await harness.run(one.id)

        assert await harness.open(one.id) == set()
        # Not checked: its alert stays open until a run checks it, and nothing marks it.
        assert await harness.open(two.id) == {"connectors_off"}
        assert await harness.checked(two.id) == harness.now - timedelta(minutes=1)
        assert await harness.checked(one.id) == harness.now
        assert harness.install.asked == 1

    async def test_a_burst_of_run_now_entries_for_one_project_is_one_run_of_it(
        self, harness: Harness, rows: Rows, redis_client: RedisClient
    ) -> None:
        project = await rows.project()
        for _ in range(ALERTS_BATCH_SIZE):
            await harness.checks.request(project.id)

        entries = await redis_client.client.xrange(JOB_STREAM_KEY.format(kind=JobKind.ALERTS))
        batch = JobBatch.of([StreamEntry(entry_id, fields) for entry_id, fields in entries])
        assert (len(batch.ids), batch.keys) == (ALERTS_BATCH_SIZE, (project.id,))
        assert AlertsJob.batch_size == ALERTS_BATCH_SIZE

        harness.now += timedelta(minutes=1)
        await harness.job.run(batch)
        assert await harness.open(project.id) == {"connectors_off"}
        assert harness.install.asked == 0

    async def test_a_batch_naming_two_projects_checks_every_project_and_the_install(
        self, harness: Harness, rows: Rows
    ) -> None:
        one, two, three = await rows.project(), await rows.project(), await rows.project()

        await harness.run(one.id, two.id)

        assert all([await harness.open(project.id) == {"connectors_off"} for project in (one, two, three)])
        assert harness.install.asked == 1

    async def test_a_project_pending_deletion_resolves_its_alerts(self, harness: Harness, rows: Rows) -> None:
        project = await rows.project()
        await harness.run()
        async with harness.sessions() as session:
            await ProjectRepository(session).mark_pending_deletion(project.id)
            await session.commit()

        await harness.run(project.id)

        assert await harness.open(project.id) == set()

    async def test_a_project_whose_checks_raise_is_left_as_it_was_and_the_others_carry_on(
        self, harness: Harness, rows: Rows, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broken, fine = await rows.project(), await rows.project()
        await harness.run()
        fixed = await rows.connector(fine)
        first = harness.now
        found = checks_module.ConnectorsOffCheck.findings

        def raising(check: checks_module.ConnectorsOffCheck, data: Any) -> list[Finding]:
            if data.project.id == broken.id:
                raise RuntimeError("a bug in a check")
            return found(check, data)

        monkeypatch.setattr(checks_module.ConnectorsOffCheck, "findings", raising)
        await rows.connector(broken)
        await harness.run()

        assert await harness.open(broken.id) == {"connectors_off"}
        assert await harness.checked(broken.id) == first
        assert await harness.open(fine.id) == set()
        assert await harness.checked(fine.id) == harness.now
        assert fixed.project_id == fine.id

    async def test_a_redis_failure_in_the_loads_fails_the_run_and_leaves_everything_as_it_was(
        self, harness: Harness, rows: Rows, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = await rows.project()
        await harness.run()
        mutes = {"connectors_off": _until_clear(harness.now)}
        async with harness.sessions() as session:
            await ProjectRepository(session).write_attention_mutes(project.id, mutes)
            await session.commit()
        await rows.connector(project)

        async def refused(self: RuntimeStateReader) -> dict[str, datetime]:
            raise RedisConnectionError("Redis is away")

        monkeypatch.setattr(RuntimeStateReader, "quarantined", refused)
        with pytest.raises(RedisConnectionError):
            await harness.run(after=REOPEN_WINDOW * 2)

        assert await harness.open(project.id) == {"connectors_off"}
        assert await harness.mutes(project) == mutes


def _until_clear(at: datetime) -> dict[str, Any]:
    return {"severity": "problem", "duration": "until_clear", "until": None, "by": "ivan", "at": at.isoformat()}


class TestMutes:
    async def test_an_until_clear_mute_ends_once_its_alert_stayed_resolved_for_the_window_with_no_history(
        self, harness: Harness, rows: Rows
    ) -> None:
        project = await rows.project()
        connector = await rows.connector(project, enabled=False)
        await harness.run()
        async with harness.sessions() as session:
            await ProjectRepository(session).write_attention_mutes(
                project.id, {"connectors_off": _until_clear(harness.now)}
            )
            await session.commit()

        await harness.enable(connector, True)
        await harness.run(project.id)
        # Resolved, and within the window: a flapping line comes back still muted.
        assert set(await harness.mutes(project)) == {"connectors_off"}
        await harness.run(project.id, after=REOPEN_WINDOW - timedelta(seconds=1))
        assert set(await harness.mutes(project)) == {"connectors_off"}

        await harness.run(project.id, after=timedelta(seconds=2))
        assert await harness.mutes(project) == {}
        async with harness.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(AuditLogModel)) == 0


class TestInstall:
    async def test_a_condition_opens_only_once_two_checks_45_seconds_apart_have_seen_it(self, harness: Harness) -> None:
        harness.install.finds(InstallKind.NO_GATEWAY_SERVING)
        await harness.run()
        assert await harness.open("") == set()

        await harness.run(after=INSTALL_SETTLE - timedelta(seconds=1))
        assert await harness.open("") == set()

        await harness.run(after=timedelta(seconds=1))
        assert await harness.open("") == {"no_gateway_serving"}
        row = (await harness.alerts(""))["no_gateway_serving"]
        assert (row.severity, row.link, row.action, row.project_id) == ("problem", "system", "", None)

    async def test_a_blip_seen_once_never_opens(self, harness: Harness) -> None:
        harness.install.finds(InstallKind.BACKGROUND_BEHIND)
        await harness.run()
        harness.install.finds()
        await harness.run()
        harness.install.finds(InstallKind.BACKGROUND_BEHIND)
        await harness.run(after=INSTALL_SETTLE)

        assert await harness.alerts("") == {}

    async def test_a_resolved_condition_reopens_once_it_settles_again(self, harness: Harness) -> None:
        harness.install.finds(InstallKind.IP_DATABASES)
        await harness.run()
        await harness.run()
        assert await harness.open("") == {"ip_databases"}

        harness.install.finds()
        await harness.run()
        assert await harness.open("") == set()

        harness.install.finds(InstallKind.IP_DATABASES)
        await harness.run()
        assert await harness.open("") == set()
        await harness.run()
        row = (await harness.alerts(""))["ip_databases"]
        assert (row.resolved_at, row.reopened_count) == (None, 1)

    async def test_a_skipped_check_or_a_held_key_leaves_the_install_alerts_as_they_were(self, harness: Harness) -> None:
        harness.install.finds(InstallKind.CHANGES_NOT_APPLIED, InstallKind.BACKGROUND_BEHIND)
        await harness.run()
        await harness.run()
        assert await harness.open("") == {"changes_not_applied", "background_behind"}
        checked = await harness.checked(INSTALL_SCOPE)

        harness.install.answer = None
        await harness.run()
        assert await harness.open("") == {"changes_not_applied", "background_behind"}
        assert await harness.checked(INSTALL_SCOPE) == checked

        harness.install.finds(
            InstallKind.STORE_UNREACHABLE, held=frozenset({"changes_not_applied", "background_behind"})
        )
        await harness.run()
        await harness.run()
        assert await harness.open("") == {"changes_not_applied", "background_behind", "store_unreachable"}

    async def test_marks_never_move_a_newer_check_back(self, harness: Harness) -> None:
        later = harness.now + timedelta(minutes=5)

        await harness.checks.mark_checked(["p", INSTALL_SCOPE], later)
        await harness.checks.mark_checked(["p"], harness.now)

        assert (await harness.checks.state("p", later)).checked_at == later
        assert (await harness.checks.state(INSTALL_SCOPE, later)).checked_at == later
