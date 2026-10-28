# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""metric_totals: windows add up, a rebuild matches the history, and totals of entities that are gone go."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.stats import MetricDelta, StepMetricDelta, TunnelPeerMetricDelta
from api.db.models import ProjectMetricsModel, ProxyMetricsModel, TunnelPeerMetricsModel
from api.db.redis import (
    METRIC_WINDOW_KINDS,
    PROJECT_WINDOWS,
    PROXY_WINDOWS,
    TUNNEL_PEER_WINDOWS,
    TotalledWindows,
)
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    MetricsRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.db.wireguard_repository import WireGuardPeerRepository
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy
from api.models.wireguard import WireGuardPeer

T1 = datetime(2026, 10, 1, 12, 0)
T2 = datetime(2026, 10, 1, 12, 1)


async def _proxy(session: AsyncSession) -> tuple[Project, Proxy]:
    """A committed proxy of a static connector, and its project."""
    project = Project(name="Totals", username="totals", password="pw")
    credential = Credential(name="Static", type=CredentialType.STATIC_PROXY_PROVIDER, project_id=project.id, config={})
    connector = Connector(
        name="Static", credential_id=credential.id, credential_type=CredentialType.STATIC_PROXY_PROVIDER,
        project_id=project.id, config={},
    )
    proxy = Proxy(host="10.0.0.1", port=8080, connector_id=connector.id)
    await ProjectRepository(session).create(project)
    await CredentialRepository(session).create(credential)
    await ConnectorRepository(session).create(connector)
    await ProxyRepository(session).create(proxy)
    await session.commit()
    return project, proxy


def _device(project_id: str) -> WireGuardPeer:
    n = uuid4().int
    return WireGuardPeer(
        project_id=project_id, name="tv", public_key=uuid4().hex, address=f"10.98.{(n >> 8) % 254 + 1}.{n % 254 + 1}"
    )


class TestKinds:
    def test_only_proxies_projects_and_tunnel_devices_keep_totals(self) -> None:
        """Their window kinds alone are totalled, so the type checker refuses a connector's for the totals."""
        totalled = [kind for kind in METRIC_WINDOW_KINDS if isinstance(kind, TotalledWindows)]

        assert totalled == [PROXY_WINDOWS, PROJECT_WINDOWS, TUNNEL_PEER_WINDOWS]


class TestUpsert:
    async def test_windows_add_to_the_totals_and_missing_rows_are_created(
        self, metrics_repo: MetricsRepository, db_session: AsyncSession
    ) -> None:
        first = MetricDelta(request_count=3, success_count=2, failure_count=1, latency_sum_ms=300.0, bytes_sent=10, bytes_received=100)
        second = MetricDelta(request_count=1, success_count=1, latency_sum_ms=50.0, bytes_sent=5, bytes_received=50)
        await metrics_repo.upsert_totals(PROXY_WINDOWS, {"p1": first})
        assert await metrics_repo.get_totals(PROXY_WINDOWS, ["p1"]) == {"p1": first}
        await metrics_repo.upsert_totals(PROXY_WINDOWS, {"p1": second, "p2": second})
        await db_session.commit()
        totals = await metrics_repo.get_totals(PROXY_WINDOWS, ["p1", "p2", "p3"])
        assert totals == {
            "p1": MetricDelta(request_count=4, success_count=3, failure_count=1, latency_sum_ms=350.0, bytes_sent=15, bytes_received=150),
            "p2": second,
        }

    async def test_a_tunnel_device_keeps_its_two_extra_counters(
        self, metrics_repo: MetricsRepository, db_session: AsyncSession
    ) -> None:
        window = TunnelPeerMetricDelta(request_count=2, success_count=2, latency_sum_ms=40.0, by_address=3, encrypted_dns_blocked=1)
        await metrics_repo.upsert_totals(TUNNEL_PEER_WINDOWS, {"d1": window})
        await metrics_repo.upsert_totals(TUNNEL_PEER_WINDOWS, {"d1": window})
        await db_session.commit()
        totals = await metrics_repo.get_totals(TUNNEL_PEER_WINDOWS, ["d1"])
        assert totals == {
            "d1": TunnelPeerMetricDelta(request_count=4, success_count=4, latency_sum_ms=80.0, by_address=6, encrypted_dns_blocked=2),
        }

    async def test_kinds_are_kept_apart(self, metrics_repo: MetricsRepository, db_session: AsyncSession) -> None:
        await metrics_repo.upsert_totals(PROXY_WINDOWS, {"same": MetricDelta(request_count=1)})
        await metrics_repo.upsert_totals(PROJECT_WINDOWS, {"same": MetricDelta(request_count=7)})
        await db_session.commit()
        assert (await metrics_repo.get_totals(PROXY_WINDOWS, ["same"]))["same"].request_count == 1
        assert (await metrics_repo.get_totals(PROJECT_WINDOWS, ["same"]))["same"].request_count == 7
        assert await metrics_repo.get_totals(TUNNEL_PEER_WINDOWS, ["same"]) == {}

    async def test_a_flush_larger_than_one_statement_is_written_and_read_whole(
        self, metrics_repo: MetricsRepository, db_session: AsyncSession
    ) -> None:
        deltas = {f"p{n:05}": MetricDelta(request_count=n + 1) for n in range(4500)}
        await metrics_repo.upsert_totals(PROXY_WINDOWS, deltas)
        await db_session.commit()
        totals = await metrics_repo.get_totals(PROXY_WINDOWS, deltas)
        assert len(totals) == 4500 and totals["p04499"].request_count == 4500


class TestRebuild:
    async def test_the_totals_become_the_sums_of_the_history(
        self, metrics_repo: MetricsRepository, db_session: AsyncSession
    ) -> None:
        project, proxy = await _proxy(db_session)
        device = _device(project.id)
        await WireGuardPeerRepository(db_session).create(device)
        db_session.add_all([
            ProxyMetricsModel(proxy_id=proxy.id, timestamp=T1, request_count=3, success_count=2, failure_count=1,
                              avg_latency_ms=100.0, bytes_sent=10, bytes_received=100, status="healthy", granularity=60),
            ProxyMetricsModel(proxy_id=proxy.id, timestamp=T2, request_count=1, success_count=1, failure_count=0,
                              avg_latency_ms=300.0, bytes_sent=5, bytes_received=50, status="healthy", granularity=3600),
            ProjectMetricsModel(project_id=project.id, timestamp=T1, request_count=4, success_count=3, failure_count=1,
                                avg_latency_ms=150.0, bytes_sent=15, bytes_received=150, granularity=60),
            TunnelPeerMetricsModel(peer_id=device.id, protocol="wireguard", project_id=project.id, timestamp=T2,
                                   request_count=2, success_count=1, failure_count=1, avg_latency_ms=20.0, bytes_sent=7,
                                   bytes_received=70, by_address=3, encrypted_dns_blocked=1, granularity=60),
        ])
        # Totals the history does not back: drifted, and of an entity without history.
        await metrics_repo.upsert_totals(PROXY_WINDOWS, {proxy.id: MetricDelta(request_count=999), "no-history": MetricDelta(request_count=1)})
        await db_session.commit()

        assert await metrics_repo.rebuild_totals() == 3
        await db_session.commit()
        # Each row's average back as a sum over its successful requests.
        assert await metrics_repo.get_totals(PROXY_WINDOWS, [proxy.id, "no-history"]) == {
            proxy.id: MetricDelta(request_count=4, success_count=3, failure_count=1, latency_sum_ms=500.0, bytes_sent=15, bytes_received=150),
        }
        assert await metrics_repo.get_totals(PROJECT_WINDOWS, [project.id]) == {
            project.id: StepMetricDelta(request_count=4, success_count=3, failure_count=1, latency_sum_ms=450.0, bytes_sent=15, bytes_received=150),
        }
        assert await metrics_repo.get_totals(TUNNEL_PEER_WINDOWS, [device.id]) == {
            device.id: TunnelPeerMetricDelta(request_count=2, success_count=1, failure_count=1, latency_sum_ms=20.0,
                                             bytes_sent=7, bytes_received=70, by_address=3, encrypted_dns_blocked=1),
        }


    async def test_a_window_with_no_counters_adds_nothing_rather_than_failing(
        self, metrics_repo: MetricsRepository, db_session: AsyncSession
    ) -> None:
        """The history's counters are nullable; a legacy or hand-edited row must not stop the rebuild."""
        _project, proxy = await _proxy(db_session)
        await db_session.execute(
            text(
                "INSERT INTO proxy_metrics (proxy_id, timestamp, request_count, success_count, failure_count, "
                "avg_latency_ms, bytes_sent, bytes_received, status, granularity) "
                "VALUES (:proxy_id, :t1, NULL, NULL, NULL, NULL, 4, 40, 'healthy', 60)"
            ),
            {"proxy_id": proxy.id, "t1": T1},
        )
        await db_session.commit()

        assert await metrics_repo.rebuild_totals() == 1
        await db_session.commit()
        assert await metrics_repo.get_totals(PROXY_WINDOWS, [proxy.id]) == {
            proxy.id: MetricDelta(bytes_sent=4, bytes_received=40)
        }


class TestRetention:
    async def test_totals_of_entities_that_are_gone_are_deleted(
        self, metrics_repo: MetricsRepository, db_session: AsyncSession
    ) -> None:
        project, proxy = await _proxy(db_session)
        device = _device(project.id)
        await WireGuardPeerRepository(db_session).create(device)
        window = MetricDelta(request_count=1)
        await metrics_repo.upsert_totals(PROXY_WINDOWS, {proxy.id: window, "gone-proxy": window})
        await metrics_repo.upsert_totals(PROJECT_WINDOWS, {project.id: window, "gone-project": window})
        await metrics_repo.upsert_totals(TUNNEL_PEER_WINDOWS, {device.id: TunnelPeerMetricDelta(request_count=1), "gone-device": window})
        await db_session.commit()

        assert await metrics_repo.delete_orphan_totals() == 3
        await db_session.commit()
        assert set(await metrics_repo.get_totals(PROXY_WINDOWS, [proxy.id, "gone-proxy"])) == {proxy.id}
        assert set(await metrics_repo.get_totals(PROJECT_WINDOWS, [project.id, "gone-project"])) == {project.id}
        assert set(await metrics_repo.get_totals(TUNNEL_PEER_WINDOWS, [device.id, "gone-device"])) == {device.id}
