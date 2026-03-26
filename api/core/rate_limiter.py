# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""In-memory per-proxy rate limiter with quarantine support.

Tracks request timestamps per proxy using deques (sliding window).
When a proxy exceeds its rate limit, it enters quarantine for a
randomized duration within the configured [min, max] range.

Quarantine state is persisted to Redis with TTL for restart recovery.
"""

import random
import time
from collections import deque

import structlog

from api.db.redis import RedisClient

logger = structlog.get_logger()

# Redis key for quarantine persistence
QUARANTINE_KEY = "proxy:quarantine:{proxy_id}"


class RateLimiter:
    """In-memory per-proxy rate limiter with quarantine support.

    Args:
        redis_client: Redis client for persisting quarantine state.
    """

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client
        # proxy_id -> deque of monotonic timestamps
        self._request_timestamps: dict[str, deque[float]] = {}
        # proxy_id -> monotonic timestamp when quarantine expires
        self._quarantine_expiry: dict[str, float] = {}

    def is_quarantined(self, proxy_id: str) -> bool:
        """Check if a proxy is currently quarantined (sync, no I/O)."""
        expiry = self._quarantine_expiry.get(proxy_id)
        if expiry is None:
            return False
        if time.monotonic() >= expiry:
            del self._quarantine_expiry[proxy_id]
            self._request_timestamps.pop(proxy_id, None)
            return False
        return True

    def get_quarantine_remaining(self, proxy_id: str) -> float:
        """Get remaining quarantine time in seconds, or 0 if not quarantined."""
        expiry = self._quarantine_expiry.get(proxy_id)
        if expiry is None:
            return 0.0
        remaining = expiry - time.monotonic()
        if remaining <= 0:
            del self._quarantine_expiry[proxy_id]
            self._request_timestamps.pop(proxy_id, None)
            return 0.0
        return remaining

    async def record_request(
        self,
        proxy_id: str,
        connector_id: str,
        max_requests: int,
        window_seconds: int,
        quarantine_seconds_min: int,
        quarantine_seconds_max: int,
    ) -> None:
        """Record a request and quarantine the proxy if limit is exceeded."""
        now = time.monotonic()

        # Skip if already quarantined
        if proxy_id in self._quarantine_expiry and now < self._quarantine_expiry[proxy_id]:
            return

        # Get or create the deque for this proxy
        if proxy_id not in self._request_timestamps:
            self._request_timestamps[proxy_id] = deque()

        timestamps = self._request_timestamps[proxy_id]

        # Add current request
        timestamps.append(now)

        # Evict timestamps outside the window
        cutoff = now - window_seconds
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

        # Check if limit exceeded
        if len(timestamps) >= max_requests:
            duration = random.uniform(quarantine_seconds_min, quarantine_seconds_max)
            self._quarantine_expiry[proxy_id] = now + duration
            timestamps.clear()

            logger.info(
                "Proxy quarantined due to rate limit",
                proxy_id=proxy_id,
                connector_id=connector_id,
                max_requests=max_requests,
                window_seconds=window_seconds,
                quarantine_seconds=round(duration, 1),
            )

            # Persist to Redis with TTL for restart recovery
            ttl = int(duration) + 1  # Round up to ensure coverage
            key = QUARANTINE_KEY.format(proxy_id=proxy_id)
            await self._redis_client.client.setex(key, ttl, "1")

    async def hydrate_from_redis(self, proxy_ids: list[str]) -> None:
        """Restore quarantine state from Redis after restart."""
        now = time.monotonic()
        restored = 0
        for proxy_id in proxy_ids:
            key = QUARANTINE_KEY.format(proxy_id=proxy_id)
            ttl = await self._redis_client.client.ttl(key)
            if ttl is not None and ttl > 0:
                self._quarantine_expiry[proxy_id] = now + ttl
                restored += 1

        if restored:
            logger.info("Restored quarantine state from Redis", count=restored)

    async def unquarantine(self, proxy_id: str) -> bool:
        """Forcefully remove a proxy from quarantine.

        Returns True if the proxy was quarantined and is now released,
        False if it was not quarantined.
        """
        if proxy_id not in self._quarantine_expiry:
            return False

        del self._quarantine_expiry[proxy_id]
        self._request_timestamps.pop(proxy_id, None)

        # Remove from Redis
        key = QUARANTINE_KEY.format(proxy_id=proxy_id)
        await self._redis_client.client.delete(key)

        logger.info("Proxy manually unquarantined", proxy_id=proxy_id)
        return True

    async def remove_proxy(self, proxy_id: str) -> None:
        """Clean up all rate limiter state (in-memory + Redis) for a removed proxy."""
        await self.remove_proxies([proxy_id])

    async def remove_proxies(self, proxy_ids: list[str]) -> None:
        """Clean up all rate limiter state for multiple proxies at once."""
        for proxy_id in proxy_ids:
            self._request_timestamps.pop(proxy_id, None)
            self._quarantine_expiry.pop(proxy_id, None)
        if proxy_ids:
            keys = [QUARANTINE_KEY.format(proxy_id=pid) for pid in proxy_ids]
            await self._redis_client.client.delete(*keys)

    def clear_connector_proxies(self, proxy_ids: list[str]) -> None:
        """Clear in-memory rate limiter state for a set of proxy IDs.

        Used when a connector's rate limit config changes. Does not touch
        Redis since the quarantine keys have TTLs and the proxies still exist.
        """
        for proxy_id in proxy_ids:
            self._request_timestamps.pop(proxy_id, None)
            self._quarantine_expiry.pop(proxy_id, None)
