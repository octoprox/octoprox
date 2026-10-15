# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the failure_metrics history: bulk snapshots, window queries, compaction, retention, cascades."""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.db.models import FailureMetricsModel
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    MetricsRepository,
    ProjectRepository,
)
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project


async def _project_with_connector(
    project_repo: ProjectRepository,
    credential_repo: CredentialRepository,
    connector_repo: ConnectorRepository,
    session: AsyncSession,
    suffix: str = "",
) -> tuple[Project, Connector]:
    project = Project(name=f"Failures Project{suffix}", username=f"failures_user{suffix}", password="pass")
    await project_repo.create(project)
    credential = Credential(
        name=f"Cred{suffix}", type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={},
    )
    await credential_repo.create(credential)
    connector = Connector(
        name=f"Conn{suffix}", credential_id=credential.id,
        credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={},
    )
    await connector_repo.create(connector)
    await session.commit()
    return project, connector


async def _insert(
    session: AsyncSession, project_id: str, connector_id: str | None, kind: str, ts: datetime, *,
    count: int = 1, upstream_status: int | None = None, granularity: int = 60,
) -> None:
    session.add(FailureMetricsModel(
        project_id=project_id, connector_id=connector_id, kind=kind, upstream_status=upstream_status,
        timestamp=ts, failure_count=count, granularity=granularity,
    ))


async def _row_count(session: AsyncSession, project_id: str, granularity: int | None = None) -> int:
    query = select(func.count()).select_from(FailureMetricsModel).where(FailureMetricsModel.project_id == project_id)
    if granularity is not None:
        query = query.where(FailureMetricsModel.granularity == granularity)
    return int((await session.execute(query)).scalar() or 0)


class TestFailureMetrics:
    async def test_snapshot_keeps_known_projects_and_connectors(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        project, connector = await _project_with_connector(project_repo, credential_repo, connector_repo, db_session)
        other, other_connector = await _project_with_connector(
            project_repo, credential_repo, connector_repo, db_session, suffix="-2"
        )
        dropped = await metrics_repo.save_failure_metrics_snapshots({
            (project.id, connector.id, "upstream_rejected", 407): 3,
            (project.id, None, "no_proxy_available", None): 2,
            (project.id, other_connector.id, "upstream_timeout", None): 1,  # not this project's connector
            ("gone", None, "no_proxy_available", None): 1,
        })
        await db_session.commit()
        assert set(dropped) == {
            (project.id, other_connector.id, "upstream_timeout", None),
            ("gone", None, "no_proxy_available", None),
        }
        assert await _row_count(db_session, project.id) == 2
        assert await _row_count(db_session, other.id) == 0
        assert await metrics_repo.save_failure_metrics_snapshots({}) == []

    async def test_breakdown_and_history_cover_the_window(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        project, connector = await _project_with_connector(project_repo, credential_repo, connector_repo, db_session)
        now = utc_now()
        await _insert(db_session, project.id, connector.id, "upstream_rejected", now, count=4, upstream_status=407)
        await _insert(db_session, project.id, connector.id, "upstream_rejected", now - timedelta(minutes=5), count=2,
                      upstream_status=407)
        await _insert(db_session, project.id, None, "no_proxy_available", now, count=1)
        await _insert(db_session, project.id, connector.id, "upstream_timeout", now - timedelta(hours=3), count=50)
        await db_session.commit()

        breakdown = await metrics_repo.get_failure_breakdown(project.id, now - timedelta(hours=1))
        assert breakdown == [
            {"kind": "upstream_rejected", "upstream_status": 407, "connector_id": connector.id, "failure_count": 6},
            {"kind": "no_proxy_available", "upstream_status": None, "connector_id": None, "failure_count": 1},
        ]
        history = await metrics_repo.get_failure_history(project.id, now - timedelta(hours=1), 3600)
        assert sum(p["failure_count"] for p in history if p["kind"] == "upstream_rejected") == 6
        assert {p["kind"] for p in history} == {"upstream_rejected", "no_proxy_available"}

    async def test_compaction_keeps_the_sums(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        project, connector = await _project_with_connector(project_repo, credential_repo, connector_repo, db_session)
        base = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
        for minute in range(30):
            ts = base + timedelta(minutes=minute)
            await _insert(db_session, project.id, connector.id, "upstream_rejected", ts, count=2, upstream_status=407)
            await _insert(db_session, project.id, connector.id, "upstream_rejected", ts, count=1, upstream_status=502)
            await _insert(db_session, project.id, None, "no_proxy_available", ts, count=1)
        await _insert(db_session, project.id, None, "no_proxy_available", utc_now(), count=9)
        await db_session.commit()

        compacted = await metrics_repo.compact_failure_metrics(
            project_id=project.id, older_than=utc_now() - timedelta(hours=24),
            source_granularity=60, target_granularity=3600,
        )
        await db_session.commit()
        assert compacted == 90
        assert await _row_count(db_session, project.id, granularity=60) == 1
        assert await _row_count(db_session, project.id, granularity=3600) == 3
        breakdown = await metrics_repo.get_failure_breakdown(project.id, utc_now() - timedelta(days=3))
        assert {(r["kind"], r["upstream_status"]): r["failure_count"] for r in breakdown} == {
            ("upstream_rejected", 407): 60,
            ("upstream_rejected", 502): 30,
            ("no_proxy_available", None): 39,
        }
        assert await metrics_repo.compact_failure_metrics(project.id, utc_now() - timedelta(hours=24), 60, 3600) == 0

    async def test_retention_and_cascade(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        project, connector = await _project_with_connector(project_repo, credential_repo, connector_repo, db_session)
        await _insert(db_session, project.id, None, "no_proxy_available", utc_now() - timedelta(days=100))
        await _insert(db_session, project.id, None, "no_proxy_available", utc_now())
        await _insert(db_session, project.id, connector.id, "upstream_timeout", utc_now())
        await db_session.commit()

        assert await metrics_repo.delete_failure_metrics_older_than(project.id, utc_now() - timedelta(days=90)) == 1
        await db_session.commit()
        await connector_repo.delete(connector.id)
        await db_session.commit()
        breakdown = await metrics_repo.get_failure_breakdown(project.id, utc_now() - timedelta(hours=1))
        assert [r["kind"] for r in breakdown] == ["no_proxy_available"]
