"""Redis client for operational data storage."""

from datetime import datetime
from functools import lru_cache
from typing import Any

import redis.asyncio as redis
import structlog

from api.models.proxy import ProxyStatus

logger = structlog.get_logger()

# Redis key prefixes
PROXY_STATUS_KEY = "proxy:status:{proxy_id}"
PROXY_METRICS_KEY = "proxy:metrics:{proxy_id}"
SESSION_KEY = "session:{session_id}"


class RedisClient:
    """Redis client wrapper for operational data.

    Args:
        redis_url: Redis connection URL.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: redis.Redis | None = None

    async def connect(self) -> None:
        """Connect to Redis."""
        self._client = redis.from_url(
            self._redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        logger.info("Connected to Redis", url=self._redis_url)
    
    async def close(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.close()
            logger.info("Closed Redis connection")
    
    @property
    def client(self) -> redis.Redis:
        """Get Redis client, raising if not connected."""
        if self._client is None:
            raise RuntimeError("Redis client not connected")
        return self._client
    
    # Proxy status operations
    async def set_proxy_status(
        self,
        proxy_id: str,
        status: ProxyStatus,
        latency_ms: float = 0.0,
        consecutive_failures: int = 0,
    ) -> None:
        """Set proxy health status in Redis."""
        key = PROXY_STATUS_KEY.format(proxy_id=proxy_id)
        data = {
            "status": status.value,
            "latency_ms": latency_ms,
            "consecutive_failures": consecutive_failures,
            "updated_at": datetime.utcnow().isoformat(),
        }
        await self.client.hset(key, mapping=data)
    
    async def get_proxy_status(self, proxy_id: str) -> dict[str, Any] | None:
        """Get proxy health status from Redis."""
        key = PROXY_STATUS_KEY.format(proxy_id=proxy_id)
        data = await self.client.hgetall(key)
        if not data:
            return None
        return {
            "status": ProxyStatus(data["status"]),
            "latency_ms": float(data["latency_ms"]),
            "consecutive_failures": int(data["consecutive_failures"]),
            "updated_at": data["updated_at"],
        }
    
    async def get_all_proxy_statuses(self) -> dict[str, dict[str, Any]]:
        """Get all proxy statuses from Redis."""
        statuses = {}
        async for key in self.client.scan_iter(match="proxy:status:*"):
            proxy_id = key.split(":")[-1]
            status = await self.get_proxy_status(proxy_id)
            if status:
                statuses[proxy_id] = status
        return statuses
    
    # Proxy metrics operations
    async def update_proxy_metrics(
        self,
        proxy_id: str,
        success: bool,
        latency_ms: float,
    ) -> None:
        """Update proxy metrics in Redis (incremental)."""
        key = PROXY_METRICS_KEY.format(proxy_id=proxy_id)
        pipe = self.client.pipeline()
        pipe.hincrby(key, "request_count", 1)
        if success:
            pipe.hincrby(key, "success_count", 1)
        else:
            pipe.hincrby(key, "failure_count", 1)
        # Store latest latency (we'll compute avg during flush)
        pipe.hset(key, "last_latency_ms", str(latency_ms))
        pipe.hset(key, "updated_at", datetime.utcnow().isoformat())
        await pipe.execute()
    
    async def get_proxy_metrics(self, proxy_id: str) -> dict[str, Any] | None:
        """Get proxy metrics from Redis."""
        key = PROXY_METRICS_KEY.format(proxy_id=proxy_id)
        data = await self.client.hgetall(key)
        if not data:
            return None
        return {
            "request_count": int(data.get("request_count", 0)),
            "success_count": int(data.get("success_count", 0)),
            "failure_count": int(data.get("failure_count", 0)),
            "last_latency_ms": float(data.get("last_latency_ms", 0)),
            "updated_at": data.get("updated_at"),
        }
    
    async def get_all_proxy_metrics(self) -> dict[str, dict[str, Any]]:
        """Get all proxy metrics from Redis."""
        metrics = {}
        async for key in self.client.scan_iter(match="proxy:metrics:*"):
            proxy_id = key.split(":")[-1]
            m = await self.get_proxy_metrics(proxy_id)
            if m:
                metrics[proxy_id] = m
        return metrics
    
    async def reset_proxy_metrics(self, proxy_id: str) -> None:
        """Reset proxy metrics after flushing to Postgres."""
        key = PROXY_METRICS_KEY.format(proxy_id=proxy_id)
        await self.client.delete(key)
    
    # Session operations (for sticky routing)
    async def set_session(self, session_id: str, proxy_id: str, ttl_seconds: int = 3600) -> None:
        """Set session to proxy mapping."""
        key = SESSION_KEY.format(session_id=session_id)
        await self.client.setex(key, ttl_seconds, proxy_id)
    
    async def get_session(self, session_id: str) -> str | None:
        """Get proxy ID for a session."""
        key = SESSION_KEY.format(session_id=session_id)
        return await self.client.get(key)
    
    async def delete_session(self, session_id: str) -> None:
        """Delete a session."""
        key = SESSION_KEY.format(session_id=session_id)
        await self.client.delete(key)


@lru_cache
def get_redis_client(redis_url: str) -> RedisClient:
    """Get or create a Redis client for the given URL.

    Uses lru_cache to ensure we reuse the same client for the same URL.

    Args:
        redis_url: Redis connection URL.

    Returns:
        Redis client instance.
    """
    return RedisClient(redis_url)
