# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Per-proxy rate limiter using a Redis sorted-set sliding window.

Sliding-window request counts live in Redis so that multiple Octoprox
instances counting against the same upstream proxy agree on the rate.
Quarantine state is a TTL'd Redis key (authoritative cross-instance),
mirrored to an in-memory cache so the sync `is_quarantined()` check on
the hot request path does not pay a Redis round-trip.

When a proxy is quarantined (or released), this module publishes a
``proxy_quarantine_changed`` event so peer instances re-hydrate their
local quarantine cache from Redis on the next subscriber tick.
"""

import random
import time
import uuid

import structlog

from api.core.event_bus import event_bus
from api.core.signals import proxy_quarantine_changed
from api.db.redis import PROXY_QUARANTINE_KEY, PROXY_REQUESTS_KEY, RedisClient

logger = structlog.get_logger()

# Lua: atomically evict expired entries, count current, and add new
# request iff under the limit.
#   KEYS[1] = requests zset
#   ARGV[1] = now_ms
#   ARGV[2] = window_ms
#   ARGV[3] = max_requests
#   ARGV[4] = new member (unique)
# Returns post-action count.
_RECORD_SCRIPT = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local max_requests = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now_ms - window_ms)
local count = redis.call('ZCARD', key)
if count >= max_requests then
    return count
end
redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window_ms + 1000)
return count + 1
"""


class RateLimiter:
    """Per-proxy rate limiter with cross-instance shared sliding window.

    Args:
        redis_client: Redis client for sliding-window + quarantine state.
    """

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client
        # proxy_id -> monotonic timestamp when quarantine expires
        # Redis is authoritative; this is a hot-path cache to keep
        # is_quarantined() synchronous.
        self._quarantine_expiry: dict[str, float] = {}

    def is_quarantined(self, proxy_id: str) -> bool:
        """Check if a proxy is currently quarantined (sync, no I/O)."""
        expiry = self._quarantine_expiry.get(proxy_id)
        if expiry is None:
            return False
        if time.monotonic() >= expiry:
            del self._quarantine_expiry[proxy_id]
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
        """Record a request and quarantine the proxy if the limit is exceeded."""
        if self.is_quarantined(proxy_id):
            return

        now_ms = int(time.time() * 1000)
        window_ms = window_seconds * 1000
        key = PROXY_REQUESTS_KEY.format(proxy_id=proxy_id)
        member = f"{now_ms}:{uuid.uuid4().hex[:12]}"

        result = await self._redis_client.client.eval(  # type: ignore[misc]
            _RECORD_SCRIPT,
            1,
            key,
            str(now_ms),
            str(window_ms),
            str(max_requests),
            member,
        )
        count = int(result)

        if count >= max_requests:
            duration = random.uniform(quarantine_seconds_min, quarantine_seconds_max)
            self._quarantine_expiry[proxy_id] = time.monotonic() + duration

            # Reset the window so post-quarantine counting starts fresh.
            await self._redis_client.client.delete(key)

            ttl = int(duration) + 1
            qkey = PROXY_QUARANTINE_KEY.format(proxy_id=proxy_id)
            await self._redis_client.client.setex(qkey, ttl, "1")

            logger.info(
                "Proxy quarantined due to rate limit",
                proxy_id=proxy_id,
                connector_id=connector_id,
                max_requests=max_requests,
                window_seconds=window_seconds,
                quarantine_seconds=round(duration, 1),
            )
            # Tell peers to refresh their local quarantine cache. They will
            # observe this via ``proxy_quarantine_changed`` and hydrate from
            # the Redis TTL key (which is already set above).
            await event_bus.publish(
                proxy_quarantine_changed,
                self,
                entity_id=proxy_id,
                op="quarantined",
            )

    async def hydrate_from_redis(self, proxy_ids: list[str]) -> None:
        """Restore in-memory quarantine cache from Redis TTLs after restart."""
        now = time.monotonic()
        restored = 0
        for proxy_id in proxy_ids:
            key = PROXY_QUARANTINE_KEY.format(proxy_id=proxy_id)
            ttl = await self._redis_client.client.ttl(key)
            if ttl is not None and ttl > 0:
                self._quarantine_expiry[proxy_id] = now + ttl
                restored += 1
        if restored:
            logger.info("Restored quarantine state from Redis", count=restored)

    async def refresh_quarantine_for(self, proxy_id: str) -> None:
        """Re-read quarantine TTL for a single proxy from Redis.

        Used by the cross-instance subscriber when a peer publishes
        ``proxy_quarantine_changed`` so this instance's selection logic
        immediately respects the peer's decision.
        """
        key = PROXY_QUARANTINE_KEY.format(proxy_id=proxy_id)
        ttl = await self._redis_client.client.ttl(key)
        if ttl is not None and ttl > 0:
            self._quarantine_expiry[proxy_id] = time.monotonic() + ttl
        else:
            self._quarantine_expiry.pop(proxy_id, None)

    async def unquarantine(self, proxy_id: str) -> bool:
        """Forcefully remove a proxy from quarantine.

        Returns True if the proxy was quarantined, False otherwise.
        """
        if proxy_id not in self._quarantine_expiry:
            return False
        del self._quarantine_expiry[proxy_id]
        qkey = PROXY_QUARANTINE_KEY.format(proxy_id=proxy_id)
        rkey = PROXY_REQUESTS_KEY.format(proxy_id=proxy_id)
        await self._redis_client.client.delete(qkey, rkey)
        logger.info("Proxy manually unquarantined", proxy_id=proxy_id)
        await event_bus.publish(
            proxy_quarantine_changed, self, entity_id=proxy_id, op="released"
        )
        return True

    async def remove_proxy(self, proxy_id: str) -> None:
        """Clean up all rate-limiter state for a removed proxy."""
        await self.remove_proxies([proxy_id])

    async def remove_proxies(self, proxy_ids: list[str]) -> None:
        """Clean up state for multiple proxies at once."""
        for proxy_id in proxy_ids:
            self._quarantine_expiry.pop(proxy_id, None)
        if proxy_ids:
            keys: list[str] = []
            for pid in proxy_ids:
                keys.append(PROXY_QUARANTINE_KEY.format(proxy_id=pid))
                keys.append(PROXY_REQUESTS_KEY.format(proxy_id=pid))
            await self._redis_client.client.delete(*keys)

    def clear_connector_proxies(self, proxy_ids: list[str]) -> None:
        """Drop in-memory quarantine cache for proxies.

        Used when a connector's rate-limit config changes; the underlying
        Redis state (quarantine TTL + requests ZSET) stays because the
        proxies still exist and future record_request calls will operate
        against the freshly-configured window.
        """
        for proxy_id in proxy_ids:
            self._quarantine_expiry.pop(proxy_id, None)
