# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the connector_metrics history: snapshots, period totals, compaction, retention."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.core.stats import MetricDelta, StepMetricDelta
from api.core.timing import Step
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
        window = MetricDelta(
            request_count=4, success_count=3, failure_count=1, latency_sum_ms=168.0, bytes_sent=100, bytes_received=900
        )
        assert await metrics_repo.save_connector_metrics_snapshots({connector.id: window}) == []
        await db_session.commit()
        history = await metrics_repo.get_connector_metrics_history(connector.id)
        assert len(history) == 1
        # Over the three successes.
        assert (history[0]["request_count"], history[0]["avg_latency_ms"]) == (4, 56.0)
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
        project, connector = await _connector(project_repo, credential_repo, connector_repo, db_session)
        old = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
        for i in range(6):
            await _insert(db_session, connector.id, old + timedelta(minutes=i), sent=7, received=11)
        await db_session.commit()

        deleted = await metrics_repo.compact_connector_metrics(
            project.id, older_than=utc_now() - timedelta(hours=24),
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

    async def test_retention(
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


class TestLatencySteps:
    """A connector's latency by step: stored per row, averaged over the successful requests wherever rows are summed."""

    @staticmethod
    def window(successes: int, failures: int, **steps: float) -> StepMetricDelta:
        """A flush window whose every success went through ``steps`` (ms by step name)."""
        delta = StepMetricDelta()
        for _ in range(successes):
            delta.add_request(True, sum(steps.values()), steps={Step(name): ms for name, ms in steps.items()})
        for _ in range(failures):
            delta.add_request(False, 30000.0, steps={Step.ROUTE: 1.0, Step.HANDSHAKE: 29999.0})
        return delta

    async def test_a_window_s_steps_are_stored_and_read_back(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        _project, connector = await _connector(project_repo, credential_repo, connector_repo, db_session)
        window = self.window(3, 1, route=1.0, preflight=600.0, handshake=400.0)
        await metrics_repo.save_connector_metrics_snapshots({connector.id: window})
        await db_session.commit()

        [point] = await metrics_repo.get_connector_metrics_history(connector.id)

        assert point["avg_latency_ms"] == 1001.0
        assert point["step_latency_ms"] == {"route": 1.0, "preflight": 600.0, "connect": 0.0, "handshake": 400.0, "response": 0.0}

    async def test_buckets_weigh_steps_by_successes_and_leave_out_rows_without_steps(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        """A row from before steps were kept still counts in the latency, but not in the steps it does not have."""
        project, connector = await _connector(project_repo, credential_repo, connector_repo, db_session)
        hour = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
        rows = [
            (5, self.window(1, 0, route=2.0, handshake=98.0).history_averages() | {"success_count": 1}),
            (15, self.window(3, 0, route=2.0, handshake=398.0).history_averages() | {"success_count": 3}),
            # Written before steps were kept: its average took the failure in.
            (25, {"avg_latency_ms": 900.0, "success_count": 2}),
            # Nothing succeeded: weighs nothing.
            (35, self.window(0, 4).history_averages() | {"success_count": 0}),
        ]
        for minute, fields in rows:
            db_session.add(ConnectorMetricsModel(
                connector_id=connector.id, timestamp=hour + timedelta(minutes=minute), request_count=4,
                failure_count=4 - fields["success_count"], bytes_sent=0, bytes_received=0, granularity=60, **fields,
            ))
        await db_session.commit()

        [bucket] = await metrics_repo.get_connector_metrics_history_aggregated(
            connector.id, since=hour - timedelta(hours=1), bucket_seconds=3600
        )
        # (100*1 + 400*3 + 900*2) / 6 successes; the steps over the four successes that have them.
        assert bucket["avg_latency_ms"] == pytest.approx(3100 / 6)
        assert bucket["step_latency_ms"] == {"route": 2.0, "preflight": 0.0, "connect": 0.0, "handshake": 323.0, "response": 0.0}

        await metrics_repo.compact_connector_metrics(
            project.id, older_than=utc_now() - timedelta(hours=24), source_granularity=60, target_granularity=3600
        )
        await db_session.commit()
        [hourly] = await metrics_repo.get_connector_metrics_history(connector.id, granularity=3600)
        assert hourly["avg_latency_ms"] == pytest.approx(3100 / 6)
        assert hourly["step_latency_ms"] == bucket["step_latency_ms"]

    async def test_a_point_without_successes_has_no_latency(
        self,
        metrics_repo: MetricsRepository,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        db_session: AsyncSession,
    ) -> None:
        _project, connector = await _connector(project_repo, credential_repo, connector_repo, db_session)
        await metrics_repo.save_connector_metrics_snapshots({connector.id: self.window(0, 2)})
        await db_session.commit()

        [point] = await metrics_repo.get_connector_metrics_history(connector.id)

        assert (point["failure_count"], point["avg_latency_ms"], point["step_latency_ms"]) == (2, None, None)


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
        window = MetricDelta(request_count=1, success_count=1, latency_sum_ms=1.0)
        assert await metrics_repo.save_connector_metrics_snapshots({connector.id: window, "gone": window}) == ["gone"]
        assert await metrics_repo.save_project_metrics_snapshots({"gone": window}) == ["gone"]
        assert await metrics_repo.save_proxy_metrics_snapshots({"gone": window}, {"gone": "healthy"}) == ["gone"]
        await db_session.commit()
        # The transaction survived the misses: the real row is there, nothing else.
        assert len(await metrics_repo.get_connector_metrics_history(connector.id)) == 1
        assert await metrics_repo.get_connector_metrics_history("gone") == []
