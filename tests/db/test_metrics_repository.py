# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for MetricsRepository: the flusher's batch inserts and the project history they feed."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.stats import MetricDelta
from api.db.models import ProxyMetricsModel
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    MetricsRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol


class TestMetricsRepository:
    """Tests for MetricsRepository operations."""

    async def _create_full_chain(
        self,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        proxy_repo: ProxyRepository,
        session: AsyncSession,
        suffix: str = "",
    ) -> tuple[Project, Credential, Connector, Proxy]:
        """Helper to create full chain of entities for metrics tests."""
        project = Project(
            name=f"Test Project{suffix}",
            username=f"user{suffix}",
            password="pass",
        )
        await project_repo.create(project)

        credential = Credential(
            name=f"Test Credential{suffix}",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await credential_repo.create(credential)

        connector = Connector(
            name=f"Test Connector{suffix}",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
            enabled=True,
        )
        await connector_repo.create(connector)

        proxy = Proxy(
            host=f"proxy{suffix}.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await proxy_repo.create(proxy)
        await session.commit()

        return project, credential, connector, proxy

    async def test_batch_snapshots_keep_known_rows_and_report_gone_ones(
        self,
        metrics_repo: MetricsRepository,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """The flusher's batch inserts write a row per known entity and return the ids that are gone."""
        project, _credential, connector, proxy = await self._create_full_chain(
            project_repo, credential_repo, connector_repo, proxy_repo, db_session
        )
        delta = MetricDelta(
            request_count=4, success_count=3, failure_count=1, latency_sum_ms=600.0, bytes_sent=50, bytes_received=70
        )

        dropped = await metrics_repo.save_proxy_metrics_snapshots(
            {proxy.id: delta, "gone-proxy": delta}, {proxy.id: "healthy"}
        )
        assert dropped == ["gone-proxy"]
        assert await metrics_repo.save_project_metrics_snapshots({project.id: delta, "gone": delta}) == ["gone"]
        assert await metrics_repo.save_connector_metrics_snapshots({connector.id: delta, "gone": delta}) == ["gone"]
        await db_session.commit()

        [proxy_row] = (
            await db_session.execute(select(ProxyMetricsModel).where(ProxyMetricsModel.proxy_id == proxy.id))
        ).scalars().all()
        # Over the three successes.
        assert (proxy_row.request_count, proxy_row.avg_latency_ms, proxy_row.status) == (4, 200.0, "healthy")
        assert (proxy_row.bytes_sent, proxy_row.bytes_received) == (50, 70)
        [project_row] = await metrics_repo.get_project_metrics_history(project.id)
        assert (project_row["request_count"], project_row["failure_count"]) == (4, 1)
        [connector_row] = await metrics_repo.get_connector_metrics_history(connector.id)
        assert (connector_row["request_count"], connector_row["success_count"]) == (4, 3)

    async def test_a_batch_larger_than_one_parent_lookup(
        self,
        metrics_repo: MetricsRepository,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Parents are looked up in slices; one found only in the last slice still gets its row."""
        _project, _credential, connector, _proxy = await self._create_full_chain(
            project_repo, credential_repo, connector_repo, proxy_repo, db_session
        )
        delta = MetricDelta(request_count=1, success_count=1)
        batch = {f"gone-{i}": delta for i in range(10_500)}
        batch[connector.id] = delta

        dropped = await metrics_repo.save_connector_metrics_snapshots(batch)
        await db_session.commit()

        assert len(dropped) == 10_500 and connector.id not in dropped
        assert len(await metrics_repo.get_connector_metrics_history(connector.id)) == 1

    # Project-level metrics tests

    async def test_a_project_window_reads_back_from_its_history(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """A flushed window becomes one history row, its latency the average over the window's successes."""
        project = Project(name="Project Metrics Test", username="projmetricsuser", password="pass")
        await project_repo.create(project)
        await db_session.commit()

        window = MetricDelta(
            request_count=100,
            success_count=90,
            failure_count=10,
            latency_sum_ms=13545.0,
            bytes_sent=50000,
            bytes_received=250000,
        )
        assert await metrics_repo.save_project_metrics_snapshots({project.id: window}) == []
        await db_session.commit()

        history = await metrics_repo.get_project_metrics_history(project.id)

        assert len(history) == 1
        assert (history[0]["request_count"], history[0]["success_count"], history[0]["failure_count"]) == (100, 90, 10)
        assert history[0]["avg_latency_ms"] == 150.5
        assert (history[0]["bytes_sent"], history[0]["bytes_received"]) == (50000, 250000)

    async def test_project_history_is_newest_first(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        project = Project(name="Multi Snapshot Project", username="multisnapshotuser", password="pass")
        await project_repo.create(project)
        await db_session.commit()

        for i in range(3):
            await metrics_repo.save_project_metrics_snapshots({project.id: MetricDelta(request_count=100 * (i + 1))})
        await db_session.commit()

        history = await metrics_repo.get_project_metrics_history(project.id)

        assert [row["request_count"] for row in history] == [300, 200, 100]

    async def test_get_project_metrics_history_empty(
        self,
        metrics_repo: MetricsRepository,
    ) -> None:
        """Test retrieving project metrics history for non-existent project."""
        history = await metrics_repo.get_project_metrics_history("non-existent-id")
        assert history == []

    async def test_get_project_metrics_history_with_limit(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """The most recent rows, newest first."""
        project = Project(name="Limit Test Project", username="limittestuser", password="pass")
        await project_repo.create(project)
        await db_session.commit()

        for i in range(5):
            await metrics_repo.save_project_metrics_snapshots({project.id: MetricDelta(request_count=100 * (i + 1))})
        await db_session.commit()

        history = await metrics_repo.get_project_metrics_history(project.id, limit=2)

        assert [row["request_count"] for row in history] == [500, 400]
