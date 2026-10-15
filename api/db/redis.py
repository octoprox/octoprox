# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Redis client for operational data storage."""

import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import fields
from functools import lru_cache
from typing import Any

import redis.asyncio as redis
import structlog
from redis.commands.core import AsyncScript

from api.core import utc_now
from api.core.stats import FailureCountKey, MetricDelta, TunnelPeerMetricDelta
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
# Per tunnel device (any protocol), the fourth hash in the metrics pipeline;
# carries the two name-resolution counters as well (TunnelPeerMetricDelta).
TUNNEL_PEER_METRICS_KEY = "tunnel_peer:metrics:{peer_id}"
TUNNEL_PEER_METRICS_SCAN = "tunnel_peer:metrics:*"
# Per destination host under one connector of one project, the fifth hash in
# the metrics pipeline (the Hosts page). The host is the last segment so a
# bracketed IPv6 literal's colons do not break the key apart; the two ids
# before it are UUIDs and never contain a colon.
HOST_METRICS_PREFIX = "host:metrics:"
HOST_METRICS_KEY = HOST_METRICS_PREFIX + "{project_id}:{connector_id}:{host}"
HOST_METRICS_SCAN = HOST_METRICS_PREFIX + "*"
HOST_METRICS_PROJECT_SCAN = HOST_METRICS_PREFIX + "{project_id}:*"
HOST_METRICS_CONNECTOR_SCAN = HOST_METRICS_PREFIX + "*:{connector_id}:*"
# Set while a connector takes no requests because its traffic limit was
# reached (api.core.traffic_limiter). The value is the epoch second the
# current period ends, which is also the key's expiry: a block never
# outlives the period it was raised in.
CONNECTOR_TRAFFIC_BLOCKED_KEY = "connector:traffic_blocked:{connector_id}"
SESSION_KEY = "session:{session_id}"
STICKY_BINDING_KEY = "sticky:{project_id}:{session_id}"
MITM_REQUESTS_KEY = "mitm:requests:{project_id}"
# Each project's failure log (api.core.failure_log): one capped stream of
# examples per kind of failure, newest last, trimmed by length and age as it
# is written, so a flood of one kind never pushes out the rare ones. The
# appended hash counts the entries each stream has ever been given, which
# tells the page how many are newer than the ones it shows. A stream without
# a kind is the single log of the first release; it expires on its own.
FAILURE_LOG_KEY = "failures:{project_id}:{kind}"
FAILURE_LOG_APPENDED_KEY = "failures:{project_id}:appended"
LEGACY_FAILURE_LOG_KEY = "failures:{project_id}"
# Failure counts by (project, connector, kind, vendor status), the sixth hash
# in the metrics pipeline: written by each instance's failure log next to its
# examples, drained by the leader into failure_metrics. "-" stands for no
# connector (refused before a proxy was chosen) and for no vendor status.
FAILURE_METRICS_PREFIX = "failure:metrics:"
FAILURE_METRICS_KEY = FAILURE_METRICS_PREFIX + "{project_id}:{connector_id}:{kind}:{upstream_status}"
FAILURE_METRICS_SCAN = FAILURE_METRICS_PREFIX + "*"
FAILURE_METRICS_PROJECT_SCAN = FAILURE_METRICS_PREFIX + "{project_id}:*"
FAILURE_METRICS_CONNECTOR_SCAN = FAILURE_METRICS_PREFIX + "*:{connector_id}:*"
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
# One hash per instance carrying a tunnel protocol (peer key -> JSON status),
# rewritten every TUNNEL_STATUS_INTERVAL seconds and expiring soon after the
# carrier stops, so a dead carrier's peers read as offline. Several carriers
# behind a load balancer each publish their own; readers merge. The protocol
# is the key prefix: ``wireguard:peer_status:...``, ``openvpn:peer_status:...``.
TUNNEL_PEER_STATUS_KEY = "{protocol}:peer_status:{instance_id}"
TUNNEL_PEER_STATUS_SCAN = "{protocol}:peer_status:*"
# The fake-IP mapping the tunnel DNS hands out, shared so a connection that
# lands on a different instance from the one that answered the query still
# knows the name, whichever tunnel protocol carried either. Two keys per
# mapping, both refreshed when read, so a name nobody has asked about for a
# week frees its address.
TUNNEL_FAKEIP_NAME_KEY = "tunnel:fakeip:name:{name}"
TUNNEL_FAKEIP_ADDR_KEY = "tunnel:fakeip:addr:{offset}"
TUNNEL_FAKEIP_COUNTER_KEY = "tunnel:fakeip:next"
TUNNEL_FAKEIP_TTL_SECONDS = 7 * 86400
TUNNEL_STATUS_INTERVAL = 10
TUNNEL_STATUS_TTL_SECONDS = 30
GEO_PREFLIGHT_KEY = "geo:preflight:{project_id}:{proxy_id}"  # one verdict per proxy, shared by its sessions
# Sign-in through an identity provider (api.sso). The state key holds what a
# login that left for the provider needs when the browser comes back: which
# provider, the nonce and the PKCE verifier. The login code is the one-time
# code the browser trades for its bearer token on the way back in. Both live
# in Redis rather than in process memory because the callback may land on a
# different instance from the one that started the login.
SSO_STATE_KEY = "sso:state:{state}"
SSO_STATE_TTL_SECONDS = 600
SSO_LOGIN_CODE_KEY = "sso:login_code:{code}"
SSO_LOGIN_CODE_TTL_SECONDS = 60

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
    ("tunnel_peer:metrics:", "Tunnel device metrics"),
    ("host:metrics:", "Host metrics"),
    ("failure:metrics:", "Failure metrics"),
    ("connector:traffic_blocked:", "Traffic limits"),
    ("sticky:", "Sticky bindings"),
    ("session:", "Sessions"),
    ("mitm:requests:", "MITM captures"),
    ("failures:", "Failure log"),
    ("instance_registry:", "Instance heartbeats"),
    ("instance_stats:", "Instance snapshots"),
    ("lease:", "Worker leases"),
    ("autoscaler:", "Auto-scaler state"),
    ("geo:", "IP attribution"),
    ("wireguard:", "WireGuard"),
    ("openvpn:", "OpenVPN"),
    ("tunnel:", "Tunnel devices"),
    ("sso:", "Sign-in flows"),
)

OTHER_KEY_GROUP = "Other"

LEASE_SCAN = "lease:*"

# How many metrics hashes one pipeline reads, drains or deletes at a time.
_HOST_METRICS_BATCH = 500

# SCAN's COUNT hint for every walk of the keyspace. Without one Redis looks
# at 10 keys per call, so a MATCH over a busy keyspace (sticky bindings,
# sessions and preflight verdicts grow with traffic) costs a round trip per
# 10 keys, and every one of them waits its turn on a loaded event loop.
SCAN_COUNT = 500

# Takes one flush's counts off a metrics hash (KEYS[1]). ARGV holds triples:
# field, the amount to add (the flushed amount, negated), and "f" for a float
# field or "i" for an integer one. Subtracting rather than deleting keeps
# what the instances added between the flusher's read and this drain: it
# belongs to the next flush. Once the integer counters are back at zero the
# hash is deleted (a float sum may keep a rounding residue, so floats do not
# keep it alive). A hash gone since the read (its entity was removed) is left
# gone, and one with a counter below zero was reset and refilled since the
# read, so nothing in it can be told apart as the next flush's: it goes.
_DRAIN_METRICS_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
local floats = {}
for i = 1, #ARGV, 3 do
  if ARGV[i + 2] == 'f' then
    redis.call('HINCRBYFLOAT', KEYS[1], ARGV[i], ARGV[i + 1])
    floats[ARGV[i]] = true
  else
    redis.call('HINCRBY', KEYS[1], ARGV[i], ARGV[i + 1])
  end
end
local values = redis.call('HGETALL', KEYS[1])
local left = false
for i = 1, #values, 2 do
  local field = values[i]
  if field ~= 'updated_at' and not floats[field] then
    local n = tonumber(values[i + 1])
    if n ~= nil and n < 0 then
      redis.call('DEL', KEYS[1])
      return 0
    end
    if n ~= nil and n > 0 then left = true end
  end
end
if not left then redis.call('DEL', KEYS[1]) end
return left and 1 or 0
"""


def parse_host_metrics_key(key: str | bytes) -> tuple[str, str, str] | None:
    """(project_id, connector_id, host) from a host metrics key, None for anything else."""
    text = key if isinstance(key, str) else key.decode()
    if not text.startswith(HOST_METRICS_PREFIX):
        return None
    parts = text[len(HOST_METRICS_PREFIX):].split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


def failure_metrics_key(key: FailureCountKey) -> str:
    """The Redis hash a failure count is kept in until the leader flushes it."""
    project_id, connector_id, kind, upstream_status = key
    return FAILURE_METRICS_KEY.format(
        project_id=project_id,
        connector_id=connector_id or "-",
        kind=kind,
        upstream_status="-" if upstream_status is None else upstream_status,
    )


def parse_failure_metrics_key(key: str | bytes) -> FailureCountKey | None:
    """(project_id, connector_id, kind, upstream_status) from a failure metrics key, None for anything else."""
    text = key if isinstance(key, str) else key.decode()
    if not text.startswith(FAILURE_METRICS_PREFIX):
        return None
    parts = text[len(FAILURE_METRICS_PREFIX):].split(":")
    if len(parts) != 4 or not all(parts):
        return None
    project_id, connector_id, kind, status = parts
    try:
        upstream_status = None if status == "-" else int(status)
    except ValueError:
        return None
    return project_id, None if connector_id == "-" else connector_id, kind, upstream_status


def _stream_id_order(stream_id: str) -> tuple[int, int]:
    ms, seq = stream_id.split("-")
    return int(ms), int(seq)


def _stream_id_before(stream_id: str) -> str | None:
    """The greatest stream ID below ``stream_id``, None below 0-0: an exclusive upper bound."""
    ms, seq = _stream_id_order(stream_id)
    if seq > 0:
        return f"{ms}-{seq - 1}"
    if ms > 0:
        return f"{ms - 1}-18446744073709551615"
    return None


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
        self._drain_script: AsyncScript | None = None

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
        return self._read_status_hash(await self.client.hgetall(key))  # type: ignore[misc]

    @staticmethod
    def _read_status_hash(data: dict[str, Any]) -> dict[str, Any] | None:
        """Decode one proxy status hash, None when it carries no status."""
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
        for key, data in await self._read_hashes("proxy:status:*"):
            status = self._read_status_hash(data)
            if status:
                statuses[key.split(":")[-1]] = status
        return statuses

    async def _read_hashes(self, pattern: str) -> list[tuple[str, dict[str, Any]]]:
        """Every hash whose key matches ``pattern``, as ``(key, fields)``.

        One SCAN walk, then the hashes read in pipelined batches rather than a
        round trip each: the metrics flusher and every instance's full reload
        read all proxy, project and connector hashes once a minute. A key that
        expired or was drained between the walk and the read comes back empty.
        """
        keys = [
            key if isinstance(key, str) else key.decode()
            async for key in self.client.scan_iter(match=pattern, count=SCAN_COUNT)
        ]
        hashes: list[tuple[str, dict[str, Any]]] = []
        for start in range(0, len(keys), _HOST_METRICS_BATCH):
            batch = keys[start:start + _HOST_METRICS_BATCH]
            pipe = self.client.pipeline(transaction=False)
            for key in batch:
                pipe.hgetall(key)
            hashes.extend(zip(batch, await pipe.execute(), strict=True))
        return hashes

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
        tunnel_peer_deltas: dict[str, TunnelPeerMetricDelta] | None = None,
        host_deltas: dict[tuple[str, str, str], MetricDelta] | None = None,
    ) -> None:
        """Apply batched per-entity metric deltas in a single pipeline.

        Each delta dict mirrors the per-request fields written by
        ``update_proxy_metrics`` / ``update_project_metrics`` but
        carries an aggregate across many requests. Used by the
        periodic flush loop in ``ProxyManager`` so the hot path no
        longer pays a Redis round-trip per request. ``host_deltas`` is
        keyed by (project_id, connector_id, host).
        """
        connector_deltas = connector_deltas or {}
        tunnel_peer_deltas = tunnel_peer_deltas or {}
        host_deltas = host_deltas or {}
        if (
            not proxy_deltas
            and not project_deltas
            and not connector_deltas
            and not tunnel_peer_deltas
            and not host_deltas
        ):
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
        for peer_id, d in tunnel_peer_deltas.items():
            key = TUNNEL_PEER_METRICS_KEY.format(peer_id=peer_id)
            self._pipeline_metric_delta(pipe, key, d, now)
        for (project_id, connector_id, host), d in host_deltas.items():
            key = HOST_METRICS_KEY.format(project_id=project_id, connector_id=connector_id, host=host)
            self._pipeline_metric_delta(pipe, key, d, now)
        await pipe.execute()

    @staticmethod
    def _pipeline_metric_delta(
        pipe: Any, key: str, delta: MetricDelta, now_iso: str
    ) -> None:
        """Queue HINCRBY/HINCRBYFLOAT ops for one entity onto a Redis pipeline.

        The hash fields are the delta's fields (so a subclass's extra
        counters travel too). The command follows the field's declared
        type, not the value's: an int-valued float field must still go
        through HINCRBYFLOAT, or Redis rejects it once the hash field
        holds a fraction and the whole pipeline fails. Zero-valued fields
        are skipped so we don't emit no-op writes.
        """
        for f in fields(delta):
            value = getattr(delta, f.name)
            if not value:
                continue
            if f.type is float:
                pipe.hincrbyfloat(key, f.name, value)
            else:
                pipe.hincrby(key, f.name, value)
        pipe.hset(key, "updated_at", now_iso)

    async def get_proxy_metrics(self, proxy_id: str) -> MetricDelta | None:
        """Get proxy metrics from Redis."""
        key = PROXY_METRICS_KEY.format(proxy_id=proxy_id)
        return self._read_metrics_hash(await self.client.hgetall(key), MetricDelta)  # type: ignore[misc]

    async def get_all_proxy_metrics(self) -> dict[str, MetricDelta]:
        """Get all proxy metrics from Redis."""
        metrics = {}
        for key, data in await self._read_hashes("proxy:metrics:*"):
            m = self._read_metrics_hash(data, MetricDelta)
            if m:
                metrics[key.split(":")[-1]] = m
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
        return self._read_metrics_hash(await self.client.hgetall(key), MetricDelta)  # type: ignore[misc]

    async def get_all_project_metrics(self) -> dict[str, MetricDelta]:
        """Get all project-level metrics from Redis."""
        metrics = {}
        for key, data in await self._read_hashes("project:metrics:*"):
            m = self._read_metrics_hash(data, MetricDelta)
            if m:
                metrics[key.split(":")[-1]] = m
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
        return self._read_metrics_hash(await self.client.hgetall(key), MetricDelta)  # type: ignore[misc]

    async def get_all_connector_metrics(self) -> dict[str, MetricDelta]:
        """Get all connector-level metrics from Redis."""
        metrics = {}
        for key, data in await self._read_hashes("connector:metrics:*"):
            m = self._read_metrics_hash(data, MetricDelta)
            if m:
                metrics[key.split(":")[-1]] = m
        return metrics

    async def reset_connector_metrics(self, connector_id: str) -> None:
        """Reset connector metrics after flushing to Postgres, or when the connector goes."""
        key = CONNECTOR_METRICS_KEY.format(connector_id=connector_id)
        await self.client.delete(key)

    # Tunnel device metrics: the same hash layout, one per device of any
    # tunnel protocol, drained by the leader into tunnel_peer_metrics.
    async def get_tunnel_peer_metrics(self, peer_id: str) -> TunnelPeerMetricDelta | None:
        key = TUNNEL_PEER_METRICS_KEY.format(peer_id=peer_id)
        return self._read_metrics_hash(await self.client.hgetall(key), TunnelPeerMetricDelta)  # type: ignore[misc]

    async def get_all_tunnel_peer_metrics(self) -> dict[str, TunnelPeerMetricDelta]:
        metrics = {}
        for key, data in await self._read_hashes(TUNNEL_PEER_METRICS_SCAN):
            m = self._read_metrics_hash(data, TunnelPeerMetricDelta)
            if m:
                metrics[key.split(":", 2)[2]] = m
        return metrics

    async def reset_tunnel_peer_metrics(self, peer_id: str) -> None:
        """Reset a device's metrics after flushing to Postgres, or when the device goes."""
        await self.client.delete(TUNNEL_PEER_METRICS_KEY.format(peer_id=peer_id))

    # Host metrics: the same hash layout, one per (project, connector, host),
    # drained by the leader into host_metrics. There can be thousands of
    # these, so they are read and deleted in pipelined batches rather than
    # one round trip each.
    async def get_all_host_metrics(self) -> dict[tuple[str, str, str], MetricDelta]:
        """Every host metrics hash, keyed by (project_id, connector_id, host)."""
        metrics: dict[tuple[str, str, str], MetricDelta] = {}
        for raw_key, data in await self._read_hashes(HOST_METRICS_SCAN):
            parsed = parse_host_metrics_key(raw_key)
            m = self._read_metrics_hash(data, MetricDelta)
            if parsed is not None and m:
                metrics[parsed] = m
        return metrics

    async def reset_host_metrics_for_project(self, project_id: str) -> None:
        """Delete every host metrics hash of a project, when the project goes."""
        await self._delete_matching(HOST_METRICS_PROJECT_SCAN.format(project_id=project_id))

    async def reset_host_metrics_for_connector(self, connector_id: str) -> None:
        """Delete every host metrics hash of a connector, when the connector goes."""
        await self._delete_matching(HOST_METRICS_CONNECTOR_SCAN.format(connector_id=connector_id))

    async def _delete_matching(self, pattern: str) -> None:
        batch: list[Any] = []
        async for key in self.client.scan_iter(match=pattern, count=SCAN_COUNT):
            batch.append(key)
            if len(batch) >= _HOST_METRICS_BATCH:
                await self.client.delete(*batch)
                batch = []
        if batch:
            await self.client.delete(*batch)

    @staticmethod
    def _read_metrics_hash[D: MetricDelta](data: dict[str, Any], shape: type[D]) -> D | None:
        """Decode one metrics hash into the additive batch shape, None for an absent key.

        Hash values are strings; ``from_dict`` converts them. Fields the
        hash lacks (a peer on an older version wrote it) read as zero.
        """
        if not data:
            return None
        return shape.from_dict({k: v for k, v in data.items() if k != "updated_at"})

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
        return await self._stream_page(key, count, before_id)

    async def _stream_page(self, key: str, count: int, before_id: str | None) -> list[dict[str, str]]:
        """Newest-first page of a stream, strictly older than ``before_id`` when given."""
        if before_id:
            before = _stream_id_before(before_id)
            if before is None:
                return []  # nothing is older than 0-0
            end = before
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

    # Failure log operations

    async def append_failures(
        self,
        entries: list[tuple[str, str, dict[str, str]]],
        counts: dict[FailureCountKey, int],
        *,
        max_entries: int,
        retention_seconds: int,
    ) -> None:
        """Write a window of the failure log, (project_id, kind, fields) examples and counts, in one round trip.

        Each kind's stream keeps its newest ``max_entries`` entries, loses
        entries older than the retention, and expires whole once that kind
        has not failed for that long. The counts go to the metrics hashes
        the leader flushes into failure_metrics.
        """
        if not entries and not counts:
            return
        # The window's examples are given IDs in the order passed (oldest
        # first) across every kind, so a page merged from several kinds'
        # streams reads in time order: auto IDs would restart at -0 in each
        # stream. An ID another instance has already passed in that stream
        # (same millisecond, or a clock ahead of ours) is refused; those few
        # are appended again with an automatic ID, in a second round trip
        # only when there are any.
        now_ms = int(time.time() * 1000)
        min_id = f"{int(time.time() - retention_seconds) * 1000}-0"
        # Examples per stream: one HINCRBY each on the appended hash, not one per example.
        streams: Counter[tuple[str, str]] = Counter()
        pipe = self.client.pipeline(transaction=False)
        for seq, (project_id, kind, entry) in enumerate(entries):
            pipe.xadd(
                FAILURE_LOG_KEY.format(project_id=project_id, kind=kind), entry,  # type: ignore[arg-type]
                id=f"{now_ms}-{seq}", maxlen=max_entries, approximate=False,
            )
            streams[(project_id, kind)] += 1
        for (project_id, kind), appended in streams.items():
            pipe.hincrby(FAILURE_LOG_APPENDED_KEY.format(project_id=project_id), kind, appended)
        for project_id, kind in streams:
            key = FAILURE_LOG_KEY.format(project_id=project_id, kind=kind)
            # Exact: an approximate MINID only drops whole stream nodes, so a
            # slowly failing kind would keep entries past the retention.
            pipe.xtrim(key, minid=min_id, approximate=False)
            pipe.expire(key, retention_seconds)
        for project_id in {project_id for project_id, _kind in streams}:
            pipe.expire(FAILURE_LOG_APPENDED_KEY.format(project_id=project_id), retention_seconds)
        for count_key, count in counts.items():
            pipe.hincrby(failure_metrics_key(count_key), "failure_count", count)
        results = await pipe.execute(raise_on_error=False)
        # The XADDs come first; anything else failing fails the window.
        failed = next((r for r in results[len(entries):] if isinstance(r, Exception)), None)
        if failed is not None:
            raise failed
        refused = [
            entry for entry, result in zip(entries, results[:len(entries)], strict=True) if isinstance(result, Exception)
        ]
        if refused:
            pipe = self.client.pipeline(transaction=False)
            for project_id, kind, entry in refused:
                key = FAILURE_LOG_KEY.format(project_id=project_id, kind=kind)
                pipe.xadd(key, entry, maxlen=max_entries, approximate=False)  # type: ignore[arg-type]
                # Again: the stream may have been cleared since, and XADD makes it anew without a TTL.
                pipe.expire(key, retention_seconds)
            await pipe.execute()

    async def get_failures(
        self,
        project_id: str,
        kinds: list[str],
        count: int = 200,
        after: tuple[str, str] | None = None,
    ) -> list[dict[str, str]]:
        """The newest examples across the kinds' streams, each with its stream ``id`` and ``kind``.

        Entries are ordered by (stream ID, kind), newest first: two streams
        can hand out the same ID in the same millisecond, and the kind
        settles which comes first. ``after`` is the (stream ID, kind) of the
        last entry of the previous page; this page holds what follows it.
        """
        bounds: list[tuple[str, str]] = []
        for kind in kinds:
            if after is None:
                bounds.append((kind, "+"))
                continue
            after_id, after_kind = after
            # Same ID, smaller kind: still after the cursor in this order.
            end = after_id if kind < after_kind else _stream_id_before(after_id)
            if end is not None:
                bounds.append((kind, end))
        if not bounds:
            return []
        pipe = self.client.pipeline(transaction=False)
        for kind, end in bounds:
            pipe.xrevrange(FAILURE_LOG_KEY.format(project_id=project_id, kind=kind), max=end, min="-", count=count)
        merged = [
            (entry_id, kind, entry)
            for (kind, _end), raw in zip(bounds, await pipe.execute(), strict=True)
            for entry_id, entry in raw
        ]
        merged.sort(key=lambda item: (_stream_id_order(item[0]), item[1]), reverse=True)
        return [{**entry, "id": entry_id, "kind": kind} for entry_id, kind, entry in merged[:count]]

    async def count_failures(self, project_id: str, kinds: list[str]) -> int:
        """How many examples the kinds' streams hold."""
        pipe = self.client.pipeline(transaction=False)
        for kind in kinds:
            pipe.xlen(FAILURE_LOG_KEY.format(project_id=project_id, kind=kind))
        return sum(int(n) for n in await pipe.execute())

    async def failures_appended(self, project_id: str) -> dict[str, int]:
        """How many examples each kind's stream has been given so far."""
        raw = await self.client.hgetall(FAILURE_LOG_APPENDED_KEY.format(project_id=project_id))  # type: ignore[misc]
        return {(k if isinstance(k, str) else k.decode()): int(v) for k, v in raw.items()}

    async def clear_failures(self, project_id: str) -> None:
        """Delete a project's failure log: every kind's examples and the appended counts. Counts in history stay."""
        await self.client.delete(LEGACY_FAILURE_LOG_KEY.format(project_id=project_id))
        await self._delete_matching(FAILURE_LOG_KEY.format(project_id=project_id, kind="*"))

    # Failure counts, drained by the leader into failure_metrics like the
    # host metrics hashes, in pipelined batches: read, then the flushed
    # amount subtracted.

    async def get_all_failure_metrics(self) -> dict[FailureCountKey, int]:
        """Every failure count hash, keyed by (project_id, connector_id, kind, upstream_status)."""
        keys: list[FailureCountKey] = []
        async for raw_key in self.client.scan_iter(match=FAILURE_METRICS_SCAN, count=SCAN_COUNT):
            parsed = parse_failure_metrics_key(raw_key)
            if parsed is not None:
                keys.append(parsed)
        counts: dict[FailureCountKey, int] = {}
        for start in range(0, len(keys), _HOST_METRICS_BATCH):
            batch = keys[start:start + _HOST_METRICS_BATCH]
            pipe = self.client.pipeline()
            for key in batch:
                pipe.hget(failure_metrics_key(key), "failure_count")
            for key, value in zip(batch, await pipe.execute(), strict=True):
                if value is not None and int(value) > 0:
                    counts[key] = int(value)
        return counts

    async def drain_flushed_metrics(
        self,
        *,
        proxies: dict[str, MetricDelta],
        projects: dict[str, MetricDelta],
        connectors: dict[str, MetricDelta],
        tunnel_peers: dict[str, TunnelPeerMetricDelta],
        hosts: dict[tuple[str, str, str], MetricDelta],
        failures: dict[FailureCountKey, int],
    ) -> None:
        """Take what a flush wrote to Postgres off the hashes it was read from.

        Called once the flush transaction has committed, with exactly what
        was read. Subtracted rather than deleted: the instances keep adding
        to these hashes while the flush runs, and what they added since the
        read belongs to the next flush (see ``_DRAIN_METRICS_SCRIPT``). A
        hash left at zero is deleted. Pipelined in batches, as there can be
        thousands of host hashes.
        """
        drains: list[tuple[str, list[str]]] = []
        for proxy_id, delta in proxies.items():
            drains.append((PROXY_METRICS_KEY.format(proxy_id=proxy_id), self._drain_args(delta)))
        for project_id, delta in projects.items():
            drains.append((PROJECT_METRICS_KEY.format(project_id=project_id), self._drain_args(delta)))
        for connector_id, delta in connectors.items():
            drains.append((CONNECTOR_METRICS_KEY.format(connector_id=connector_id), self._drain_args(delta)))
        for peer_id, peer_delta in tunnel_peers.items():
            drains.append((TUNNEL_PEER_METRICS_KEY.format(peer_id=peer_id), self._drain_args(peer_delta)))
        for (project_id, connector_id, host), delta in hosts.items():
            key = HOST_METRICS_KEY.format(project_id=project_id, connector_id=connector_id, host=host)
            drains.append((key, self._drain_args(delta)))
        for count_key, count in failures.items():
            drains.append((failure_metrics_key(count_key), ["failure_count", str(-count), "i"]))
        if self._drain_script is None:
            self._drain_script = self.client.register_script(_DRAIN_METRICS_SCRIPT)
        for start in range(0, len(drains), _HOST_METRICS_BATCH):
            pipe = self.client.pipeline(transaction=False)
            for name, args in drains[start:start + _HOST_METRICS_BATCH]:
                await self._drain_script(keys=[name], args=args, client=pipe)
            await pipe.execute()

    @staticmethod
    def _drain_args(delta: MetricDelta) -> list[str]:
        """The drain script's (field, negated amount, kind) triples for one hash's flushed delta."""
        args: list[str] = []
        for f in fields(delta):
            value = getattr(delta, f.name)
            if not value:
                continue
            # Same type test as _pipeline_metric_delta: the field's declared type, not the value's.
            if f.type is float:
                args += [f.name, repr(-float(value)), "f"]
            else:
                args += [f.name, str(-int(value)), "i"]
        return args

    async def reset_failure_metrics_for_project(self, project_id: str) -> None:
        """Delete every failure count hash of a project, when the project goes."""
        await self._delete_matching(FAILURE_METRICS_PROJECT_SCAN.format(project_id=project_id))

    async def reset_failure_metrics_for_connector(self, connector_id: str) -> None:
        """Delete every failure count hash of a connector, when the connector goes."""
        await self._delete_matching(FAILURE_METRICS_CONNECTOR_SCAN.format(connector_id=connector_id))

    # Tunnel peer status, per protocol

    async def set_tunnel_peer_status(self, protocol: str, instance_id: str, statuses: dict[str, str]) -> None:
        """Replace this instance's published peer statuses for a protocol (peer key -> JSON) with a fresh expiry."""
        key = TUNNEL_PEER_STATUS_KEY.format(protocol=protocol, instance_id=instance_id)
        pipe = self.client.pipeline()
        pipe.delete(key)
        if statuses:
            pipe.hset(key, mapping=statuses)
        pipe.expire(key, TUNNEL_STATUS_TTL_SECONDS)
        await pipe.execute()

    async def fakeip_offset_for(self, name: str) -> int | None:
        """The pool offset a name was given on any instance, refreshing its lease; None if never."""
        name_key = TUNNEL_FAKEIP_NAME_KEY.format(name=name)
        raw = await self.client.get(name_key)
        if raw is None:
            return None
        offset = int(raw)
        pipe = self.client.pipeline()
        pipe.expire(name_key, TUNNEL_FAKEIP_TTL_SECONDS)
        pipe.expire(TUNNEL_FAKEIP_ADDR_KEY.format(offset=offset), TUNNEL_FAKEIP_TTL_SECONDS)
        await pipe.execute()
        return offset

    async def fakeip_name_for(self, offset: int) -> str | None:
        """The name a pool offset was handed out for on any instance, refreshing its lease."""
        addr_key = TUNNEL_FAKEIP_ADDR_KEY.format(offset=offset)
        name = await self.client.get(addr_key)
        if name is None:
            return None
        pipe = self.client.pipeline()
        pipe.expire(addr_key, TUNNEL_FAKEIP_TTL_SECONDS)
        pipe.expire(TUNNEL_FAKEIP_NAME_KEY.format(name=name), TUNNEL_FAKEIP_TTL_SECONDS)
        await pipe.execute()
        return str(name)

    async def fakeip_allocate(self, name: str, capacity: int, attempts: int = 64) -> int:
        """Give ``name`` a free pool offset, cluster-wide.

        A shared counter walks the pool; an offset whose lease has not
        expired is skipped. Two instances allocating the same name at once
        each get an offset and both point at the name, which is harmless.
        """
        for _ in range(attempts):
            counter = int(await self.client.incr(TUNNEL_FAKEIP_COUNTER_KEY))
            offset = (counter - 1) % capacity
            claimed = await self.client.set(
                TUNNEL_FAKEIP_ADDR_KEY.format(offset=offset), name, nx=True, ex=TUNNEL_FAKEIP_TTL_SECONDS
            )
            if claimed:
                await self.client.set(
                    TUNNEL_FAKEIP_NAME_KEY.format(name=name), str(offset), ex=TUNNEL_FAKEIP_TTL_SECONDS
                )
                return offset
        raise RuntimeError(f"no free fake IP after {attempts} attempts; the range is exhausted")

    async def get_tunnel_peer_status(self, protocol: str) -> list[dict[str, str]]:
        """What every instance carrying a protocol last published, one hash per carrier; [] when none is."""
        hashes = await self._read_hashes(TUNNEL_PEER_STATUS_SCAN.format(protocol=protocol))
        return [data for _key, data in hashes if data]


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
