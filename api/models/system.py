# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Response models for the admin system statistics endpoint.

The payload is deliberately split by *where the number comes from*, because
the answers have different authority in a multi-instance deployment:

* ``inventory`` / ``database`` - Postgres, the same for every instance.
* ``redis`` / ``workers.leases`` / ``workers.instances`` - the shared
  operational store, so also cluster-wide.
* ``runtime`` / ``cache`` / ``workers.tasks`` - the process that served the
  request, and nobody else. Every *other* instance reports the same three
  sections about itself in ``workers.instances[].snapshot``, published on its
  heartbeat rather than collected live - see :class:`InstanceSnapshot`.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class RuntimeStats(BaseModel):
    """Identity and configuration of the instance that answered the request."""

    version: str
    instance_id: str
    role: str
    environment: str
    python_version: str
    platform: str
    pid: int
    started_at: datetime | None = None
    uptime_seconds: float = 0.0
    api_port: int
    proxy_port: int
    log_level: str
    health_check_interval: int
    metrics_flush_interval: int
    ip_refresh_interval: int


class InventoryStats(BaseModel):
    """Exact entity counts from Postgres, across every project."""

    projects: int = 0
    credentials: int = 0
    connectors: int = 0
    connectors_enabled: int = 0
    connectors_failing: int = 0
    proxies: int = 0
    users: int = 0
    users_active: int = 0
    users_by_role: dict[str, int] = Field(default_factory=dict)
    providers_total: int = 0
    providers_builtin: int = 0
    providers_custom: int = 0
    custom_providers_enabled: int = 0
    # Health status is operational state, so it comes from the live pool this
    # instance holds rather than from Postgres.
    proxies_by_status: dict[str, int] = Field(default_factory=dict)


class ProjectUsage(BaseModel):
    """Per-project share of the inventory."""

    id: str
    name: str
    credentials: int
    connectors: int
    proxies: int


class TableStats(BaseModel):
    """Size of one Postgres table, including its indexes and TOAST data."""

    name: str
    # None until the table has been analysed at least once.
    row_estimate: int | None
    total_bytes: int
    table_bytes: int
    index_bytes: int


class DatabaseStats(BaseModel):
    """Postgres size and connection usage.

    ``row_estimate`` on each table comes from the planner statistics
    (``pg_class.reltuples``), which is why it is free to collect on tables
    with millions of metric rows - and why it drifts until the next ANALYZE,
    and is ``None`` on a table autovacuum has not reached yet.
    """

    name: str = ""
    size_bytes: int = 0
    tables: list[TableStats] = Field(default_factory=list)
    backends: int | None = None
    pool_size: int | None = None
    pool_checked_out: int | None = None
    error: str | None = None


class RedisKeyGroup(BaseModel):
    """Key count for one logical slice of the Redis keyspace."""

    label: str
    keys: int


class RedisStats(BaseModel):
    """Redis memory, throughput and keyspace composition."""

    version: str = ""
    uptime_seconds: int = 0
    used_memory_bytes: int = 0
    used_memory_peak_bytes: int = 0
    used_memory_rss_bytes: int = 0
    maxmemory_bytes: int = 0
    connected_clients: int = 0
    ops_per_sec: int = 0
    keyspace_hits: int = 0
    keyspace_misses: int = 0
    total_keys: int = 0
    groups: list[RedisKeyGroup] = Field(default_factory=list)
    scanned_keys: int = 0
    # True when the keyspace is larger than the scan budget, so ``groups``
    # describes a sample rather than the whole keyspace.
    truncated: bool = False
    error: str | None = None


class CacheStats(BaseModel):
    """Entry counts of the in-memory caches held by this instance."""

    projects: int = 0
    credentials: int = 0
    connectors: int = 0
    proxies: int = 0
    project_strategies: int = 0
    geo_provision_locks: int = 0
    pending_proxy_deltas: int = 0
    pending_project_deltas: int = 0
    quarantined_proxies: int = 0
    tls_contexts: int = 0
    provider_types: int = 0


class WorkerTask(BaseModel):
    """One background loop running in this process.

    Two different failure stories live here. ``state``/``error`` describe the
    asyncio task: "running" means the loop still exists. The run counters
    describe the work inside it: a loop that raises every cycle and swallows
    the error is still "running", and only ``consecutive_failures`` says so.

    ``scope`` is how the loop behaves across instances - ``"instance"`` runs
    everywhere, ``"singleton"`` only on whoever holds ``lease``, which is the
    name to match against ``WorkerStats.leases``. Counters are per-process and
    reset with it, so a standby's singleton worker shows zero runs.
    """

    name: str
    description: str = ""
    scope: Literal["instance", "singleton"] = "instance"
    # Lease name for a global singleton, lease name prefix for a per-resource
    # one (``autoscaler`` -> ``autoscaler:<connector id>``). None when the
    # worker runs on every instance.
    lease: str | None = None
    # Whether that lease is taken per resource. A global singleton holds its
    # lease continuously, so an absent lease means a failover gap. A
    # per-resource worker takes one per connector and releases it a moment
    # later, so between ticks no lease exists - which is idle, not standby,
    # and several instances can hold different ones at once. Without this,
    # "not in ``WorkerStats.leases``" reads the same for both.
    lease_per_resource: bool = False
    state: str  # running | done | cancelled | failed
    error: str | None = None
    # Cadence the loop was started with; null for the event-driven subscribers,
    # which run when a peer message arrives rather than on a clock.
    interval_seconds: float | None = None
    runs: int = 0
    # Runs that found nothing to do. A subset of ``runs``: subtract to get the
    # cycles that did something. The split is what separates "the loop is
    # alive" from "the loop is busy" - an install taking no traffic ticks the
    # metric-delta publisher every 5s forever with an empty buffer to flush.
    # Durations below describe the working cycles only.
    idle_runs: int = 0
    failures: int = 0
    # Cycles that took longer than ``interval_seconds``. A loop cannot start
    # its next cycle until the current one returns, so these are cycles that
    # pushed the worker off its configured cadence. ``overruns`` is the
    # lifetime count; ``consecutive_overruns`` is the streak the last cycle
    # back inside the cadence resets, i.e. whether the worker is behind *now*.
    overruns: int = 0
    consecutive_overruns: int = 0
    last_overrun_at: datetime | None = None
    consecutive_failures: int = 0
    last_run_at: datetime | None = None
    last_duration_ms: float | None = None
    avg_duration_ms: float | None = None
    max_duration_ms: float | None = None
    # The last cycle error, which - unlike ``error`` - the loop recovered from.
    last_error: str | None = None
    last_error_at: datetime | None = None


class LeaseInfo(BaseModel):
    """A Redis lease, i.e. which instance is currently running a singleton job.

    ``worker`` names the background loop that takes this lease, so a lease row
    and a ``WorkerStats.tasks`` row can be lined up: the lease says *where* the
    job runs right now, the task says *how* it is going on this instance.
    """

    name: str
    kind: str
    worker: str = ""
    target: str | None = None
    holder: str
    held_by_self: bool
    ttl_ms: int


class InstanceSnapshot(BaseModel):
    """What one instance publishes about itself on its heartbeat.

    The three sections a peer cannot collect on another's behalf: the process
    identity, the caches it holds in memory, and the counters of the loops it
    runs. Republished every :data:`api.db.redis.INSTANCE_HEARTBEAT_INTERVAL`
    seconds, so it is a recent reading rather than a live one - ``age_seconds``
    on :class:`InstanceInfo` says how recent.
    """

    runtime: RuntimeStats
    cache: CacheStats
    tasks: list[WorkerTask] = Field(default_factory=list)
    proxy_server_listening: bool = False
    proxy_server_connections: int = 0
    geo_lookup_enabled: bool = False
    geo_lookups_in_flight: int = 0


class InstanceInfo(BaseModel):
    """A live Octoprox process advertising itself in the instance registry.

    ``snapshot`` is what that process last published about itself. Both keys
    are written in one pipeline, so a live instance normally has one; it is
    None when that instance runs a version predating snapshots, or could not
    build one. ``age_seconds`` is derived from the snapshot key's remaining TTL
    rather than from its timestamp, so it does not depend on two hosts agreeing
    on the time.
    """

    instance_id: str
    role: str
    is_self: bool
    ttl_seconds: int
    # Present for this instance too, a few seconds behind the live ``runtime``
    # / ``cache`` / ``workers.tasks`` sections of the same response. Kept
    # uniform so a consumer reading the instance list does not have to
    # special-case the one that answered; the UI prefers the live sections.
    snapshot: InstanceSnapshot | None = None
    age_seconds: float | None = None


class WorkerStats(BaseModel):
    """What is running: locally as tasks, cluster-wide as leases."""

    tasks: list[WorkerTask] = Field(default_factory=list)
    leases: list[LeaseInfo] = Field(default_factory=list)
    instances: list[InstanceInfo] = Field(default_factory=list)
    proxy_server_listening: bool = False
    proxy_server_connections: int = 0
    geo_lookup_enabled: bool = False
    geo_lookups_in_flight: int = 0


class SystemStats(BaseModel):
    """Everything the admin system view renders."""

    generated_at: datetime
    runtime: RuntimeStats
    inventory: InventoryStats
    projects: list[ProjectUsage] = Field(default_factory=list)
    database: DatabaseStats
    redis: RedisStats
    cache: CacheStats
    workers: WorkerStats


# --- trend history -------------------------------------------------------------------


class SystemMetricsPoint(BaseModel):
    """One gauge reading, raw or averaged over a bucket."""

    timestamp: datetime
    database_size_bytes: int
    redis_memory_bytes: int
    redis_keys: int
    projects: int
    credentials: int
    connectors: int
    connectors_enabled: int
    users: int
    proxies_total: int
    proxies_healthy: int
    proxies_unhealthy: int


class TableGrowth(BaseModel):
    """How much one table grew across the requested window."""

    name: str
    first_bytes: int
    last_bytes: int
    delta_bytes: int


class SystemMetricsHistory(BaseModel):
    """Gauge history for the admin trend charts.

    ``bucket_seconds`` is null when points are raw snapshots and set when they
    are averages over a window - the charts say which, so a flat line from
    averaging is not mistaken for a flat line in the data.
    """

    range: str
    bucket_seconds: int | None = None
    interval_seconds: int
    snapshots: list[SystemMetricsPoint] = Field(default_factory=list)
    table_growth: list[TableGrowth] = Field(default_factory=list)
