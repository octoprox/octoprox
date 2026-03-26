# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the RateLimiter module."""

import time
from collections import deque

import pytest

from api.core.rate_limiter import QUARANTINE_KEY, RateLimiter
from api.db.redis import RedisClient


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
        # Verify the key was persisted in Redis with a TTL
        key = QUARANTINE_KEY.format(proxy_id="proxy-1")
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
        # Quarantine it
        for _ in range(5):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=60, quarantine_seconds_max=60,
            )
        assert rate_limiter.is_quarantined("proxy-1") is True

        # Delete the Redis key manually to detect if a second write happens
        key = QUARANTINE_KEY.format(proxy_id="proxy-1")
        await redis_client.client.delete(key)

        # Record another request — should be a no-op (skip because already quarantined)
        await rate_limiter.record_request(
            proxy_id="proxy-1", connector_id="conn-1",
            max_requests=5, window_seconds=60,
            quarantine_seconds_min=60, quarantine_seconds_max=60,
        )
        # Key should still be absent because the skip didn't re-persist
        assert await redis_client.client.ttl(key) < 0

    @pytest.mark.asyncio
    async def test_sliding_window_evicts_old_timestamps(self, rate_limiter: RateLimiter) -> None:
        old_time = time.monotonic() - 120
        rate_limiter._request_timestamps["proxy-1"] = deque([old_time] * 8)

        for _ in range(2):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=10, window_seconds=60,
                quarantine_seconds_min=30, quarantine_seconds_max=60,
            )
        assert rate_limiter.is_quarantined("proxy-1") is False
        assert len(rate_limiter._request_timestamps["proxy-1"]) == 2

    @pytest.mark.asyncio
    async def test_timestamps_cleared_after_quarantine(self, rate_limiter: RateLimiter) -> None:
        for _ in range(5):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=30, quarantine_seconds_max=60,
            )
        assert rate_limiter.is_quarantined("proxy-1") is True
        assert len(rate_limiter._request_timestamps.get("proxy-1", [])) == 0

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
        # Quarantine via record_request so Redis key exists
        for _ in range(5):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=300, quarantine_seconds_max=300,
            )
        assert rate_limiter.is_quarantined("proxy-1") is True
        key = QUARANTINE_KEY.format(proxy_id="proxy-1")
        assert await redis_client.client.ttl(key) > 0

        result = await rate_limiter.unquarantine("proxy-1")

        assert result is True
        assert rate_limiter.is_quarantined("proxy-1") is False
        assert await redis_client.client.ttl(key) < 0  # Key deleted

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

        # Should accept requests again without immediate re-quarantine
        await rate_limiter.record_request(
            proxy_id="proxy-1", connector_id="conn-1",
            max_requests=5, window_seconds=60,
            quarantine_seconds_min=300, quarantine_seconds_max=300,
        )
        assert rate_limiter.is_quarantined("proxy-1") is False


class TestRemoveProxy:
    """Tests for remove_proxy cleanup."""

    @pytest.mark.asyncio
    async def test_removes_state_and_redis_key(
        self, rate_limiter: RateLimiter, redis_client: RedisClient
    ) -> None:
        # Quarantine so Redis key exists
        for _ in range(5):
            await rate_limiter.record_request(
                proxy_id="proxy-1", connector_id="conn-1",
                max_requests=5, window_seconds=60,
                quarantine_seconds_min=300, quarantine_seconds_max=300,
            )
        key = QUARANTINE_KEY.format(proxy_id="proxy-1")
        assert await redis_client.client.ttl(key) > 0

        await rate_limiter.remove_proxy("proxy-1")

        assert "proxy-1" not in rate_limiter._request_timestamps
        assert "proxy-1" not in rate_limiter._quarantine_expiry
        assert await redis_client.client.ttl(key) < 0

    @pytest.mark.asyncio
    async def test_remove_nonexistent_proxy_is_safe(self, rate_limiter: RateLimiter) -> None:
        await rate_limiter.remove_proxy("nonexistent")


class TestRemoveProxies:
    """Tests for remove_proxies bulk cleanup."""

    @pytest.mark.asyncio
    async def test_removes_multiple_proxies(
        self, rate_limiter: RateLimiter, redis_client: RedisClient
    ) -> None:
        # Quarantine two proxies
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
            key = QUARANTINE_KEY.format(proxy_id=pid)
            assert await redis_client.client.ttl(key) < 0

    @pytest.mark.asyncio
    async def test_empty_list_is_safe(self, rate_limiter: RateLimiter) -> None:
        await rate_limiter.remove_proxies([])


class TestClearConnectorProxies:
    """Tests for clear_connector_proxies (in-memory only)."""

    def test_clears_specified_proxies(self, rate_limiter: RateLimiter) -> None:
        rate_limiter._request_timestamps["p1"] = deque([time.monotonic()])
        rate_limiter._request_timestamps["p2"] = deque([time.monotonic()])
        rate_limiter._request_timestamps["p3"] = deque([time.monotonic()])
        rate_limiter._quarantine_expiry["p1"] = time.monotonic() + 100
        rate_limiter._quarantine_expiry["p2"] = time.monotonic() + 100

        rate_limiter.clear_connector_proxies(["p1", "p2"])

        assert "p1" not in rate_limiter._request_timestamps
        assert "p2" not in rate_limiter._request_timestamps
        assert "p3" in rate_limiter._request_timestamps
        assert "p1" not in rate_limiter._quarantine_expiry
        assert "p2" not in rate_limiter._quarantine_expiry


class TestHydrateFromRedis:
    """Tests for hydrate_from_redis."""

    @pytest.mark.asyncio
    async def test_restores_quarantine_from_redis(
        self, rate_limiter: RateLimiter, redis_client: RedisClient
    ) -> None:
        # Simulate a quarantine key left in Redis (as if from a previous run)
        key = QUARANTINE_KEY.format(proxy_id="proxy-1")
        await redis_client.client.setex(key, 45, "1")

        await rate_limiter.hydrate_from_redis(["proxy-1", "proxy-2"])

        assert rate_limiter.is_quarantined("proxy-1") is True
        remaining = rate_limiter.get_quarantine_remaining("proxy-1")
        assert 43 < remaining <= 45
        assert rate_limiter.is_quarantined("proxy-2") is False

    @pytest.mark.asyncio
    async def test_skips_expired_keys(self, rate_limiter: RateLimiter) -> None:
        # No key in Redis — should not restore
        await rate_limiter.hydrate_from_redis(["proxy-1"])
        assert rate_limiter.is_quarantined("proxy-1") is False

    @pytest.mark.asyncio
    async def test_empty_proxy_list(self, rate_limiter: RateLimiter) -> None:
        await rate_limiter.hydrate_from_redis([])
