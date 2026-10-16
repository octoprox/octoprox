# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the metrics compactor's upkeep of the Redis metrics indexes."""

import asyncio
import contextlib
from unittest.mock import MagicMock

import pytest

from api.core import metrics_compactor
from api.core.metrics_compactor import MetricsCompactor
from api.db.redis import PROXY_METRICS_KEY, RedisClient


class AcquiredLease:
    """Free to take: the compactor leads from its first try."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.is_held = False

    async def try_acquire(self) -> bool:
        self.is_held = True
        return True

    async def release(self) -> None:
        pass


class TestIndexUpkeep:
    async def test_a_new_leader_lists_the_hashes_no_index_has_at_once(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After an upgrade, hashes written by older instances are in no index: found on taking the lease."""
        await redis_client.client.hset(PROXY_METRICS_KEY.format(proxy_id="legacy"), mapping={"request_count": 1})
        monkeypatch.setattr(metrics_compactor, "Lease", AcquiredLease)
        # An hour between compactions: only the sweep on taking the lease can list it in time.
        compactor = MetricsCompactor(MagicMock(), redis_client, "inst", interval=3600)

        task = asyncio.create_task(compactor.run())
        try:
            async with asyncio.timeout(5):
                while "legacy" not in await redis_client.get_all_proxy_metrics():
                    await asyncio.sleep(0.01)
        finally:
            compactor.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_the_walk_runs_once_per_lease_not_with_every_compaction(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A whole-keyspace walk finds nothing once done; only a failed one is tried again."""
        walks: list[int] = []

        async def reindex() -> int:
            walks.append(1)
            if len(walks) == 1:
                raise ConnectionError("Redis went away mid-walk")
            return 0

        monkeypatch.setattr(redis_client, "reindex_metrics", reindex)
        monkeypatch.setattr(metrics_compactor, "Lease", AcquiredLease)
        compactor = MetricsCompactor(MagicMock(), redis_client, "inst", interval=0)
        compactions: list[int] = []

        async def compact() -> None:
            compactions.append(1)
            if len(compactions) == 3:
                compactor.stop()

        monkeypatch.setattr(compactor, "_compact_and_retain", compact)
        await asyncio.wait_for(compactor.run(), 5)

        # Failed on taking the lease, tried again after the first compaction, then never again.
        assert (len(compactions), len(walks)) == (3, 2)
