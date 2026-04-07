# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for metrics compaction and retention repository methods."""

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.db.models import ProjectMetricsModel, ProxyMetricsModel
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


async def _create_full_chain(
    project_repo: ProjectRepository,
    credential_repo: CredentialRepository,
    connector_repo: ConnectorRepository,
    proxy_repo: ProxyRepository,
    session: AsyncSession,
    suffix: str = "",
) -> tuple[Project, Proxy]:
    """Helper to create project -> credential -> connector -> proxy chain."""
    project = Project(
        name=f"Compaction Project{suffix}",
        username=f"compact_user{suffix}",
        password="pass",
    )
    await project_repo.create(project)

    credential = Credential(
        name=f"Cred{suffix}",
        type=CredentialType.STATIC_PROXY_PROVIDER,
        project_id=project.id,
        config={},
    )
    await credential_repo.create(credential)

    connector = Connector(
        name=f"Conn{suffix}",
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
    return project, proxy


async def _insert_project_metrics(
    session: AsyncSession,
    project_id: str,
    timestamp: object,
    request_count: int = 10,
    success_count: int = 8,
    failure_count: int = 2,
    avg_latency_ms: float = 100.0,
    bytes_sent: int = 1000,
    bytes_received: int = 5000,
    granularity: int = 60,
) -> None:
    """Insert a project metrics row with a specific timestamp."""
    model = ProjectMetricsModel(
        project_id=project_id,
        timestamp=timestamp,
        request_count=request_count,
        success_count=success_count,
        failure_count=failure_count,
        avg_latency_ms=avg_latency_ms,
        bytes_sent=bytes_sent,
        bytes_received=bytes_received,
        granularity=granularity,
    )
    session.add(model)


async def _insert_proxy_metrics(
    session: AsyncSession,
    proxy_id: str,
    timestamp: object,
    request_count: int = 10,
    success_count: int = 8,
    failure_count: int = 2,
    avg_latency_ms: float = 100.0,
    bytes_sent: int = 1000,
    bytes_received: int = 5000,
    status: str = "healthy",
    granularity: int = 60,
) -> None:
    """Insert a proxy metrics row with a specific timestamp."""
    model = ProxyMetricsModel(
        proxy_id=proxy_id,
        timestamp=timestamp,
        request_count=request_count,
        success_count=success_count,
        failure_count=failure_count,
        avg_latency_ms=avg_latency_ms,
        bytes_sent=bytes_sent,
        bytes_received=bytes_received,
        status=status,
        granularity=granularity,
    )
    session.add(model)


class TestCompactProjectMetrics:
    """Tests for compact_project_metrics."""

    async def test_compact_reduces_row_count(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        proxy_repo: ProxyRepository,
        db_session: AsyncSession,
    ) -> None:
        """Compacting 60 raw rows into 1-hour buckets drastically reduces rows."""
        project, _ = await _create_full_chain(
            project_repo, credential_repo, connector_repo, proxy_repo, db_session
        )

        # Insert 60 raw rows spanning 60 minutes, 2 days ago.
        # These may straddle 1-2 hour boundaries depending on the clock.
        base_time = utc_now() - timedelta(days=2)
        for i in range(60):
            ts = base_time + timedelta(minutes=i)
            await _insert_project_metrics(db_session, project.id, ts)
        await db_session.commit()

        # Verify 60 rows exist
        history = await metrics_repo.get_project_metrics_history(
            project.id, granularity=60
        )
        assert len(history) == 60

        # Compact: raw -> hourly for data older than 24h
        cutoff = utc_now() - timedelta(hours=24)
        deleted = await metrics_repo.compact_project_metrics(
            project_id=project.id,
            older_than=cutoff,
            source_granularity=60,
            target_granularity=3600,
        )
        await db_session.commit()

        assert deleted == 60

        # Raw rows should be gone
        raw = await metrics_repo.get_project_metrics_history(
            project.id, granularity=60
        )
        assert len(raw) == 0

        # Compacted rows: 1 or 2 depending on hour boundary, but far fewer than 60
        compacted = await metrics_repo.get_project_metrics_history(
            project.id, granularity=3600
        )
        assert 1 <= len(compacted) <= 2

    async def test_compact_preserves_totals(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        proxy_repo: ProxyRepository,
        db_session: AsyncSession,
    ) -> None:
        """Sum of all metrics is identical before and after compaction."""
        project, _ = await _create_full_chain(
            project_repo, credential_repo, connector_repo, proxy_repo, db_session
        )

        base_time = utc_now() - timedelta(days=2)
        total_requests = 0
        total_successes = 0
        total_bytes_sent = 0
        for i in range(10):
            req = 10 * (i + 1)
            suc = 8 * (i + 1)
            bs = 1000 * (i + 1)
            total_requests += req
            total_successes += suc
            total_bytes_sent += bs
            await _insert_project_metrics(
                db_session, project.id,
                base_time + timedelta(minutes=i),
                request_count=req,
                success_count=suc,
                failure_count=2 * (i + 1),
                bytes_sent=bs,
            )
        await db_session.commit()

        # Get cumulative before compaction
        before = await metrics_repo.get_cumulative_project_metrics()
        before_project = before[project.id]

        # Compact
        cutoff = utc_now() - timedelta(hours=24)
        await metrics_repo.compact_project_metrics(
            project_id=project.id,
            older_than=cutoff,
            source_granularity=60,
            target_granularity=3600,
        )
        await db_session.commit()

        # Get cumulative after compaction
        after = await metrics_repo.get_cumulative_project_metrics()
        after_project = after[project.id]

        assert after_project["request_count"] == before_project["request_count"]
        assert after_project["success_count"] == before_project["success_count"]
        assert after_project["failure_count"] == before_project["failure_count"]
        assert after_project["bytes_sent"] == before_project["bytes_sent"]
        assert after_project["bytes_received"] == before_project["bytes_received"]
        # Weighted avg latency should also match
        assert abs(
            after_project["avg_latency_ms"] - before_project["avg_latency_ms"]
        ) < 0.01

    async def test_compact_does_not_touch_recent_data(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        proxy_repo: ProxyRepository,
        db_session: AsyncSession,
    ) -> None:
        """Rows newer than the cutoff are left untouched."""
        project, _ = await _create_full_chain(
            project_repo, credential_repo, connector_repo, proxy_repo, db_session
        )

        # Insert old row (2 days ago) and recent row (1 hour ago)
        old_ts = utc_now() - timedelta(days=2)
        recent_ts = utc_now() - timedelta(hours=1)
        await _insert_project_metrics(db_session, project.id, old_ts, request_count=50)
        await _insert_project_metrics(db_session, project.id, recent_ts, request_count=25)
        await db_session.commit()

        cutoff = utc_now() - timedelta(hours=24)
        deleted = await metrics_repo.compact_project_metrics(
            project_id=project.id,
            older_than=cutoff,
            source_granularity=60,
            target_granularity=3600,
        )
        await db_session.commit()

        assert deleted == 1  # only old row

        # Recent raw row still exists
        raw = await metrics_repo.get_project_metrics_history(
            project.id, granularity=60
        )
        assert len(raw) == 1
        assert raw[0]["request_count"] == 25

    async def test_compact_noop_when_no_matching_rows(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Returns 0 when there's nothing to compact."""
        project = Project(
            name="Empty Project",
            username="empty_user",
            password="pass",
        )
        await project_repo.create(project)
        await db_session.commit()

        deleted = await metrics_repo.compact_project_metrics(
            project_id=project.id,
            older_than=utc_now(),
            source_granularity=60,
            target_granularity=3600,
        )
        assert deleted == 0


class TestCompactProxyMetrics:
    """Tests for compact_proxy_metrics."""

    async def test_compact_preserves_totals_and_latest_status(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        proxy_repo: ProxyRepository,
        db_session: AsyncSession,
    ) -> None:
        """Proxy compaction preserves totals and picks the latest status."""
        _, proxy = await _create_full_chain(
            project_repo, credential_repo, connector_repo, proxy_repo, db_session
        )

        # Anchor to a round hour so both rows land in the same 1-hour bucket
        now = utc_now()
        base_time = now.replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
        # First row: healthy, second: unhealthy (later = latest)
        await _insert_proxy_metrics(
            db_session, proxy.id, base_time + timedelta(minutes=5),
            request_count=10, success_count=8, failure_count=2,
            status="healthy",
        )
        await _insert_proxy_metrics(
            db_session, proxy.id, base_time + timedelta(minutes=35),
            request_count=20, success_count=15, failure_count=5,
            status="unhealthy",
        )
        await db_session.commit()

        # Get totals before
        before = await metrics_repo.get_cumulative_metrics_for_all_proxies()
        before_proxy = before[proxy.id]

        cutoff = now - timedelta(hours=24)
        deleted = await metrics_repo.compact_proxy_metrics(
            proxy_id=proxy.id,
            older_than=cutoff,
            source_granularity=60,
            target_granularity=3600,
        )
        await db_session.commit()

        assert deleted == 2

        # Totals preserved
        after = await metrics_repo.get_cumulative_metrics_for_all_proxies()
        after_proxy = after[proxy.id]
        assert after_proxy["request_count"] == before_proxy["request_count"]
        assert after_proxy["success_count"] == before_proxy["success_count"]
        assert after_proxy["bytes_sent"] == before_proxy["bytes_sent"]

        # Check the compacted row has the latest status
        history = await metrics_repo.get_metrics_history(proxy.id)
        assert len(history) == 1
        assert history[0]["status"] == "unhealthy"


class TestDeleteMetrics:
    """Tests for retention deletion methods."""

    async def test_delete_project_metrics_older_than(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Deletes only rows older than the cutoff."""
        project = Project(
            name="Retention Project",
            username="ret_user",
            password="pass",
        )
        await project_repo.create(project)
        await db_session.commit()

        old_ts = utc_now() - timedelta(days=100)
        recent_ts = utc_now() - timedelta(hours=1)
        await _insert_project_metrics(db_session, project.id, old_ts)
        await _insert_project_metrics(db_session, project.id, recent_ts)
        await db_session.commit()

        cutoff = utc_now() - timedelta(days=90)
        deleted = await metrics_repo.delete_project_metrics_older_than(
            project.id, cutoff
        )
        await db_session.commit()

        assert deleted == 1

        remaining = await metrics_repo.get_project_metrics_history(project.id)
        assert len(remaining) == 1

    async def test_delete_proxy_metrics_for_project(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        proxy_repo: ProxyRepository,
        db_session: AsyncSession,
    ) -> None:
        """Deletes proxy metrics for all proxies in a project via subquery."""
        project, proxy = await _create_full_chain(
            project_repo, credential_repo, connector_repo, proxy_repo, db_session
        )

        old_ts = utc_now() - timedelta(days=100)
        recent_ts = utc_now() - timedelta(hours=1)
        await _insert_proxy_metrics(db_session, proxy.id, old_ts)
        await _insert_proxy_metrics(db_session, proxy.id, recent_ts)
        await db_session.commit()

        cutoff = utc_now() - timedelta(days=90)
        deleted = await metrics_repo.delete_proxy_metrics_for_project_older_than(
            project.id, cutoff
        )
        await db_session.commit()

        assert deleted == 1

        remaining = await metrics_repo.get_metrics_history(proxy.id)
        assert len(remaining) == 1


class TestGranularityFilters:
    """Tests that query methods correctly filter by granularity."""

    async def test_raw_history_excludes_compacted_rows(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """get_project_metrics_history with granularity=60 ignores compacted rows."""
        project = Project(
            name="Filter Project",
            username="filter_user",
            password="pass",
        )
        await project_repo.create(project)
        await db_session.commit()

        ts = utc_now() - timedelta(hours=1)
        await _insert_project_metrics(
            db_session, project.id, ts, granularity=60, request_count=10
        )
        await _insert_project_metrics(
            db_session, project.id, ts, granularity=3600, request_count=100
        )
        await db_session.commit()

        raw = await metrics_repo.get_project_metrics_history(
            project.id, granularity=60
        )
        assert len(raw) == 1
        assert raw[0]["request_count"] == 10

    async def test_aggregated_includes_compacted_rows(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """get_project_metrics_history_aggregated includes rows with granularity <= bucket."""
        project = Project(
            name="Agg Filter Project",
            username="agg_filter_user",
            password="pass",
        )
        await project_repo.create(project)
        await db_session.commit()

        ts = utc_now() - timedelta(hours=1)
        # Raw row (granularity=60) — should be included for 3600s bucket
        await _insert_project_metrics(
            db_session, project.id, ts, granularity=60, request_count=10
        )
        # Compacted hourly row (granularity=3600) — should be included
        await _insert_project_metrics(
            db_session, project.id, ts - timedelta(hours=2), granularity=3600,
            request_count=100,
        )
        # Compacted 6-hourly row (granularity=21600) — should be EXCLUDED
        # because 21600 > 3600
        await _insert_project_metrics(
            db_session, project.id, ts - timedelta(hours=3), granularity=21600,
            request_count=999,
        )
        await db_session.commit()

        since = utc_now() - timedelta(days=1)
        rows = await metrics_repo.get_project_metrics_history_aggregated(
            project_id=project.id,
            since=since,
            bucket_seconds=3600,
        )

        total_requests = sum(r["request_count"] for r in rows)
        assert total_requests == 110  # 10 + 100, not 999
