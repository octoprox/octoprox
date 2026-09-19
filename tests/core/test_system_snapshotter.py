# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the install-wide gauge snapshotter and its history queries."""

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.config import Settings
from api.core.system_snapshotter import SystemSnapshotter
from api.db.models import SystemMetricsModel
from api.db.redis import RedisClient
from api.db.repository import SystemMetricsRepository


def _snapshotter(
    db_session_factory: async_sessionmaker[AsyncSession],
    redis_client: RedisClient,
    test_settings: Settings,
    proxy_manager: Any,
    **overrides: Any,
) -> SystemSnapshotter:
    settings = test_settings.model_copy(update=overrides) if overrides else test_settings
    return SystemSnapshotter(db_session_factory, redis_client, proxy_manager, settings)


@pytest.fixture
def stub_manager(mock_proxy_manager: Any) -> Any:
    """A manager with an empty pool - enough for the gauges we store."""
    mock_proxy_manager.proxies = []
    mock_proxy_manager.provider_registry.list.return_value = []
    return mock_proxy_manager


class TestTakeSnapshot:
    async def test_stores_one_row_of_gauges(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        test_settings: Settings,
        stub_manager: Any,
    ) -> None:
        await _snapshotter(db_session_factory, redis_client, test_settings, stub_manager).take_snapshot()

        repo = SystemMetricsRepository(db_session)
        rows = await repo.get_history(utc_now() - timedelta(minutes=5))

        assert len(rows) == 1
        row = rows[0]
        # Postgres and Redis are real containers here, so these are live reads.
        assert row["database_size_bytes"] > 0
        assert row["redis_memory_bytes"] > 0
        assert row["projects"] >= 0

    async def test_records_table_size_breakdown(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        test_settings: Settings,
        stub_manager: Any,
    ) -> None:
        await _snapshotter(db_session_factory, redis_client, test_settings, stub_manager).take_snapshot()

        model = (await db_session.execute(SystemMetricsModel.__table__.select())).one()

        assert "proxies" in model.table_sizes
        assert model.table_sizes["proxies"] > 0

    async def test_skips_when_a_recent_snapshot_exists(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        test_settings: Settings,
        stub_manager: Any,
    ) -> None:
        """Restarts and lease handovers must not insert off-cadence rows."""
        snapshotter = _snapshotter(
            db_session_factory, redis_client, test_settings, stub_manager,
            system_metrics_interval=3600,
        )
        await snapshotter.take_snapshot()
        await snapshotter._snapshot_if_due()

        repo = SystemMetricsRepository(db_session)
        assert len(await repo.get_history(utc_now() - timedelta(hours=2))) == 1

    async def test_takes_one_when_the_last_is_stale(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        test_settings: Settings,
        stub_manager: Any,
    ) -> None:
        snapshotter = _snapshotter(
            db_session_factory, redis_client, test_settings, stub_manager,
            system_metrics_interval=60,
        )
        async with db_session_factory() as session:
            session.add(
                SystemMetricsModel(timestamp=utc_now() - timedelta(hours=3), database_size_bytes=1)
            )
            await session.commit()

        await snapshotter._snapshot_if_due()

        repo = SystemMetricsRepository(db_session)
        assert len(await repo.get_history(utc_now() - timedelta(hours=4))) == 2


class TestRetention:
    async def test_prunes_past_the_window(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        test_settings: Settings,
        stub_manager: Any,
    ) -> None:
        async with db_session_factory() as session:
            session.add(SystemMetricsModel(timestamp=utc_now() - timedelta(days=120)))
            session.add(SystemMetricsModel(timestamp=utc_now() - timedelta(days=10)))
            await session.commit()

        snapshotter = _snapshotter(
            db_session_factory, redis_client, test_settings, stub_manager,
            system_metrics_retention_days=90,
        )
        await snapshotter._apply_retention()

        repo = SystemMetricsRepository(db_session)
        assert len(await repo.get_history(utc_now() - timedelta(days=365))) == 1

    async def test_zero_days_keeps_everything(
        self,
        db_session: AsyncSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        test_settings: Settings,
        stub_manager: Any,
    ) -> None:
        async with db_session_factory() as session:
            session.add(SystemMetricsModel(timestamp=utc_now() - timedelta(days=900)))
            await session.commit()

        snapshotter = _snapshotter(
            db_session_factory, redis_client, test_settings, stub_manager,
            system_metrics_retention_days=0,
        )
        await snapshotter._apply_retention()

        repo = SystemMetricsRepository(db_session)
        assert len(await repo.get_history(utc_now() - timedelta(days=1000))) == 1


class TestBucketedHistory:
    async def test_buckets_average_rather_than_sum(
        self, db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The whole point of a separate pipeline: gauges average, not sum."""
        # Buckets align to absolute epoch boundaries, so anchor the samples
        # inside one known hour rather than relative to "now", which would
        # straddle a boundary depending on when the suite runs.
        base = utc_now().replace(minute=5, second=0, microsecond=0) - timedelta(hours=2)
        async with db_session_factory() as session:
            for offset, size in ((0, 100), (5, 200), (10, 300)):
                session.add(
                    SystemMetricsModel(
                        timestamp=base + timedelta(minutes=offset),
                        database_size_bytes=size,
                        proxies_total=size // 100,
                    )
                )
            await session.commit()

        repo = SystemMetricsRepository(db_session)
        rows = await repo.get_history_aggregated(
            utc_now() - timedelta(hours=4), bucket_seconds=3600
        )

        assert len(rows) == 1
        # Averaged: 200, not the 600 a counter pipeline would have produced.
        assert rows[0]["database_size_bytes"] == 200
        assert rows[0]["proxies_total"] == 2

    async def test_returns_points_oldest_first(
        self, db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        now = utc_now()
        async with db_session_factory() as session:
            for minutes in (30, 10, 20):
                session.add(
                    SystemMetricsModel(
                        timestamp=now - timedelta(minutes=minutes),
                        database_size_bytes=minutes,
                    )
                )
            await session.commit()

        repo = SystemMetricsRepository(db_session)
        rows = await repo.get_history(now - timedelta(hours=1))

        assert [r["database_size_bytes"] for r in rows] == [30, 20, 10]

    async def test_table_growth_uses_window_edges(
        self, db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        now = utc_now()
        async with db_session_factory() as session:
            session.add(
                SystemMetricsModel(
                    timestamp=now - timedelta(hours=2), table_sizes={"proxies": 100}
                )
            )
            session.add(
                SystemMetricsModel(
                    timestamp=now - timedelta(minutes=1), table_sizes={"proxies": 180}
                )
            )
            await session.commit()

        repo = SystemMetricsRepository(db_session)
        first, last = await repo.get_table_sizes_at_edges(now - timedelta(hours=3))

        assert first == {"proxies": 100}
        assert last == {"proxies": 180}
