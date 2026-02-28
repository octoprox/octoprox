# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Redis client for operational data storage."""

from functools import lru_cache
from typing import Any

import redis.asyncio as redis
import structlog

from api.core import utc_now
from api.models.proxy import ProxyStatus

logger = structlog.get_logger()

# Redis key prefixes
PROXY_STATUS_KEY = "proxy:status:{proxy_id}"
PROXY_METRICS_KEY = "proxy:metrics:{proxy_id}"
PROJECT_METRICS_KEY = "project:metrics:{project_id}"
SESSION_KEY = "session:{session_id}"
MITM_REQUESTS_KEY = "mitm:requests:{project_id}"


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
        self._client = redis.from_url(  # type: ignore[no-untyped-call]
            self._redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        logger.info("Connected to Redis", url=self._redis_url)

    async def close(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.aclose()
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
            "updated_at": utc_now().isoformat(),
        }
        await self.client.hset(key, mapping=data)  # type: ignore[misc]

    async def get_proxy_status(self, proxy_id: str) -> dict[str, Any] | None:
        """Get proxy health status from Redis."""
        key = PROXY_STATUS_KEY.format(proxy_id=proxy_id)
        data = await self.client.hgetall(key)  # type: ignore[misc]
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

    async def delete_proxy_status(self, proxy_id: str) -> None:
        """Delete proxy status from Redis."""
        key = PROXY_STATUS_KEY.format(proxy_id=proxy_id)
        await self.client.delete(key)

    # Proxy metrics operations
    async def update_proxy_metrics(
        self,
        proxy_id: str,
        success: bool,
        latency_ms: float,
        bytes_sent: int = 0,
        bytes_received: int = 0,
    ) -> None:
        """Update proxy metrics in Redis (incremental)."""
        key = PROXY_METRICS_KEY.format(proxy_id=proxy_id)
        pipe = self.client.pipeline()
        pipe.hincrby(key, "request_count", 1)
        if success:
            pipe.hincrby(key, "success_count", 1)
        else:
            pipe.hincrby(key, "failure_count", 1)
        # Store latency sum for computing true average during flush
        pipe.hincrbyfloat(key, "latency_sum_ms", latency_ms)
        # Track bytes transferred
        if bytes_sent > 0:
            pipe.hincrby(key, "bytes_sent", bytes_sent)
        if bytes_received > 0:
            pipe.hincrby(key, "bytes_received", bytes_received)
        pipe.hset(key, "updated_at", utc_now().isoformat())
        await pipe.execute()

    async def get_proxy_metrics(self, proxy_id: str) -> dict[str, Any] | None:
        """Get proxy metrics from Redis."""
        key = PROXY_METRICS_KEY.format(proxy_id=proxy_id)
        data = await self.client.hgetall(key)  # type: ignore[misc]
        if not data:
            return None
        request_count = int(data.get("request_count", 0))
        latency_sum = float(data.get("latency_sum_ms", 0))
        avg_latency = latency_sum / request_count if request_count > 0 else 0.0
        return {
            "request_count": request_count,
            "success_count": int(data.get("success_count", 0)),
            "failure_count": int(data.get("failure_count", 0)),
            "latency_sum_ms": latency_sum,
            "avg_latency_ms": avg_latency,
            "bytes_sent": int(data.get("bytes_sent", 0)),
            "bytes_received": int(data.get("bytes_received", 0)),
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

    # Project metrics operations
    async def update_project_metrics(
        self,
        project_id: str,
        success: bool,
        latency_ms: float,
        bytes_sent: int = 0,
        bytes_received: int = 0,
    ) -> None:
        """Update project-level metrics in Redis (incremental).

        These metrics aggregate across all proxies in a project and persist
        across proxy rotation.
        """
        key = PROJECT_METRICS_KEY.format(project_id=project_id)
        pipe = self.client.pipeline()
        pipe.hincrby(key, "request_count", 1)
        if success:
            pipe.hincrby(key, "success_count", 1)
        else:
            pipe.hincrby(key, "failure_count", 1)
        # Store latency sum for computing true average during flush
        pipe.hincrbyfloat(key, "latency_sum_ms", latency_ms)
        # Track bytes transferred
        if bytes_sent > 0:
            pipe.hincrby(key, "bytes_sent", bytes_sent)
        if bytes_received > 0:
            pipe.hincrby(key, "bytes_received", bytes_received)
        pipe.hset(key, "updated_at", utc_now().isoformat())
        await pipe.execute()

    async def get_project_metrics(self, project_id: str) -> dict[str, Any] | None:
        """Get project-level metrics from Redis."""
        key = PROJECT_METRICS_KEY.format(project_id=project_id)
        data = await self.client.hgetall(key)  # type: ignore[misc]
        if not data:
            return None
        request_count = int(data.get("request_count", 0))
        latency_sum = float(data.get("latency_sum_ms", 0))
        avg_latency = latency_sum / request_count if request_count > 0 else 0.0
        return {
            "request_count": request_count,
            "success_count": int(data.get("success_count", 0)),
            "failure_count": int(data.get("failure_count", 0)),
            "latency_sum_ms": latency_sum,
            "avg_latency_ms": avg_latency,
            "bytes_sent": int(data.get("bytes_sent", 0)),
            "bytes_received": int(data.get("bytes_received", 0)),
            "updated_at": data.get("updated_at"),
        }

    async def get_all_project_metrics(self) -> dict[str, dict[str, Any]]:
        """Get all project-level metrics from Redis."""
        metrics = {}
        async for key in self.client.scan_iter(match="project:metrics:*"):
            project_id = key.split(":")[-1]
            m = await self.get_project_metrics(project_id)
            if m:
                metrics[project_id] = m
        return metrics

    async def reset_project_metrics(self, project_id: str) -> None:
        """Reset project metrics after flushing to Postgres."""
        key = PROJECT_METRICS_KEY.format(project_id=project_id)
        await self.client.delete(key)

    # Session operations (for sticky routing)
    async def set_session(self, session_id: str, proxy_id: str, ttl_seconds: int = 3600) -> None:
        """Set session to proxy mapping."""
        key = SESSION_KEY.format(session_id=session_id)
        await self.client.setex(key, ttl_seconds, proxy_id)

    async def get_session(self, session_id: str) -> str | None:
        """Get proxy ID for a session."""
        key = SESSION_KEY.format(session_id=session_id)
        result: str | None = await self.client.get(key)
        return result

    async def delete_session(self, session_id: str) -> None:
        """Delete a session."""
        key = SESSION_KEY.format(session_id=session_id)
        await self.client.delete(key)

    # MITM request recording operations
    async def record_mitm_request(
        self,
        project_id: str,
        fields: dict[str, str],
    ) -> None:
        """Append a MITM request record to the project's capped stream.

        Uses XADD with approximate MAXLEN to keep the last ~1000 entries.
        """
        key = MITM_REQUESTS_KEY.format(project_id=project_id)
        await self.client.xadd(key, fields, maxlen=1000, approximate=True)  # type: ignore[arg-type]

    async def get_mitm_requests(
        self,
        project_id: str,
        count: int = 50,
        before_id: str | None = None,
    ) -> list[dict[str, str]]:
        """Read MITM request records from the project's stream (newest first).

        Args:
            project_id: Project ID.
            count: Max number of records to return.
            before_id: If set, return records older than this stream entry ID
                (cursor-based pagination).

        Returns:
            List of dicts with stream entry ``id`` plus all field/value pairs.
        """
        key = MITM_REQUESTS_KEY.format(project_id=project_id)

        if before_id:
            # Make the bound exclusive by decrementing the sequence number
            parts = before_id.split("-")
            ts_part, seq_part = parts[0], int(parts[1])
            if seq_part > 0:
                end = f"{ts_part}-{seq_part - 1}"
            else:
                end = f"{int(ts_part) - 1}-18446744073709551615"
        else:
            end = "+"

        raw: list[tuple[str, dict[str, str]]] = await self.client.xrevrange(
            key,
            max=end,
            min="-",
            count=count,
        )
        return [{"id": entry_id, **fields} for entry_id, fields in raw]

    async def clear_mitm_requests(self, project_id: str) -> None:
        """Delete all MITM request records for a project."""
        key = MITM_REQUESTS_KEY.format(project_id=project_id)
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
