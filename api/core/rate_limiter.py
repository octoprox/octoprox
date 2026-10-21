# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Per-proxy rate limits: a sliding window shared by every gateway, and the quarantine it trips.

A full window trips the proxy in one transaction that starts its window over and announces the quarantine.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import structlog

from api.core.logging import error_text
from api.db.redis import PROXY_REQUESTS_KEY, QUARANTINE_KEY, RedisClient
from api.db.streams import RuntimeSignal

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

# What a trip hands on locally: (proxy_id, until), where until is when the
# quarantine ends (naive UTC). The gateway wires it to its local exclusions.
TripHandler = Callable[[str, datetime], None]


class RateLimiter:
    """Counts requests in each proxy's shared window and trips its quarantine when the window is full.

    Args:
        on_trip: told of every trip this limiter makes, so the proxy is left out of routing here at once.
    """

    def __init__(self, redis_client: RedisClient, *, on_trip: TripHandler) -> None:
        self._redis_client = redis_client
        self._on_trip = on_trip
        # Completed requests let through uncounted because Redis did not
        # answer, since start.
        self.window_errors = 0

    async def record_request(
        self,
        proxy_id: str,
        connector_id: str,
        max_requests: int,
        window_seconds: int,
        quarantine_seconds_min: int,
        quarantine_seconds_max: int,
    ) -> None:
        """Count one completed request in the proxy's window, tripping its quarantine when the window is full.

        Each call runs under the client's request deadline and never raises for Redis: a window that cannot be counted in
        time lets the request through.
        """
        redis = self._redis_client
        now = time.time()
        now_ms = int(now * 1000)
        key = PROXY_REQUESTS_KEY.format(proxy_id=proxy_id)
        member = f"{now_ms}:{uuid.uuid4().hex[:12]}"
        try:
            async with redis.request_deadline():
                result = await redis.client.eval(
                    _RECORD_SCRIPT, 1, key, str(now_ms), str(window_seconds * 1000), str(max_requests), member
                )
        except Exception as exc:
            self.window_errors += 1
            logger.debug("Rate limit window not counted", proxy_id=proxy_id, error=error_text(exc))
            return
        if int(result) < max_requests:
            return

        duration = random.uniform(quarantine_seconds_min, quarantine_seconds_max)
        until = now + duration
        # Out of routing here before anything else awaits.
        self._on_trip(proxy_id, datetime.fromtimestamp(until, UTC).replace(tzinfo=None))
        try:
            async with redis.request_deadline():
                pipe = redis.client.pipeline(transaction=True)
                # Counting starts over once the quarantine is over.
                pipe.delete(key)
                pipe.zadd(QUARANTINE_KEY, {proxy_id: until}, gt=True)
                # In the same transaction, so the controller never reads the entry without the quarantine.
                RuntimeSignal.QUARANTINE.append(pipe, proxy_id)
                await pipe.execute()
        except Exception as exc:
            # Rare: Redis answered the count a moment ago. The window is still
            # full, so the next request through the proxy trips it again.
            logger.warning("Rate limit quarantine not shared", proxy_id=proxy_id, error=error_text(exc))
        logger.info(
            "Proxy quarantined due to rate limit",
            proxy_id=proxy_id,
            connector_id=connector_id,
            max_requests=max_requests,
            window_seconds=window_seconds,
            quarantine_seconds=round(duration, 1),
        )

