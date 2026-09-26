# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Redis client for operational data storage."""

import time
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

import redis.asyncio as redis
import structlog

from api.core import utc_now
from api.core.stats import MetricDelta
from api.models.proxy import ProxyStatus

logger = structlog.get_logger()

# Redis key prefixes. Keep all key formats in this module so the global
# layout is auditable from one place.
PROXY_STATUS_KEY = "proxy:status:{proxy_id}"
# Field of the status hash holding the exit IP the observation flusher last
# counted as a hand-out for the proxy. Runtime bookkeeping like the health
# fields: refreshed as sightings arrive, gone with the hash when the proxy is.
# Only ever written into an existing hash, so the flusher cannot bring back
# the hash of a removed proxy.
PROXY_EXIT_IP_FIELD = "exit_ip"
_SET_FIELD_IF_KEY_EXISTS = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
    return 1
end
return 0
"""
PROXY_METRICS_KEY = "proxy:metrics:{proxy_id}"
PROJECT_METRICS_KEY = "project:metrics:{project_id}"
CONNECTOR_METRICS_KEY = "connector:metrics:{connector_id}"
# Set while a connector takes no requests because its traffic limit was
# reached (api.core.traffic_limiter). The value is the epoch second the
# current period ends, which is also the key's expiry: a block never
# outlives the period it was raised in.
CONNECTOR_TRAFFIC_BLOCKED_KEY = "connector:traffic_blocked:{connector_id}"
SESSION_KEY = "session:{session_id}"
STICKY_BINDING_KEY = "sticky:{project_id}:{session_id}"
MITM_REQUESTS_KEY = "mitm:requests:{project_id}"
INSTANCE_REGISTRY_KEY = "instance_registry:{instance_id}"
INSTANCE_REGISTRY_SCAN = "instance_registry:*"
# What one instance can see about itself and nobody else can: its runtime,
# its in-memory caches and its background-worker counters. Published on the
# same heartbeat as the registry key above so the admin system view can show
# the workers of every instance, not just the one the load balancer picked.
#
# Deliberately a second key rather than a richer registry payload: the
# registry value is a bare role string that peers on an older version still
# read, and a rolling upgrade should not make them render a JSON blob as the
# role. The scan globs stay disjoint because the prefixes differ.
INSTANCE_STATS_KEY = "instance_stats:{instance_id}"
INSTANCE_STATS_SCAN = "instance_stats:*"
# TTL of both instance keys, and the interval on which they are rewritten.
# The interval is half the TTL so one missed write does not declare an
# instance dead. Reading code derives snapshot age from the remaining TTL,
# which avoids comparing clocks across hosts.
INSTANCE_TTL_SECONDS = 10
INSTANCE_HEARTBEAT_INTERVAL = 5
# Pub/sub channel used by ``ProxyManager`` to broadcast accumulated
# metric deltas across instances so peers can update in-memory totals
# without each having to read Redis on a polling cadence.
METRIC_DELTAS_CHANNEL = "octoprox:metric_deltas"
# Per-proxy rate-limiter state (used by api.core.rate_limiter)
PROXY_QUARANTINE_KEY = "proxy:quarantine:{proxy_id}"
PROXY_REQUESTS_KEY = "proxy:requests:{proxy_id}"
# Lease keys (used by api.core.leadership). Lease names embed the
# resource id, e.g. "metrics_flusher", "autoscaler:<connector_id>".
LEASE_KEY = "lease:{name}"
# Cross-instance autoscaler cooldown state (hash, field=connector_id)
AUTOSCALER_LAST_ACTION_KEY = "autoscaler:last_action"
# IP attribution: observations queued for the leader's flusher, and per-session
# preflight verdicts.
GEO_OBSERVATIONS_KEY = "geo:observations"
GEO_PREFLIGHT_KEY = "geo:preflight:{project_id}:{proxy_id}"  # one verdict per proxy, shared by its sessions

# Logical grouping of the keyspace, used by the admin system view to report
# what Redis memory is being spent on. Every prefix written above appears
# here; anything unmatched falls into ``OTHER_KEY_GROUP``.
REDIS_KEY_GROUPS: tuple[tuple[str, str], ...] = (
    ("proxy:status:", "Proxy health"),
    ("proxy:metrics:", "Proxy metrics"),
    ("proxy:quarantine:", "Quarantine"),
    ("proxy:requests:", "Rate-limit windows"),
    ("project:metrics:", "Project metrics"),
    ("connector:metrics:", "Connector metrics"),
    ("connector:traffic_blocked:", "Traffic limits"),
    ("sticky:", "Sticky bindings"),
    ("session:", "Sessions"),
    ("mitm:requests:", "MITM captures"),
    ("instance_registry:", "Instance heartbeats"),
    ("instance_stats:", "Instance snapshots"),
    ("lease:", "Worker leases"),
    ("autoscaler:", "Auto-scaler state"),
    ("geo:", "IP attribution"),
)

OTHER_KEY_GROUP = "Other"

LEASE_SCAN = "lease:*"


def classify_key(key: str) -> str:
    """Return the :data:`REDIS_KEY_GROUPS` label a Redis key belongs to."""
    for prefix, label in REDIS_KEY_GROUPS:
        if key.startswith(prefix):
            return label
    return OTHER_KEY_GROUP


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
        # The observation flusher keeps its ``exit_ip`` field in this hash; a
        # hash holding only that (a proxy not yet health-checked, or one whose
        # sighting was flushed after its removal) carries no status.
        if not data or "status" not in data:
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

    async def get_proxy_exit_ips(self, proxy_ids: Iterable[str]) -> dict[str, str | None]:
        """The last counted exit IP per proxy, None where none is recorded. One round trip."""
        ids = list(dict.fromkeys(proxy_ids))
        if not ids:
            return {}
        pipe = self.client.pipeline()
        for proxy_id in ids:
            pipe.hget(PROXY_STATUS_KEY.format(proxy_id=proxy_id), PROXY_EXIT_IP_FIELD)
        values = await pipe.execute()
        result: dict[str, str | None] = {}
        for proxy_id, value in zip(ids, values, strict=True):
            if isinstance(value, bytes):
                value = value.decode()
            result[proxy_id] = value if isinstance(value, str) and value else None
        return result

    async def set_proxy_exit_ips(self, exits: dict[str, str]) -> int:
        """Record the exit IP last counted for each proxy whose status hash exists. One round trip.

        The check and the write are one atomic script per proxy, so a hash
        deleted by a proxy removal is never recreated, not even in the gap
        between looking and writing. A proxy the health checker has not
        written yet is skipped too; the flusher's exit-table fallback covers
        its sightings until then. Returns how many were written.
        """
        if not exits:
            return 0
        pipe = self.client.pipeline()
        for proxy_id, ip in exits.items():
            pipe.eval(_SET_FIELD_IF_KEY_EXISTS, 1, PROXY_STATUS_KEY.format(proxy_id=proxy_id), PROXY_EXIT_IP_FIELD, ip)
        results = await pipe.execute()
        return sum(1 for r in results if r)

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

    async def flush_metric_deltas(
        self,
        proxy_deltas: dict[str, MetricDelta],
        project_deltas: dict[str, MetricDelta],
        connector_deltas: dict[str, MetricDelta] | None = None,
    ) -> None:
        """Apply batched per-entity metric deltas in a single pipeline.

        Each delta dict mirrors the per-request fields written by
        ``update_proxy_metrics`` / ``update_project_metrics`` but
        carries an aggregate across many requests. Used by the
        periodic flush loop in ``ProxyManager`` so the hot path no
        longer pays a Redis round-trip per request.
        """
        connector_deltas = connector_deltas or {}
        if not proxy_deltas and not project_deltas and not connector_deltas:
            return
        now = utc_now().isoformat()
        pipe = self.client.pipeline()
        for proxy_id, d in proxy_deltas.items():
            key = PROXY_METRICS_KEY.format(proxy_id=proxy_id)
            self._pipeline_metric_delta(pipe, key, d, now)
        for project_id, d in project_deltas.items():
            key = PROJECT_METRICS_KEY.format(project_id=project_id)
            self._pipeline_metric_delta(pipe, key, d, now)
        for connector_id, d in connector_deltas.items():
            key = CONNECTOR_METRICS_KEY.format(connector_id=connector_id)
            self._pipeline_metric_delta(pipe, key, d, now)
        await pipe.execute()

    @staticmethod
    def _pipeline_metric_delta(
        pipe: Any, key: str, delta: MetricDelta, now_iso: str
    ) -> None:
        """Queue HINCRBY/HINCRBYFLOAT ops for one entity onto a Redis pipeline.

        Zero-valued fields are skipped so we don't emit no-op writes.
        """
        if delta.request_count:
            pipe.hincrby(key, "request_count", delta.request_count)
        if delta.success_count:
            pipe.hincrby(key, "success_count", delta.success_count)
        if delta.failure_count:
            pipe.hincrby(key, "failure_count", delta.failure_count)
        if delta.latency_sum_ms:
            pipe.hincrbyfloat(key, "latency_sum_ms", delta.latency_sum_ms)
        if delta.bytes_sent:
            pipe.hincrby(key, "bytes_sent", delta.bytes_sent)
        if delta.bytes_received:
            pipe.hincrby(key, "bytes_received", delta.bytes_received)
        pipe.hset(key, "updated_at", now_iso)

    async def get_proxy_metrics(self, proxy_id: str) -> MetricDelta | None:
        """Get proxy metrics from Redis."""
        key = PROXY_METRICS_KEY.format(proxy_id=proxy_id)
        return self._read_metrics_hash(await self.client.hgetall(key))  # type: ignore[misc]

    async def get_all_proxy_metrics(self) -> dict[str, MetricDelta]:
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

    async def get_project_metrics(self, project_id: str) -> MetricDelta | None:
        """Get project-level metrics from Redis."""
        key = PROJECT_METRICS_KEY.format(project_id=project_id)
        return self._read_metrics_hash(await self.client.hgetall(key))  # type: ignore[misc]

    async def get_all_project_metrics(self) -> dict[str, MetricDelta]:
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

    # Connector metrics operations: the same hash layout as proxies and
    # projects, one per connector, drained by the leader into connector_metrics.
    async def get_connector_metrics(self, connector_id: str) -> MetricDelta | None:
        """Get connector-level metrics from Redis."""
        key = CONNECTOR_METRICS_KEY.format(connector_id=connector_id)
        return self._read_metrics_hash(await self.client.hgetall(key))  # type: ignore[misc]

    async def get_all_connector_metrics(self) -> dict[str, MetricDelta]:
        """Get all connector-level metrics from Redis."""
        metrics = {}
        async for key in self.client.scan_iter(match="connector:metrics:*"):
            connector_id = key.split(":")[-1]
            m = await self.get_connector_metrics(connector_id)
            if m:
                metrics[connector_id] = m
        return metrics

    async def reset_connector_metrics(self, connector_id: str) -> None:
        """Reset connector metrics after flushing to Postgres, or when the connector goes."""
        key = CONNECTOR_METRICS_KEY.format(connector_id=connector_id)
        await self.client.delete(key)

    @staticmethod
    def _read_metrics_hash(data: dict[str, Any]) -> MetricDelta | None:
        """Decode one metrics hash into the additive batch shape, None for an absent key."""
        if not data:
            return None
        return MetricDelta(
            request_count=int(data.get("request_count", 0)),
            success_count=int(data.get("success_count", 0)),
            failure_count=int(data.get("failure_count", 0)),
            latency_sum_ms=float(data.get("latency_sum_ms", 0)),
            bytes_sent=int(data.get("bytes_sent", 0)),
            bytes_received=int(data.get("bytes_received", 0)),
        )

    # Traffic limit block state (api.core.traffic_limiter)
    async def set_connector_traffic_blocked(self, connector_id: str, until_epoch: float) -> None:
        """Mark a connector blocked until ``until_epoch`` (seconds), expiring with the period."""
        key = CONNECTOR_TRAFFIC_BLOCKED_KEY.format(connector_id=connector_id)
        ttl = max(1, int(until_epoch - time.time()) + 1)
        await self.client.set(key, repr(until_epoch), ex=ttl)

    async def get_connector_traffic_blocked(self, connector_id: str) -> float | None:
        """The epoch second a connector's block ends, or None when it is not blocked."""
        key = CONNECTOR_TRAFFIC_BLOCKED_KEY.format(connector_id=connector_id)
        return self._blocked_until(await self.client.get(key))

    async def get_connector_traffic_blocked_many(
        self, connector_ids: Iterable[str]
    ) -> dict[str, float]:
        """Block end per connector for those currently blocked, in one round trip."""
        ids = list(connector_ids)
        if not ids:
            return {}
        values = await self.client.mget(
            [CONNECTOR_TRAFFIC_BLOCKED_KEY.format(connector_id=cid) for cid in ids]
        )
        blocked: dict[str, float] = {}
        for connector_id, value in zip(ids, values, strict=True):
            until = self._blocked_until(value)
            if until is not None:
                blocked[connector_id] = until
        return blocked

    @staticmethod
    def _blocked_until(value: Any) -> float | None:
        """Decode one block key's value; None when absent, unreadable or already over."""
        if value is None:
            return None
        try:
            until = float(value)
        except (TypeError, ValueError):
            return None
        return until if until > time.time() else None

    async def clear_connector_traffic_blocked(self, connector_id: str) -> None:
        """Lift a connector's traffic block."""
        key = CONNECTOR_TRAFFIC_BLOCKED_KEY.format(connector_id=connector_id)
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

    # Sticky-session bindings (project-scoped) used by select_proxy_for_project
    # to maintain session→proxy affinity across instances.
    async def get_sticky_binding(self, project_id: str, session_id: str) -> str | None:
        key = STICKY_BINDING_KEY.format(project_id=project_id, session_id=session_id)
        result: str | None = await self.client.get(key)
        return result

    async def set_sticky_binding(
        self, project_id: str, session_id: str, proxy_id: str, ttl_seconds: int = 300
    ) -> None:
        key = STICKY_BINDING_KEY.format(project_id=project_id, session_id=session_id)
        await self.client.setex(key, ttl_seconds, proxy_id)

    async def delete_sticky_binding(self, project_id: str, session_id: str) -> None:
        key = STICKY_BINDING_KEY.format(project_id=project_id, session_id=session_id)
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
