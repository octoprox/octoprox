# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the host_metrics history: bulk snapshots, window queries, compaction, retention."""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.core.stats import HOST_OVERFLOW, MetricDelta
from api.db.models import HostMetricsModel
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    MetricsRepository,
    ProjectRepository,
)
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project


async def _project_with_connectors(
    project_repo: ProjectRepository,
    credential_repo: CredentialRepository,
    connector_repo: ConnectorRepository,
    session: AsyncSession,
    count: int = 2,
    suffix: str = "",
) -> tuple[Project, list[Connector]]:
    project = Project(name=f"Hosts Project{suffix}", username=f"hosts_user{suffix}", password="pass")
    await project_repo.create(project)
    credential = Credential(
        name=f"Cred{suffix}", type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={},
    )
    await credential_repo.create(credential)
    connectors = []
    for i in range(count):
        connector = Connector(
            name=f"Conn{suffix}-{i}", credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={},
        )
        await connector_repo.create(connector)
        connectors.append(connector)
    await session.commit()
    return project, connectors


async def _insert(
    session: AsyncSession, project_id: str, connector_id: str, host: str, ts: datetime, *,
    requests: int = 1, failures: int = 0, sent: int = 10, received: int = 20, latency: float = 100.0,
    granularity: int = 60,
) -> None:
    session.add(HostMetricsModel(
        project_id=project_id, connector_id=connector_id, host=host, timestamp=ts,
        request_count=requests, success_count=requests - failures, failure_count=failures,
        avg_latency_ms=latency, bytes_sent=sent, bytes_received=received, granularity=granularity,
    ))
    await session.flush()


async def _row_count(session: AsyncSession, project_id: str, granularity: int | None = None) -> int:
    query = select(func.count()).select_from(HostMetricsModel).where(HostMetricsModel.project_id == project_id)
    if granularity is not None:
        query = query.where(HostMetricsModel.granularity == granularity)
    return int((await session.execute(query)).scalar() or 0)


class TestSaveHostMetricsSnapshots:
    async def test_bulk_insert_keeps_known_connectors_and_drops_orphans(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        project, (a, b) = await _project_with_connectors(project_repo, credential_repo, connector_repo, db_session)
        batch = {
            (project.id, a.id, "shop.example.com"): MetricDelta(request_count=3, success_count=3, latency_sum_ms=300, bytes_sent=30, bytes_received=60),
            (project.id, b.id, "shop.example.com"): MetricDelta(request_count=1, success_count=0, failure_count=1, latency_sum_ms=900),
            # A hash whose connector is gone, and one that claims the wrong project.
            (project.id, "gone-connector", "x.example.com"): MetricDelta(request_count=1, success_count=1),
            ("other-project", a.id, "y.example.com"): MetricDelta(request_count=1, success_count=1),
        }
        dropped = await metrics_repo.save_host_metrics_snapshots(batch)
        await db_session.commit()

        assert set(dropped) == {("other-project", a.id, "y.example.com"), (project.id, "gone-connector", "x.example.com")}
        rows = (await db_session.execute(
            select(HostMetricsModel).where(HostMetricsModel.project_id == project.id).order_by(HostMetricsModel.connector_id)
        )).scalars().all()
        assert len(rows) == 2
        by_connector = {r.connector_id: r for r in rows}
        assert by_connector[a.id].request_count == 3
        assert by_connector[a.id].avg_latency_ms == 100.0
        assert by_connector[a.id].bytes_received == 60
        assert by_connector[a.id].granularity == 60
        assert by_connector[b.id].failure_count == 1
        assert by_connector[b.id].avg_latency_ms == 900.0

    async def test_empty_batch_is_a_no_op(self, metrics_repo: MetricsRepository) -> None:
        assert await metrics_repo.save_host_metrics_snapshots({}) == []


class TestHostWindowQueries:
    async def _seed(
        self,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        session: AsyncSession,
    ) -> tuple[Project, list[Connector], datetime]:
        project, (a, b) = await _project_with_connectors(project_repo, credential_repo, connector_repo, session)
        now = utc_now().replace(microsecond=0)
        # shop: 6 requests across both connectors; news: 2 on a; a stale row outside the window.
        await _insert(session, project.id, a.id, "shop.example.com", now - timedelta(minutes=5), requests=4, failures=1, sent=100, received=400, latency=100)
        await _insert(session, project.id, b.id, "shop.example.com", now - timedelta(minutes=4), requests=2, sent=50, received=50, latency=400)
        await _insert(session, project.id, a.id, "news.example.org", now - timedelta(minutes=3), requests=2, sent=10, received=990, latency=50)
        await _insert(session, project.id, a.id, "old.example.net", now - timedelta(days=2), requests=50, sent=1, received=1)
        await session.commit()
        return project, [a, b], now

    async def test_totals_summary_and_breakdown(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        project, (a, b), now = await self._seed(project_repo, credential_repo, connector_repo, db_session)
        since = now - timedelta(hours=1)

        rows = await metrics_repo.get_host_totals(project.id, since)
        assert [r["host"] for r in rows] == ["shop.example.com", "news.example.org"]
        shop = rows[0]
        assert shop["request_count"] == 6 and shop["failure_count"] == 1
        assert shop["bytes_sent"] == 150 and shop["bytes_received"] == 450
        assert shop["connector_count"] == 2
        # Weighted by requests: (4*100 + 2*400) / 6
        assert shop["avg_latency_ms"] == (400 + 800) / 6

        summary = await metrics_repo.get_host_summary(project.id, since)
        assert summary["host_count"] == 2
        assert summary["request_count"] == 8
        assert summary["bytes_received"] == 1440

        breakdown = await metrics_repo.get_host_connector_breakdown(project.id, since, ["shop.example.com"])
        parts = breakdown["shop.example.com"]
        assert [p["connector_id"] for p in parts] == [a.id, b.id]
        assert parts[1]["avg_latency_ms"] == 400.0
        assert await metrics_repo.get_host_connector_breakdown(project.id, since, []) == {}

    async def test_connector_filter_search_and_limit(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        project, (a, b), now = await self._seed(project_repo, credential_repo, connector_repo, db_session)
        since = now - timedelta(hours=1)

        only_b = await metrics_repo.get_host_totals(project.id, since, connector_id=b.id)
        assert [(r["host"], r["request_count"]) for r in only_b] == [("shop.example.com", 2)]

        # Case-insensitive substring; the SQL wildcards in the needle are literal.
        assert [r["host"] for r in await metrics_repo.get_host_totals(project.id, since, search="NEWS")] == ["news.example.org"]
        assert await metrics_repo.get_host_totals(project.id, since, search="%") == []
        assert await metrics_repo.get_host_totals(project.id, since, search="e_a") == []

        assert len(await metrics_repo.get_host_totals(project.id, since, limit=1)) == 1
        summary = await metrics_repo.get_host_summary(project.id, since, search="example")
        assert summary["host_count"] == 2

    async def test_history_folds_the_rest_into_the_overflow_series(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        project, (a, b), now = await self._seed(project_repo, credential_repo, connector_repo, db_session)
        since = now - timedelta(hours=1)

        rows = await metrics_repo.get_host_history_aggregated(project.id, since, 3600, ["shop.example.com"])
        by_series = {r["host"]: r for r in rows}
        assert set(by_series) == {"shop.example.com", HOST_OVERFLOW}
        assert by_series["shop.example.com"]["request_count"] == 6
        assert by_series[HOST_OVERFLOW]["request_count"] == 2
        assert by_series[HOST_OVERFLOW]["bytes_received"] == 990
        # Chronological, bucketed to the hour.
        timestamps = [r["timestamp"] for r in rows]
        assert timestamps == sorted(timestamps)

        # No chosen hosts: everything is the overflow series.
        rows = await metrics_repo.get_host_history_aggregated(project.id, since, 3600, [])
        assert [r["host"] for r in rows] == [HOST_OVERFLOW]
        assert rows[0]["request_count"] == 8

        # The connector filter applies to the series too.
        rows = await metrics_repo.get_host_history_aggregated(project.id, since, 3600, ["shop.example.com"], connector_id=b.id)
        assert [(r["host"], r["request_count"]) for r in rows] == [("shop.example.com", 2)]


class TestHostCompactionAndRetention:
    async def test_compaction_keeps_per_host_per_connector_sums(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        project, (a, b) = await _project_with_connectors(project_repo, credential_repo, connector_repo, db_session)
        base = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
        for minute in range(30):
            ts = base + timedelta(minutes=minute)
            await _insert(db_session, project.id, a.id, "shop.example.com", ts, requests=2, sent=10, received=20, latency=100)
            await _insert(db_session, project.id, b.id, "shop.example.com", ts, requests=1, sent=5, received=5, latency=300)
            await _insert(db_session, project.id, a.id, "news.example.org", ts, requests=1, failures=1, latency=50)
        # A fresh row that must not be touched.
        fresh = utc_now()
        await _insert(db_session, project.id, a.id, "shop.example.com", fresh, requests=7)
        await db_session.commit()
        assert await _row_count(db_session, project.id) == 91

        compacted = await metrics_repo.compact_host_metrics(
            project_id=project.id, older_than=utc_now() - timedelta(hours=24),
            source_granularity=60, target_granularity=3600,
        )
        await db_session.commit()
        assert compacted == 90
        assert await _row_count(db_session, project.id, granularity=60) == 1
        hourly = (await db_session.execute(
            select(HostMetricsModel).where(HostMetricsModel.project_id == project.id, HostMetricsModel.granularity == 3600)
        )).scalars().all()
        assert len(hourly) == 3
        by_key = {(r.connector_id, r.host): r for r in hourly}
        shop_a = by_key[(a.id, "shop.example.com")]
        assert (shop_a.request_count, shop_a.bytes_sent, shop_a.bytes_received, shop_a.avg_latency_ms) == (60, 300, 600, 100.0)
        shop_b = by_key[(b.id, "shop.example.com")]
        assert (shop_b.request_count, shop_b.avg_latency_ms) == (30, 300.0)
        news = by_key[(a.id, "news.example.org")]
        assert (news.request_count, news.failure_count) == (30, 30)
        assert shop_a.timestamp == base

        # The window sums are unchanged by compaction.
        totals = await metrics_repo.get_host_totals(project.id, utc_now() - timedelta(days=3))
        assert {r["host"]: r["request_count"] for r in totals} == {"shop.example.com": 97, "news.example.org": 30}

    async def test_compaction_with_nothing_old_returns_zero(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        project, _ = await _project_with_connectors(project_repo, credential_repo, connector_repo, db_session)
        assert await metrics_repo.compact_host_metrics(project.id, utc_now(), 60, 3600) == 0

    async def test_retention_deletes_only_the_project_and_only_old_rows(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        project, (a, _b) = await _project_with_connectors(project_repo, credential_repo, connector_repo, db_session)
        other, (c, _d) = await _project_with_connectors(project_repo, credential_repo, connector_repo, db_session, suffix="-2")
        old = utc_now() - timedelta(days=100)
        await _insert(db_session, project.id, a.id, "shop.example.com", old)
        await _insert(db_session, project.id, a.id, "shop.example.com", utc_now())
        await _insert(db_session, other.id, c.id, "shop.example.com", old)
        await db_session.commit()

        deleted = await metrics_repo.delete_host_metrics_older_than(project.id, utc_now() - timedelta(days=90))
        await db_session.commit()
        assert deleted == 1
        assert await _row_count(db_session, project.id) == 1
        assert await _row_count(db_session, other.id) == 1

    async def test_rows_cascade_with_their_connector(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        project, (a, b) = await _project_with_connectors(project_repo, credential_repo, connector_repo, db_session)
        now = utc_now()
        await _insert(db_session, project.id, a.id, "shop.example.com", now)
        await _insert(db_session, project.id, b.id, "shop.example.com", now)
        await db_session.commit()
        await connector_repo.delete(a.id)
        await db_session.commit()
        rows = await metrics_repo.get_host_totals(project.id, now - timedelta(hours=1))
        assert [(r["host"], r["connector_count"]) for r in rows] == [("shop.example.com", 1)]
