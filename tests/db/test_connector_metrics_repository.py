# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the connector_metrics history: snapshots, period totals, compaction, retention."""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.db.models import ConnectorMetricsModel
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    MetricsRepository,
    ProjectRepository,
)
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project


async def _connector(
    project_repo: ProjectRepository,
    credential_repo: CredentialRepository,
    connector_repo: ConnectorRepository,
    session: AsyncSession,
    suffix: str = "",
) -> tuple[Project, Connector]:
    project = Project(name=f"Traffic Project{suffix}", username=f"traffic_user{suffix}", password="pass")
    await project_repo.create(project)
    credential = Credential(
        name=f"Cred{suffix}", type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={},
    )
    await credential_repo.create(credential)
    connector = Connector(
        name=f"Conn{suffix}", credential_id=credential.id,
        credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={},
        traffic_config={"limit_bytes": 1000, "action": "block"},
    )
    await connector_repo.create(connector)
    await session.commit()
    return project, connector


async def _insert(
    session: AsyncSession, connector_id: str, ts: datetime, *, sent: int = 10, received: int = 20,
    requests: int = 1, granularity: int = 60,
) -> None:
    session.add(ConnectorMetricsModel(
        connector_id=connector_id, timestamp=ts, request_count=requests, success_count=requests,
        failure_count=0, avg_latency_ms=100.0, bytes_sent=sent, bytes_received=received,
        granularity=granularity,
    ))
    await session.flush()


class TestConnectorRepositoryTrafficFields:
    async def test_traffic_config_and_reset_round_trip(
        self,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        _project, connector = await _connector(project_repo, credential_repo, connector_repo, db_session)
        loaded = await connector_repo.get_by_id(connector.id)
        assert loaded is not None
        assert loaded.traffic_config == {"limit_bytes": 1000, "action": "block"}
        assert loaded.traffic_reset_at is None

        reset_at = utc_now().replace(microsecond=0)
        connector.traffic_reset_at = reset_at
        connector.traffic_config = {"price_per_gb": 4.5}
        await connector_repo.update(connector)
        await db_session.commit()
        loaded = await connector_repo.get_by_id(connector.id)
        assert loaded is not None
        assert loaded.traffic_reset_at == reset_at
        assert loaded.traffic_config == {"price_per_gb": 4.5}


class TestConnectorMetrics:
    async def test_snapshot_and_history(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        _project, connector = await _connector(project_repo, credential_repo, connector_repo, db_session)
        await metrics_repo.save_connector_metrics_snapshot(
            connector_id=connector.id, request_count=4, success_count=3, failure_count=1,
            avg_latency_ms=42.0, bytes_sent=100, bytes_received=900,
        )
        await db_session.commit()
        history = await metrics_repo.get_connector_metrics_history(connector.id)
        assert len(history) == 1
        assert history[0]["request_count"] == 4
        assert history[0]["bytes_received"] == 900

    async def test_totals_since_use_each_connectors_window(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        _p1, a = await _connector(project_repo, credential_repo, connector_repo, db_session, "A")
        _p2, b = await _connector(project_repo, credential_repo, connector_repo, db_session, "B")
        now = utc_now()
        await _insert(db_session, a.id, now - timedelta(days=3), sent=1, received=1)
        await _insert(db_session, a.id, now - timedelta(hours=1), sent=10, received=20)
        await _insert(db_session, b.id, now - timedelta(days=3), sent=100, received=200, requests=5)
        await _insert(db_session, b.id, now - timedelta(hours=1), sent=1000, received=2000, requests=7)
        await db_session.commit()

        totals = await metrics_repo.get_connector_totals_since({
            a.id: now - timedelta(days=1),   # only the recent row
            b.id: now - timedelta(days=7),   # both rows
        })
        assert totals[a.id] == {"request_count": 1, "bytes_sent": 10, "bytes_received": 20}
        assert totals[b.id] == {"request_count": 12, "bytes_sent": 1100, "bytes_received": 2200}
        assert await metrics_repo.get_connector_totals_since({}) == {}
        assert await metrics_repo.get_connector_totals_since({a.id: now + timedelta(days=1)}) == {}

    async def test_aggregated_history_buckets(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        _project, connector = await _connector(project_repo, credential_repo, connector_repo, db_session)
        base = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
        for minute in (5, 25, 45):
            await _insert(db_session, connector.id, base + timedelta(minutes=minute), sent=100, received=100)
        await db_session.commit()
        rows = await metrics_repo.get_connector_metrics_history_aggregated(
            connector.id, since=base - timedelta(hours=1), bucket_seconds=3600
        )
        assert len(rows) == 1
        assert rows[0]["request_count"] == 3
        assert rows[0]["bytes_sent"] == 300

    async def test_compaction_preserves_totals(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        _project, connector = await _connector(project_repo, credential_repo, connector_repo, db_session)
        old = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
        for i in range(6):
            await _insert(db_session, connector.id, old + timedelta(minutes=i), sent=7, received=11)
        await db_session.commit()

        deleted = await metrics_repo.compact_connector_metrics(
            connector.id, older_than=utc_now() - timedelta(hours=24),
            source_granularity=60, target_granularity=3600,
        )
        await db_session.commit()
        assert deleted == 6
        assert await metrics_repo.get_connector_metrics_history(connector.id, granularity=60) == []
        hourly = await metrics_repo.get_connector_metrics_history(connector.id, granularity=3600)
        assert len(hourly) == 1
        assert hourly[0]["bytes_sent"] == 42
        assert hourly[0]["bytes_received"] == 66
        assert hourly[0]["request_count"] == 6
        totals = await metrics_repo.get_connector_totals_since({connector.id: old - timedelta(days=1)})
        assert totals[connector.id]["bytes_received"] == 66

    async def test_retention_and_connector_ids(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        project, connector = await _connector(project_repo, credential_repo, connector_repo, db_session)
        await _insert(db_session, connector.id, utc_now() - timedelta(days=100))
        await _insert(db_session, connector.id, utc_now() - timedelta(hours=1))
        await db_session.commit()
        assert await metrics_repo.get_connector_ids_for_project(project.id) == [connector.id]
        deleted = await metrics_repo.delete_connector_metrics_for_project_older_than(
            project.id, utc_now() - timedelta(days=90)
        )
        await db_session.commit()
        assert deleted == 1
        assert len(await metrics_repo.get_connector_metrics_history(connector.id)) == 1

    async def test_rows_go_with_the_connector(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        _project, connector = await _connector(project_repo, credential_repo, connector_repo, db_session)
        await _insert(db_session, connector.id, utc_now())
        await db_session.commit()
        assert await connector_repo.delete(connector.id)
        await db_session.commit()
        assert await metrics_repo.get_connector_metrics_history(connector.id) == []


class TestSnapshotsForGoneEntities:
    """A Redis hash can outlive its row; its snapshot is dropped, never a foreign key error."""

    async def test_unknown_parent_is_skipped_without_raising(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        _project, connector = await _connector(project_repo, credential_repo, connector_repo, db_session)
        common = {"request_count": 1, "success_count": 1, "failure_count": 0, "avg_latency_ms": 1.0}
        assert await metrics_repo.save_connector_metrics_snapshot(connector_id=connector.id, **common) is True
        assert await metrics_repo.save_connector_metrics_snapshot(connector_id="gone", **common) is False
        assert await metrics_repo.save_project_metrics_snapshot(project_id="gone", **common) is False
        assert await metrics_repo.save_metrics_snapshot(proxy_id="gone", status="healthy", **common) is False
        await db_session.commit()
        # The transaction survived the misses: the real row is there, nothing else.
        assert len(await metrics_repo.get_connector_metrics_history(connector.id)) == 1
        assert await metrics_repo.get_connector_metrics_history("gone") == []
