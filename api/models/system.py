# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The heartbeat snapshot every process publishes, and the admin System view built from them.

:class:`InstanceSnapshot` is one process's account of itself; :class:`SystemStats` is the cluster as the stores describe it.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, computed_field

from api.core.leadership import LEASE_TTL_MS, STANDBY_POLL_MS
from api.core.logging import error_label
from api.core.roles import JobKind, Role
from api.models.tunnel import TunnelEndpointState

if TYPE_CHECKING:
    import asyncio

    from api.core.job_stats import JobStats
    from api.core.workers import WorkerInfo

# --- Heartbeat snapshot ------------------------------------------------------------------------


class InstanceRuntime(BaseModel):
    """The process an instance snapshot describes: what it runs, since when, and how late its event loop is."""

    version: str
    role: Role
    instance_id: str
    pid: int
    # When the process's node started; None before it has.
    started_at: datetime | None = None
    uptime_seconds: float = 0.0
    # How late the event loop ran timers over the last full minute
    # (api.core.loop_lag), worst and average, in milliseconds; None before
    # the first minute is up.
    loop_lag_max_ms: float | None = None
    loop_lag_mean_ms: float | None = None


class WorkerTask(BaseModel):
    """One background loop running in a process: its asyncio task's state, and the counters of the work inside it.

    A loop that raises every cycle and swallows the error is still
    ``running``, and only ``consecutive_failures`` says so.
    """

    name: str
    description: str = ""
    # ``instance``: every process of the loop's roles runs it; ``singleton``:
    # only the one holding ``lease``, so a standby's copy shows no runs.
    # Counters are per process and reset with it.
    scope: Literal["instance", "singleton"] = "instance"
    # Lease name for a singleton (``controller`` for the controller's loops);
    # None when every process of the loop's roles runs it.
    lease: str | None = None
    state: str  # running | done | cancelled | failed
    error: str | None = None
    # Cadence the loop was started with; null for a loop that declared none.
    interval_seconds: float | None = None
    runs: int = 0
    # Runs that found nothing to do. A subset of ``runs``: subtract to get the
    # cycles that did something. The split is what separates "the loop is
    # alive" from "the loop is busy": a gateway taking no traffic ticks its
    # request metering every 5s forever with nothing to write. Durations
    # below describe the working cycles only.
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

    @classmethod
    def of(cls, task: asyncio.Task[Any], stats: JobStats | None, info: WorkerInfo | None) -> WorkerTask:
        """The row of the loop ``task`` runs: its state from the task, cycles from ``stats``, the rest from ``info``.

        Args:
            stats: the loop's counters; None before it declared or filed any, shown as zeroes.
            info: the loop's catalogue entry; None for a loop the catalogue lacks, shown undescribed.
        """
        state, error = cls._task_state(task)
        return cls(
            name=task.get_name(),
            description=info.description if info else "",
            scope=info.scope if info else "instance",
            lease=info.lease if info else None,
            state=state,
            error=error,
            interval_seconds=stats.interval_seconds if stats else None,
            runs=stats.runs if stats else 0,
            idle_runs=stats.idle_runs if stats else 0,
            failures=stats.failures if stats else 0,
            overruns=stats.overruns if stats else 0,
            consecutive_overruns=stats.consecutive_overruns if stats else 0,
            last_overrun_at=stats.last_overrun_at if stats else None,
            consecutive_failures=stats.consecutive_failures if stats else 0,
            last_run_at=stats.last_run_at if stats else None,
            last_duration_ms=cls._round_ms(stats.last_duration_ms) if stats else None,
            avg_duration_ms=cls._round_ms(stats.avg_duration_ms) if stats else None,
            max_duration_ms=cls._round_ms(stats.max_duration_ms) if stats else None,
            last_error=stats.last_error if stats else None,
            last_error_at=stats.last_error_at if stats else None,
        )

    @staticmethod
    def _task_state(task: asyncio.Task[Any]) -> tuple[str, str | None]:
        """running, done, cancelled or failed, with the failure."""
        if not task.done():
            return "running", None
        if task.cancelled():
            return "cancelled", None
        exc = task.exception()
        if exc is not None:
            return "failed", error_label(exc)
        return "done", None

    @staticmethod
    def _round_ms(value: float | None) -> float | None:
        """Sub-microsecond precision on a timing nobody reads that closely."""
        return None if value is None else round(value, 3)


class ApiSnapshot(BaseModel):
    """What an api process, or the api runtime of ``all``, reports in its heartbeat and on ``/metrics``."""

    # The process's Postgres pool (one engine per process): connections it
    # keeps open, connections in use now, and connections opened beyond the
    # pool size. None when the pool does not say.
    pool_size: int | None = None
    pool_checked_out: int | None = None
    pool_overflow: int | None = None


class GatewaySnapshot(BaseModel):
    """What a gateway reports in its heartbeat; the Prometheus collector exports it as is.

    Built by :meth:`api.roles.gateway.GatewayRuntime.snapshot`.
    """

    # The proxy listener: the port it is bound to, and client connections being served now.
    listener_port: int
    listening: bool
    active_connections: int
    # The routing feed applied last: the head's digest, how far the change
    # stream had been read by the compile that made it (an entry ID), when
    # it was applied and how long ago; None before the first apply.
    feed_digest: str | None = None
    feed_changes: str | None = None
    feed_applied_at: datetime | None = None
    feed_age_seconds: float | None = None
    # What this gateway keeps out of routing on its own, and transfers open now.
    retired_slots: int
    local_quarantines: int
    in_flight: int
    # Requests this gateway carried since it started (counted when they end)
    # and their bytes (counted as they flow).
    requests: int
    successes: int
    failures: int
    bytes_sent: int
    bytes_received: int
    # Failed and refused requests since start, by failure kind (the failure log's kinds).
    failures_by_kind: dict[str, int]
    # Metric entries waiting for the next write to Redis, writes Redis did not
    # take, and rate-limit windows that could not be counted.
    pending_metric_deltas: int
    metric_flush_failures: int
    rate_limit_errors: int
    # Echoes preflight sent through an upstream, and verdicts that placed the exit elsewhere, since start.
    preflight_checks: int
    preflight_rejections: int
    # IP databases: the ids open, why the others are not, and the ids the gateway still waits for.
    geo_databases_loaded: list[str]
    geo_database_errors: dict[str, str]
    geo_databases_missing: list[str]
    # Buffers on their way to Redis, and what they dropped since start.
    observations_pending: int
    observations_dropped: int
    sightings_pending: int
    sightings_dropped: int
    failure_log_dropped: int
    mitm_captures_dropped: int
    # The tunnel endpoints this gateway runs, one per protocol it terminates.
    tunnels: list[TunnelEndpointState]


class ControllerSnapshot(BaseModel):
    """What one controller process reports in its heartbeat and on ``/metrics`` (``ControllerRuntime.snapshot``).

    A standby reports ``is_leader`` False and whatever it last saw while it
    led, if it ever did.
    """

    # Whether this process holds the controller lease and runs the loops.
    is_leader: bool
    # How many times this process has taken the lease since it started.
    lease_terms: int
    # How far the change stream had been read by the last compile (an entry
    # ID), and when that compile was made.
    changes: str | None = None
    compiled_at: datetime | None = None
    # Compiles since start, how long the last one took (ms), and why the
    # last one failed while none has succeeded since.
    compiles: int = 0
    compile_ms: float | None = None
    compile_error: str | None = None
    # Publishes since start, the last one's time and duration (ms), and why
    # the last one failed while none has succeeded since.
    publishes: int = 0
    published_at: datetime | None = None
    publish_ms: float | None = None
    publish_error: str | None = None
    # Sections the feed holds now, and since start the sections publishes
    # wrote (new or changed) and removed: what gateways had to fetch.
    sections: int = 0
    sections_written: int = 0
    sections_removed: int = 0
    # Proxies in quarantine, and connectors whose traffic block still runs, as published.
    quarantined: int = 0
    blocked_connectors: int = 0


class JobKindStats(BaseModel):
    """One job kind on this worker, since the process started."""

    # Batches run: each is one run of the kind's job.
    runs: int = 0
    # Batches whose run raised: delivered again, and given up after five deliveries.
    failures: int = 0
    # Seconds spent in the job, summed over the batches that ran.
    duration_seconds: float = 0.0
    # Entries this worker holds now, in the batches it is running or giving up.
    in_flight: int = 0


class WorkerSnapshot(BaseModel):
    """What a worker process reports in its heartbeat and on ``/metrics``.

    Built by :meth:`api.roles.worker.WorkerRuntime.snapshot`.
    """

    # The job kinds this worker takes, each with its counters.
    jobs: dict[JobKind, JobKindStats]
    # Preflight sightings attributed, and lost with a batch given up after
    # repeated deliveries, since start.
    sightings_consumed: int = 0
    sightings_dropped: int = 0
    # IP observations and exit judgements ingested, and lost with a batch
    # given up after repeated deliveries, since start.
    observations_consumed: int = 0
    observations_dropped: int = 0


class StreamState(BaseModel):
    """How far one process has read one Redis stream it follows, and how often it read in full instead.

    A follower reads on from its position in memory, and reads in full at
    start, after a gap or a failed read, and once per ``reconcile_interval``.
    """

    # How far the follower has handled the stream, as an entry ID
    # (``<milliseconds>-<sequence>``): the last entry it read, or the newest
    # one at its last full read (``0-0`` when there was no stream); None
    # before its first read.
    position: str | None = None
    # How far behind the stream's newest entry the follower's last read found
    # it, in seconds: from the oldest entry that read returned to the newest
    # entry, by the milliseconds both IDs carry, so no two clocks are
    # compared and a quiet stream's idle hours never count. 0 when the read
    # found nothing new or was a full read; None before its first read.
    lag_seconds: float | None = None
    # Entries read since the process started.
    entries: int = 0
    # Full reads since the process started, by reason (``start``, ``gap``,
    # ``reconnect``, ``reconcile``), and when the last one was made.
    full_reads: dict[str, int] = Field(default_factory=dict)
    last_full_read_at: datetime | None = None


class InstanceSnapshot(BaseModel):
    """What one process publishes about itself on its heartbeat, and what its ``/metrics`` renders.

    Built from memory every :data:`api.db.redis.INSTANCE_HEARTBEAT_INTERVAL`
    seconds: the process, its loops, its readiness, the streams it follows,
    and a section per runtime it runs.
    """

    runtime: InstanceRuntime
    # Whether the process answers /ready with 200 now, and when not, why.
    ready: bool = False
    reasons: list[str] = Field(default_factory=list)
    tasks: list[WorkerTask] = Field(default_factory=list)
    # Each Redis stream the process follows, by name (``changes``,
    # ``runtime``, ``feed``).
    streams: dict[str, StreamState] = Field(default_factory=dict)
    api: ApiSnapshot | None = None
    gateway: GatewaySnapshot | None = None
    controller: ControllerSnapshot | None = None
    worker: WorkerSnapshot | None = None


# --- System view: the cluster ------------------------------------------------------------------


class InstanceInfo(BaseModel):
    """One live process, as its heartbeat describes it: listed while its registry key, which holds its role, has not expired.

    ``snapshot`` is None when the process could not build one or this
    release cannot read it (another version during an upgrade).
    """

    instance_id: str
    # The role the process registered with (``api.core.roles.Role``).
    role: str
    snapshot: InstanceSnapshot | None = None
    # How long ago the snapshot was published, read off its key's remaining
    # TTL so no two hosts' clocks are compared; None without one.
    age_seconds: float | None = None


class ControllerStatus(BaseModel):
    """The active controller: who holds the lease, the compile it last made, and how soon a standby takes the lease over."""

    # The instance id holding the controller lease; None while no controller
    # leads (none is running, or a failover is under way).
    leader: str | None = None
    # How far the change stream had been read by the leader's last compile
    # (an entry ID), and when that compile was made, from its heartbeat;
    # None until it reports one.
    changes: str | None = None
    compiled_at: datetime | None = None
    # The change stream's newest entry ID (its ``last-generated-id``), read
    # now. Ahead of ``changes`` while the leader has changes it has not
    # compiled yet, or its heartbeat has not reported the compile yet; None
    # while there is no stream (nothing announced since Redis started empty)
    # or it could not be read.
    changes_head: str | None = None
    # How soon a standby takes the lease over, in milliseconds: it asks for
    # the lease every ``standby_poll_ms``, and a leader that fails holds it
    # for at most ``lease_ttl_ms`` more. The values every controller of this
    # release runs with, for the System page to word the takeover from.
    standby_poll_ms: int = STANDBY_POLL_MS
    lease_ttl_ms: int = LEASE_TTL_MS
    # Why the lease and the heartbeats could not be read from Redis.
    error: str | None = None


class GatewayFeedStatus(BaseModel):
    """One gateway's routing feed against the head in Redis."""

    instance_id: str
    # The digest of the feed the gateway applied last, and how far the
    # change stream had been read by the compile that made it (an entry ID);
    # None before its first apply.
    digest: str | None = None
    changes: str | None = None
    # Whether the gateway routes on the feed in Redis now. False while it is
    # behind: after every publish until its next heartbeat reports the
    # apply, or for as long as it cannot apply one (its snapshot's
    # ``reasons`` say why).
    in_sync: bool = False
    # Seconds since the gateway applied a feed update, as of this answer; it
    # grows while nothing changes. None before its first apply.
    age_seconds: float | None = None


class FeedStatus(BaseModel):
    """The routing feed: the head the active controller published, and every gateway against it."""

    # The head in Redis (``o6:feed:head``): the digest over every section,
    # how far the change stream had been read by the compile that last
    # changed the feed (an entry ID) and when it was made, and how many
    # sections it holds. None while there is no head (no controller has
    # published yet, or Redis lost it and the controller has not
    # republished it yet).
    digest: str | None = None
    changes: str | None = None
    compiled_at: datetime | None = None
    sections: int | None = None
    # Every live gateway (a process of role ``gateway`` or ``all``), by instance id.
    gateways: list[GatewayFeedStatus] = Field(default_factory=list)
    # Why the head or the gateways' heartbeats could not be read.
    error: str | None = None


class WorkStream(BaseModel):
    """One work stream and the workers' consumer group on it: the backlog, the work in hand, and the workers taking it."""

    # Entries on the stream: queued and running, since a worker deletes each
    # entry once it has handled it.
    length: int = 0
    # Entries delivered to a worker and not acknowledged yet: running, or waiting
    # to be reclaimed (a worker stopped, a batch failed, or its acknowledgement was refused).
    pending: int = 0
    # Worker processes in the group, the stopped ones too until index hygiene
    # prunes them (within the hour).
    consumers: int = 0
    # Age of the oldest entry in seconds, by Redis's clock; None when the stream is empty.
    oldest_seconds: float | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def queued(self) -> int:
        """Entries no worker has taken yet, the backlog; never below zero, since the cap can trim entries still pending."""
        return max(0, self.length - self.pending)


class WorkStreams(BaseModel):
    """Every work stream by name (``jobs.<kind>``, ``sightings``, ``observations``), as Redis describes it now."""

    # Empty when Redis could not be read (``error`` says why).
    by_name: dict[str, WorkStream] = Field(default_factory=dict)
    error: str | None = None


# --- System view: the stores -------------------------------------------------------------------


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
    # Proxies per status (``ProxyStatus`` values), from the ``proxies.status``
    # column the health jobs and the drains write; a status no proxy holds
    # is absent.
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
    # Deleted or replaced tuples no vacuum has reclaimed yet, TOAST chunks included.
    dead_rows: int = 0
    last_vacuum_at: datetime | None = None


class DatabaseStats(BaseModel):
    """Postgres size and connections; each api process's own connection pool is in its snapshot (:class:`ApiSnapshot`).

    A table's ``row_estimate`` is the planner's (``pg_class.reltuples``):
    free on tables of millions of rows, and drifting until the next ANALYZE.
    """

    name: str = ""
    size_bytes: int = 0
    tables: list[TableStats] = Field(default_factory=list)
    # Connections to this database, from every process and client.
    backends: int | None = None
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


class SystemStats(BaseModel):
    """Everything the admin System view renders: the cluster and the stores, the same from every api process."""

    generated_at: datetime
    # Every live process, by the role it registered with (``all``, ``api``,
    # ``gateway``, ``controller``, ``worker`` in that order, then any other),
    # each list sorted by instance id. A role with no live process is absent;
    # echo processes do not heartbeat. Empty when Redis could not be read
    # (``controller.error`` says why).
    instances: dict[str, list[InstanceInfo]] = Field(default_factory=dict)
    controller: ControllerStatus
    feed: FeedStatus
    # The work streams: each job kind's, the sightings and the observations.
    streams: WorkStreams = Field(default_factory=WorkStreams)
    inventory: InventoryStats
    projects: list[ProjectUsage] = Field(default_factory=list)
    database: DatabaseStats
    redis: RedisStats


# --- Trend history -----------------------------------------------------------------------------


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
