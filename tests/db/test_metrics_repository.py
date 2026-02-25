# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for MetricsRepository."""

from sqlalchemy.ext.asyncio import AsyncSession

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

    async def test_save_metrics_snapshot(
        self,
        metrics_repo: MetricsRepository,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test saving a metrics snapshot for a proxy."""
        project, credential, connector, proxy = await self._create_full_chain(
            project_repo, credential_repo, connector_repo, proxy_repo, db_session
        )

        await metrics_repo.save_metrics_snapshot(
            proxy_id=proxy.id,
            request_count=10,
            success_count=8,
            failure_count=2,
            avg_latency_ms=150.5,
            status="healthy",
            bytes_sent=5000,
            bytes_received=25000,
        )
        await db_session.commit()

        # Verify by getting history
        history = await metrics_repo.get_metrics_history(proxy.id)

        assert len(history) == 1
        assert history[0]["request_count"] == 10
        assert history[0]["success_count"] == 8
        assert history[0]["failure_count"] == 2
        assert history[0]["avg_latency_ms"] == 150.5
        assert history[0]["status"] == "healthy"
        assert history[0]["bytes_sent"] == 5000
        assert history[0]["bytes_received"] == 25000

    async def test_save_multiple_snapshots(
        self,
        metrics_repo: MetricsRepository,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test saving multiple metrics snapshots."""
        project, credential, connector, proxy = await self._create_full_chain(
            project_repo, credential_repo, connector_repo, proxy_repo, db_session
        )

        # Save multiple snapshots
        for i in range(3):
            await metrics_repo.save_metrics_snapshot(
                proxy_id=proxy.id,
                request_count=10 * (i + 1),
                success_count=8 * (i + 1),
                failure_count=2 * (i + 1),
                avg_latency_ms=100.0 + i * 10,
                status="healthy",
                bytes_sent=1000 * (i + 1),
                bytes_received=5000 * (i + 1),
            )
        await db_session.commit()

        history = await metrics_repo.get_metrics_history(proxy.id)

        assert len(history) == 3
        # History is ordered by timestamp desc, so most recent first
        assert history[0]["request_count"] == 30
        assert history[2]["request_count"] == 10

    async def test_get_cumulative_metrics(
        self,
        metrics_repo: MetricsRepository,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving cumulative metrics for all proxies."""
        project, credential, connector, proxy1 = await self._create_full_chain(
            project_repo, credential_repo, connector_repo, proxy_repo, db_session
        )

        # Create second proxy
        proxy2 = Proxy(
            host="proxy2.example.com",
            port=8081,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await proxy_repo.create(proxy2)
        await db_session.commit()

        # Save metrics for both proxies
        await metrics_repo.save_metrics_snapshot(
            proxy_id=proxy1.id,
            request_count=10,
            success_count=8,
            failure_count=2,
            avg_latency_ms=100.0,
            status="healthy",
            bytes_sent=5000,
            bytes_received=25000,
        )
        await metrics_repo.save_metrics_snapshot(
            proxy_id=proxy2.id,
            request_count=20,
            success_count=15,
            failure_count=5,
            avg_latency_ms=200.0,
            status="healthy",
            bytes_sent=10000,
            bytes_received=50000,
        )
        await db_session.commit()

        cumulative = await metrics_repo.get_cumulative_metrics_for_all_proxies()

        assert len(cumulative) == 2
        assert proxy1.id in cumulative
        assert proxy2.id in cumulative
        assert cumulative[proxy1.id]["request_count"] == 10
        assert cumulative[proxy2.id]["request_count"] == 20
        assert cumulative[proxy1.id]["bytes_sent"] == 5000
        assert cumulative[proxy1.id]["bytes_received"] == 25000
        assert cumulative[proxy2.id]["bytes_sent"] == 10000
        assert cumulative[proxy2.id]["bytes_received"] == 50000

    async def test_get_metrics_history_empty(
        self,
        metrics_repo: MetricsRepository,
    ) -> None:
        """Test retrieving metrics history for non-existent proxy."""
        history = await metrics_repo.get_metrics_history("non-existent-id")
        assert history == []

    async def test_get_metrics_history_with_limit(
        self,
        metrics_repo: MetricsRepository,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving metrics history with a limit."""
        project, credential, connector, proxy = await self._create_full_chain(
            project_repo, credential_repo, connector_repo, proxy_repo, db_session
        )

        # Save 5 snapshots
        for i in range(5):
            await metrics_repo.save_metrics_snapshot(
                proxy_id=proxy.id,
                request_count=10 * (i + 1),
                success_count=8 * (i + 1),
                failure_count=2 * (i + 1),
                avg_latency_ms=100.0,
                status="healthy",
                bytes_sent=1000 * (i + 1),
                bytes_received=5000 * (i + 1),
            )
        await db_session.commit()

        # Get only 2 most recent
        history = await metrics_repo.get_metrics_history(proxy.id, limit=2)

        assert len(history) == 2
        # Most recent should be first (request_count=50)
        assert history[0]["request_count"] == 50
        assert history[1]["request_count"] == 40

    # Project-level metrics tests

    async def test_save_project_metrics_snapshot(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test saving a project-level metrics snapshot."""
        project = Project(
            name="Project Metrics Test",
            username="projmetricsuser",
            password="pass",
        )
        await project_repo.create(project)
        await db_session.commit()

        await metrics_repo.save_project_metrics_snapshot(
            project_id=project.id,
            request_count=100,
            success_count=90,
            failure_count=10,
            avg_latency_ms=150.5,
            bytes_sent=50000,
            bytes_received=250000,
        )
        await db_session.commit()

        # Verify by getting history
        history = await metrics_repo.get_project_metrics_history(project.id)

        assert len(history) == 1
        assert history[0]["request_count"] == 100
        assert history[0]["success_count"] == 90
        assert history[0]["failure_count"] == 10
        assert history[0]["avg_latency_ms"] == 150.5
        assert history[0]["bytes_sent"] == 50000
        assert history[0]["bytes_received"] == 250000

    async def test_save_multiple_project_snapshots(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test saving multiple project metrics snapshots."""
        project = Project(
            name="Multi Snapshot Project",
            username="multisnapshotuser",
            password="pass",
        )
        await project_repo.create(project)
        await db_session.commit()

        # Save multiple snapshots
        for i in range(3):
            await metrics_repo.save_project_metrics_snapshot(
                project_id=project.id,
                request_count=100 * (i + 1),
                success_count=90 * (i + 1),
                failure_count=10 * (i + 1),
                avg_latency_ms=100.0 + i * 10,
                bytes_sent=10000 * (i + 1),
                bytes_received=50000 * (i + 1),
            )
        await db_session.commit()

        history = await metrics_repo.get_project_metrics_history(project.id)

        assert len(history) == 3
        # History is ordered by timestamp desc, so most recent first
        assert history[0]["request_count"] == 300
        assert history[2]["request_count"] == 100

    async def test_get_cumulative_project_metrics(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving cumulative metrics for all projects."""
        # Create two projects
        project1 = Project(
            name="Cumulative Project 1",
            username="cumproj1user",
            password="pass",
        )
        project2 = Project(
            name="Cumulative Project 2",
            username="cumproj2user",
            password="pass",
        )
        await project_repo.create(project1)
        await project_repo.create(project2)
        await db_session.commit()

        # Save metrics for both projects
        await metrics_repo.save_project_metrics_snapshot(
            project_id=project1.id,
            request_count=100,
            success_count=90,
            failure_count=10,
            avg_latency_ms=100.0,
            bytes_sent=50000,
            bytes_received=250000,
        )
        await metrics_repo.save_project_metrics_snapshot(
            project_id=project2.id,
            request_count=200,
            success_count=180,
            failure_count=20,
            avg_latency_ms=200.0,
            bytes_sent=100000,
            bytes_received=500000,
        )
        await db_session.commit()

        cumulative = await metrics_repo.get_cumulative_project_metrics()

        assert len(cumulative) == 2
        assert project1.id in cumulative
        assert project2.id in cumulative
        assert cumulative[project1.id]["request_count"] == 100
        assert cumulative[project2.id]["request_count"] == 200
        assert cumulative[project1.id]["bytes_sent"] == 50000
        assert cumulative[project1.id]["bytes_received"] == 250000
        assert cumulative[project2.id]["bytes_sent"] == 100000
        assert cumulative[project2.id]["bytes_received"] == 500000

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
        """Test retrieving project metrics history with a limit."""
        project = Project(
            name="Limit Test Project",
            username="limittestuser",
            password="pass",
        )
        await project_repo.create(project)
        await db_session.commit()

        # Save 5 snapshots
        for i in range(5):
            await metrics_repo.save_project_metrics_snapshot(
                project_id=project.id,
                request_count=100 * (i + 1),
                success_count=90 * (i + 1),
                failure_count=10 * (i + 1),
                avg_latency_ms=100.0,
                bytes_sent=10000 * (i + 1),
                bytes_received=50000 * (i + 1),
            )
        await db_session.commit()

        # Get only 2 most recent
        history = await metrics_repo.get_project_metrics_history(project.id, limit=2)

        assert len(history) == 2
        # Most recent should be first (request_count=500)
        assert history[0]["request_count"] == 500
        assert history[1]["request_count"] == 400

