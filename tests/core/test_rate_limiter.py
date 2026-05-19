# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the RateLimiter module."""

import time

import pytest

from api.core.rate_limiter import RateLimiter
from api.db.redis import PROXY_QUARANTINE_KEY, PROXY_REQUESTS_KEY, RedisClient


@pytest.fixture
def rate_limiter(redis_client: RedisClient) -> RateLimiter:
    """Create a RateLimiter with the real test Redis client."""
    return RateLimiter(redis_client)


class TestIsQuarantined:
    """Tests for is_quarantined (sync check)."""

    def test_unknown_proxy_not_quarantined(self, rate_limiter: RateLimiter) -> None:
        assert rate_limiter.is_quarantined("unknown-proxy") is False

    def test_quarantined_proxy_returns_true(self, rate_limiter: RateLimiter) -> None:
        rate_limiter._quarantine_expiry["proxy-1"] = time.monotonic() + 100
        assert rate_limiter.is_quarantined("proxy-1") is True

    def test_expired_quarantine_returns_false(self, rate_limiter: RateLimiter) -> None:
        rate_limiter._quarantine_expiry["proxy-1"] = time.monotonic() - 1
        assert rate_limiter.is_quarantined("proxy-1") is False
        assert "proxy-1" not in rate_limiter._quarantine_expiry


class TestGetQuarantineRemaining:
    """Tests for get_quarantine_remaining."""

    def test_unknown_proxy_returns_zero(self, rate_limiter: RateLimiter) -> None:
        assert rate_limiter.get_quarantine_remaining("unknown") == 0.0

    def test_quarantined_proxy_returns_remaining(self, rate_limiter: RateLimiter) -> None:
        rate_limiter._quarantine_expiry["proxy-1"] = time.monotonic() + 50
        remaining = rate_limiter.get_quarantine_remaining("proxy-1")
        assert 49 < remaining <= 50

    def test_expired_quarantine_returns_zero_and_cleans_up(self, rate_limiter: RateLimiter) -> None:
        rate_limiter._quarantine_expiry["proxy-1"] = time.monotonic() - 1
        assert rate_limiter.get_quarantine_remaining("proxy-1") == 0.0
        assert "proxy-1" not in rate_limiter._quarantine_expiry


class TestRecordRequest:
    """Tests for record_request (async, triggers quarantine)."""

    @pytest.mark.asyncio
    async def test_under_limit_no_quarantine(self, rate_limiter: RateLimiter) -> None:
        for _ in range(9):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=10, window_seconds=60,
                quarantine_seconds_min=30, quarantine_seconds_max=60,
            )
        assert rate_limiter.is_quarantined("proxy-1") is False

    @pytest.mark.asyncio
    async def test_at_limit_triggers_quarantine_and_persists_to_redis(
        self, rate_limiter: RateLimiter, redis_client: RedisClient
    ) -> None:
        for _ in range(10):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=10, window_seconds=60,
                quarantine_seconds_min=30, quarantine_seconds_max=60,
            )
        assert rate_limiter.is_quarantined("proxy-1") is True
        key = PROXY_QUARANTINE_KEY.format(proxy_id="proxy-1")
        ttl = await redis_client.client.ttl(key)
        assert ttl > 0

    @pytest.mark.asyncio
    async def test_quarantine_duration_within_range(self, rate_limiter: RateLimiter) -> None:
        for _ in range(5):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=100, quarantine_seconds_max=200,
            )
        remaining = rate_limiter.get_quarantine_remaining("proxy-1")
        assert 99 < remaining <= 200

    @pytest.mark.asyncio
    async def test_fixed_quarantine_when_min_equals_max(self, rate_limiter: RateLimiter) -> None:
        for _ in range(5):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=60, quarantine_seconds_max=60,
            )
        remaining = rate_limiter.get_quarantine_remaining("proxy-1")
        assert 59 < remaining <= 60

    @pytest.mark.asyncio
    async def test_skip_if_already_quarantined(
        self, rate_limiter: RateLimiter, redis_client: RedisClient
    ) -> None:
        for _ in range(5):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=60, quarantine_seconds_max=60,
            )
        assert rate_limiter.is_quarantined("proxy-1") is True

        # Delete the Redis key manually to detect if a second write happens
        key = PROXY_QUARANTINE_KEY.format(proxy_id="proxy-1")
        await redis_client.client.delete(key)

        # Record another request — should be a no-op (skip because already quarantined)
        await rate_limiter.record_request(
            proxy_id="proxy-1", connector_id="conn-1",
            max_requests=5, window_seconds=60,
            quarantine_seconds_min=60, quarantine_seconds_max=60,
        )
        assert await redis_client.client.ttl(key) < 0

    @pytest.mark.asyncio
    async def test_sliding_window_evicts_old_timestamps(
        self, rate_limiter: RateLimiter, redis_client: RedisClient
    ) -> None:
        # Seed the ZSET with timestamps from 120s ago (well outside the 60s window)
        rkey = PROXY_REQUESTS_KEY.format(proxy_id="proxy-1")
        old_ms = int(time.time() * 1000) - 120_000
        old_entries: dict[str, float] = {
            f"old-{i}": old_ms + i for i in range(8)
        }
        await redis_client.client.zadd(rkey, mapping=old_entries)

        # Two new requests should NOT trigger quarantine — the old ones are
        # evicted by the sliding-window check inside record_request.
        for _ in range(2):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=10, window_seconds=60,
                quarantine_seconds_min=30, quarantine_seconds_max=60,
            )
        assert rate_limiter.is_quarantined("proxy-1") is False
        # Only the two fresh entries should remain
        remaining = await redis_client.client.zcard(rkey)
        assert remaining == 2

    @pytest.mark.asyncio
    async def test_zset_cleared_after_quarantine(
        self, rate_limiter: RateLimiter, redis_client: RedisClient
    ) -> None:
        for _ in range(5):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=30, quarantine_seconds_max=60,
            )
        assert rate_limiter.is_quarantined("proxy-1") is True
        rkey = PROXY_REQUESTS_KEY.format(proxy_id="proxy-1")
        assert await redis_client.client.zcard(rkey) == 0

    @pytest.mark.asyncio
    async def test_multiple_proxies_independent(self, rate_limiter: RateLimiter) -> None:
        for _ in range(5):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=30, quarantine_seconds_max=60,
            )
        assert rate_limiter.is_quarantined("proxy-1") is True
        assert rate_limiter.is_quarantined("proxy-2") is False


class TestUnquarantine:
    """Tests for unquarantine (force-remove from quarantine)."""

    @pytest.mark.asyncio
    async def test_unquarantine_quarantined_proxy(
        self, rate_limiter: RateLimiter, redis_client: RedisClient
    ) -> None:
        for _ in range(5):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=300, quarantine_seconds_max=300,
            )
        assert rate_limiter.is_quarantined("proxy-1") is True
        key = PROXY_QUARANTINE_KEY.format(proxy_id="proxy-1")
        assert await redis_client.client.ttl(key) > 0

        result = await rate_limiter.unquarantine("proxy-1")

        assert result is True
        assert rate_limiter.is_quarantined("proxy-1") is False
        assert await redis_client.client.ttl(key) < 0

    @pytest.mark.asyncio
    async def test_unquarantine_non_quarantined_proxy(self, rate_limiter: RateLimiter) -> None:
        result = await rate_limiter.unquarantine("proxy-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_unquarantine_then_can_receive_requests(self, rate_limiter: RateLimiter) -> None:
        for _ in range(5):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=300, quarantine_seconds_max=300,
            )
        assert rate_limiter.is_quarantined("proxy-1") is True

        await rate_limiter.unquarantine("proxy-1")

        await rate_limiter.record_request(
            proxy_id="proxy-1", connector_id="conn-1",
            max_requests=5, window_seconds=60,
            quarantine_seconds_min=300, quarantine_seconds_max=300,
        )
        assert rate_limiter.is_quarantined("proxy-1") is False


class TestRemoveProxy:
    """Tests for remove_proxy cleanup."""

    @pytest.mark.asyncio
    async def test_removes_state_and_redis_keys(
        self, rate_limiter: RateLimiter, redis_client: RedisClient
    ) -> None:
        for _ in range(5):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=300, quarantine_seconds_max=300,
            )
        qkey = PROXY_QUARANTINE_KEY.format(proxy_id="proxy-1")
        assert await redis_client.client.ttl(qkey) > 0

        await rate_limiter.remove_proxy("proxy-1")

        assert "proxy-1" not in rate_limiter._quarantine_expiry
        assert await redis_client.client.ttl(qkey) < 0
        rkey = PROXY_REQUESTS_KEY.format(proxy_id="proxy-1")
        assert await redis_client.client.zcard(rkey) == 0

    @pytest.mark.asyncio
    async def test_remove_nonexistent_proxy_is_safe(self, rate_limiter: RateLimiter) -> None:
        await rate_limiter.remove_proxy("nonexistent")


class TestRemoveProxies:
    """Tests for remove_proxies bulk cleanup."""

    @pytest.mark.asyncio
    async def test_removes_multiple_proxies(
        self, rate_limiter: RateLimiter, redis_client: RedisClient
    ) -> None:
        for pid in ("p1", "p2"):
            for _ in range(5):
                await rate_limiter.record_request(
                    proxy_id=pid, connector_id="conn-1",
                    max_requests=5, window_seconds=60,
                    quarantine_seconds_min=300, quarantine_seconds_max=300,
                )
        assert rate_limiter.is_quarantined("p1") is True
        assert rate_limiter.is_quarantined("p2") is True

        await rate_limiter.remove_proxies(["p1", "p2"])

        assert "p1" not in rate_limiter._quarantine_expiry
        assert "p2" not in rate_limiter._quarantine_expiry
        for pid in ("p1", "p2"):
            assert await redis_client.client.ttl(PROXY_QUARANTINE_KEY.format(proxy_id=pid)) < 0

    @pytest.mark.asyncio
    async def test_empty_list_is_safe(self, rate_limiter: RateLimiter) -> None:
        await rate_limiter.remove_proxies([])


class TestClearConnectorProxies:
    """Tests for clear_connector_proxies (in-memory cache only)."""

    def test_clears_specified_proxies(self, rate_limiter: RateLimiter) -> None:
        rate_limiter._quarantine_expiry["p1"] = time.monotonic() + 100
        rate_limiter._quarantine_expiry["p2"] = time.monotonic() + 100
        rate_limiter._quarantine_expiry["p3"] = time.monotonic() + 100

        rate_limiter.clear_connector_proxies(["p1", "p2"])

        assert "p1" not in rate_limiter._quarantine_expiry
        assert "p2" not in rate_limiter._quarantine_expiry
        assert "p3" in rate_limiter._quarantine_expiry


class TestHydrateFromRedis:
    """Tests for hydrate_from_redis."""

    @pytest.mark.asyncio
    async def test_restores_quarantine_from_redis(
        self, rate_limiter: RateLimiter, redis_client: RedisClient
    ) -> None:
        key = PROXY_QUARANTINE_KEY.format(proxy_id="proxy-1")
        await redis_client.client.setex(key, 45, "1")

        await rate_limiter.hydrate_from_redis(["proxy-1", "proxy-2"])

        assert rate_limiter.is_quarantined("proxy-1") is True
        remaining = rate_limiter.get_quarantine_remaining("proxy-1")
        assert 43 < remaining <= 45
        assert rate_limiter.is_quarantined("proxy-2") is False

    @pytest.mark.asyncio
    async def test_skips_expired_keys(self, rate_limiter: RateLimiter) -> None:
        await rate_limiter.hydrate_from_redis(["proxy-1"])
        assert rate_limiter.is_quarantined("proxy-1") is False

    @pytest.mark.asyncio
    async def test_empty_proxy_list(self, rate_limiter: RateLimiter) -> None:
        await rate_limiter.hydrate_from_redis([])


class TestCrossInstanceSlidingWindow:
    """Two RateLimiter instances sharing Redis see the same sliding window."""

    @pytest.mark.asyncio
    async def test_two_limiters_share_counts(
        self, redis_client: RedisClient
    ) -> None:
        a = RateLimiter(redis_client)
        b = RateLimiter(redis_client)
        for _ in range(3):
            await a.record_request(
                proxy_id="shared", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=30, quarantine_seconds_max=60,
            )
        for _ in range(2):
            await b.record_request(
                proxy_id="shared", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=30, quarantine_seconds_max=60,
            )
        # b's 2nd record hits the combined limit (3+2=5), quarantining via
        # b's local cache. a's local cache doesn't know yet (A2 phase will
        # propagate via pub/sub), but the Redis key is set.
        assert b.is_quarantined("shared") is True
        key = PROXY_QUARANTINE_KEY.format(proxy_id="shared")
        assert await redis_client.client.ttl(key) > 0
