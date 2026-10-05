# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the tunnel_peer_metrics history: snapshots, totals, history, compaction, retention, last seen."""

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.core.stats import TunnelPeerMetricDelta
from api.db.models import TunnelPeerMetricsModel
from api.db.repository import MetricsRepository, ProjectRepository
from api.db.wireguard_repository import WireGuardPeerRepository
from api.models.project import Project
from api.models.wireguard import WireGuardPeer


def _unique_address() -> str:
    """Addresses and keys are unique install-wide, and route tests leave peers behind (no db_session there)."""
    n = uuid4().int
    return f"10.99.{(n >> 8) % 254 + 1}.{n % 254 + 1}"


def _unique_key() -> str:
    return uuid4().hex[:44]


async def _peer(
    project_repo: ProjectRepository, session: AsyncSession, suffix: str = ""
) -> tuple[Project, WireGuardPeer]:
    project = Project(name=f"Tunnel Project{suffix}", username=f"tunnel_user{suffix}", password="pass")
    await project_repo.create(project)
    peer = WireGuardPeer(project_id=project.id, name=f"tv{suffix}", public_key=_unique_key(), address=_unique_address())
    await WireGuardPeerRepository(session).create(peer)
    await session.commit()
    return project, peer


async def _insert(
    session: AsyncSession, project_id: str, peer_id: str, ts: datetime, *, sent: int = 10, received: int = 20,
    requests: int = 1, by_address: int = 0, blocked: int = 0, granularity: int = 60,
) -> None:
    session.add(TunnelPeerMetricsModel(
        peer_id=peer_id, protocol="wireguard", project_id=project_id, timestamp=ts,
        request_count=requests, success_count=requests, failure_count=0, avg_latency_ms=100.0,
        bytes_sent=sent, bytes_received=received, by_address=by_address, encrypted_dns_blocked=blocked,
        granularity=granularity,
    ))
    await session.flush()


class TestSnapshots:
    async def test_snapshot_copies_protocol_and_project_from_the_device(
        self, metrics_repo: MetricsRepository, project_repo: ProjectRepository, db_session: AsyncSession
    ) -> None:
        project, peer = await _peer(project_repo, db_session)
        delta = TunnelPeerMetricDelta(request_count=4, success_count=3, failure_count=1, latency_sum_ms=168.0,
                                      bytes_sent=100, bytes_received=900, by_address=2, encrypted_dns_blocked=1)
        assert await metrics_repo.save_tunnel_peer_metrics_snapshot(peer.id, delta) is True
        await db_session.commit()
        history = await metrics_repo.get_tunnel_peer_metrics_history(project.id, peer.id)
        assert len(history) == 1
        assert history[0]["request_count"] == 4 and history[0]["avg_latency_ms"] == 42.0
        assert history[0]["connections_by_address"] == 2 and history[0]["encrypted_dns_blocked"] == 1
        row = (await db_session.execute(
            TunnelPeerMetricsModel.__table__.select().where(TunnelPeerMetricsModel.peer_id == peer.id)
        )).one()
        assert row.protocol == "wireguard" and row.project_id == project.id
        # Scoped by project: another project cannot read it.
        assert await metrics_repo.get_tunnel_peer_metrics_history("other", peer.id) == []

    async def test_unknown_device_is_dropped_without_raising(
        self, metrics_repo: MetricsRepository, project_repo: ProjectRepository, db_session: AsyncSession
    ) -> None:
        project, peer = await _peer(project_repo, db_session)
        assert await metrics_repo.save_tunnel_peer_metrics_snapshot("gone", TunnelPeerMetricDelta(request_count=1)) is False
        assert await metrics_repo.save_tunnel_peer_metrics_snapshot(peer.id, TunnelPeerMetricDelta(request_count=1)) is True
        await db_session.commit()
        assert len(await metrics_repo.get_tunnel_peer_metrics_history(project.id, peer.id)) == 1

    async def test_cumulative_totals_per_device(
        self, metrics_repo: MetricsRepository, project_repo: ProjectRepository, db_session: AsyncSession
    ) -> None:
        project, a = await _peer(project_repo, db_session, "A")
        _project, b = await _peer(project_repo, db_session, "B")
        now = utc_now()
        await _insert(db_session, project.id, a.id, now - timedelta(days=3), sent=1, received=1, by_address=1)
        await _insert(db_session, project.id, a.id, now - timedelta(hours=1), sent=10, received=20, blocked=3)
        await _insert(db_session, b.project_id, b.id, now - timedelta(hours=1), sent=100, received=200, requests=5)
        await db_session.commit()
        totals = await metrics_repo.get_cumulative_tunnel_peer_metrics()
        assert totals[a.id] == TunnelPeerMetricDelta(
            request_count=2, success_count=2, latency_sum_ms=200.0, bytes_sent=11, bytes_received=21,
            by_address=1, encrypted_dns_blocked=3,
        )
        assert totals[b.id].request_count == 5 and totals[b.id].bytes_received == 200


class TestHistoryAndCompaction:
    async def test_aggregated_history_buckets(
        self, metrics_repo: MetricsRepository, project_repo: ProjectRepository, db_session: AsyncSession
    ) -> None:
        project, peer = await _peer(project_repo, db_session)
        base = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
        for minute in (5, 25, 45):
            await _insert(db_session, project.id, peer.id, base + timedelta(minutes=minute), sent=100, received=100, by_address=1)
        await db_session.commit()
        rows = await metrics_repo.get_tunnel_peer_metrics_history_aggregated(
            project.id, peer.id, since=base - timedelta(hours=1), bucket_seconds=3600
        )
        assert len(rows) == 1
        assert rows[0]["request_count"] == 3 and rows[0]["bytes_sent"] == 300
        assert rows[0]["connections_by_address"] == 3 and rows[0]["avg_latency_ms"] == 100.0

    async def test_compaction_covers_every_device_of_the_project(
        self, metrics_repo: MetricsRepository, project_repo: ProjectRepository, db_session: AsyncSession
    ) -> None:
        project, a = await _peer(project_repo, db_session, "A")
        b = WireGuardPeer(project_id=project.id, name="tvB", public_key=_unique_key(), address=_unique_address())
        await WireGuardPeerRepository(db_session).create(b)
        old = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
        for i in range(6):
            await _insert(db_session, project.id, a.id, old + timedelta(minutes=i), sent=7, received=11, by_address=1)
            await _insert(db_session, project.id, b.id, old + timedelta(minutes=i), sent=1, received=1, blocked=2)
        await db_session.commit()

        deleted = await metrics_repo.compact_tunnel_peer_metrics(
            project.id, older_than=utc_now() - timedelta(hours=24), source_granularity=60, target_granularity=3600
        )
        await db_session.commit()
        assert deleted == 12
        assert await metrics_repo.get_tunnel_peer_metrics_history(project.id, a.id, granularity=60) == []
        hourly_a = await metrics_repo.get_tunnel_peer_metrics_history(project.id, a.id, granularity=3600)
        hourly_b = await metrics_repo.get_tunnel_peer_metrics_history(project.id, b.id, granularity=3600)
        assert len(hourly_a) == 1 and len(hourly_b) == 1
        assert hourly_a[0]["bytes_sent"] == 42 and hourly_a[0]["connections_by_address"] == 6
        assert hourly_b[0]["bytes_received"] == 6 and hourly_b[0]["encrypted_dns_blocked"] == 12
        assert hourly_b[0]["request_count"] == 6
        totals = await metrics_repo.get_cumulative_tunnel_peer_metrics()
        assert totals[a.id].bytes_received == 66 and totals[a.id].by_address == 6

    async def test_retention(
        self, metrics_repo: MetricsRepository, project_repo: ProjectRepository, db_session: AsyncSession
    ) -> None:
        project, peer = await _peer(project_repo, db_session)
        await _insert(db_session, project.id, peer.id, utc_now() - timedelta(days=100))
        await _insert(db_session, project.id, peer.id, utc_now() - timedelta(hours=1))
        await db_session.commit()
        deleted = await metrics_repo.delete_tunnel_peer_metrics_older_than(project.id, utc_now() - timedelta(days=90))
        await db_session.commit()
        assert deleted == 1
        assert len(await metrics_repo.get_tunnel_peer_metrics_history(project.id, peer.id)) == 1


class TestRowsFollowTheDevice:
    async def test_rows_go_with_the_device(
        self, metrics_repo: MetricsRepository, project_repo: ProjectRepository, db_session: AsyncSession
    ) -> None:
        project, peer = await _peer(project_repo, db_session)
        await _insert(db_session, project.id, peer.id, utc_now())
        await db_session.commit()
        assert await WireGuardPeerRepository(db_session).delete(peer.id)
        await db_session.commit()
        assert await metrics_repo.get_tunnel_peer_metrics_history(project.id, peer.id) == []
        assert peer.id not in await metrics_repo.get_cumulative_tunnel_peer_metrics()

    async def test_rows_go_with_the_project(
        self, metrics_repo: MetricsRepository, project_repo: ProjectRepository, db_session: AsyncSession
    ) -> None:
        project, peer = await _peer(project_repo, db_session)
        await _insert(db_session, project.id, peer.id, utc_now())
        await db_session.commit()
        assert await project_repo.delete(project.id)
        await db_session.commit()
        assert peer.id not in await metrics_repo.get_cumulative_tunnel_peer_metrics()


class TestLastSeen:
    async def test_last_seen_only_moves_forward(
        self, project_repo: ProjectRepository, db_session: AsyncSession
    ) -> None:
        project, peer = await _peer(project_repo, db_session)
        repo = WireGuardPeerRepository(db_session)
        other = WireGuardPeer(project_id=project.id, name="other", public_key=_unique_key(), address=_unique_address())
        await repo.create(other)
        first = utc_now().replace(microsecond=0) - timedelta(minutes=5)
        assert await repo.record_last_seen([]) == 0
        # One statement for the batch: both devices move, the unknown one is ignored.
        assert await repo.record_last_seen([(peer.id, first, "1.2.3.4:5"), (other.id, first, None), ("gone", first, None)]) == 2
        # Older sightings do not regress a row; newer ones advance it.
        assert await repo.record_last_seen([(peer.id, first - timedelta(minutes=1), "old")]) == 0
        assert await repo.record_last_seen([(peer.id, first + timedelta(minutes=1), "5.6.7.8:9")]) == 1
        await db_session.commit()
        loaded = await repo.get_by_id(peer.id)
        assert loaded is not None
        assert loaded.last_handshake_at == first + timedelta(minutes=1) and loaded.last_endpoint == "5.6.7.8:9"
        loaded_other = await repo.get_by_id(other.id)
        assert loaded_other is not None
        assert loaded_other.last_handshake_at == first and loaded_other.last_endpoint is None
        # A sighting is not an edit: updated_at is left where creation put it.
        assert loaded.updated_at == peer.updated_at
        # Operational data: an edit does not touch it, and the version is not bumped by a sighting.
        await repo.update(loaded.model_copy(update={"name": "renamed"}))
        await db_session.commit()
        again = await repo.get_by_id(peer.id)
        assert again is not None and again.name == "renamed" and again.last_endpoint == "5.6.7.8:9"
