# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Collectors behind the admin system view.

Each section is gathered independently and every collector is failure-tolerant:
a Redis outage or a Postgres role without ``pg_database_size`` rights degrades
that one card to an ``error`` string instead of failing the whole request - an
admin looking at a partly broken instance is exactly who needs this page.

Cost notes, since this runs on a polling UI:

* Entity counts are exact ``count(*)`` over the small operational tables.
* Table sizes use catalog metadata and ``pg_class.reltuples``, so the
  metric tables (millions of rows) stay cheap at the price of an estimate
  that is unknown until autovacuum first analyses the table.
* The Redis keyspace breakdown is a bounded ``SCAN``; past
  :data:`REDIS_SCAN_LIMIT` keys it reports a sample and sets ``truncated``.
"""

from __future__ import annotations

import asyncio
import os
import platform
import sys
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from api import __version__
from api.core import utc_now
from api.core.config import Settings
from api.core.job_stats import job_stats
from api.core.workers import LEASE_KINDS, LEASE_WORKERS, worker_info
from api.db.redis import INSTANCE_REGISTRY_SCAN, LEASE_SCAN, RedisClient, classify_key
from api.models.system import (
    CacheStats,
    DatabaseStats,
    InstanceInfo,
    InventoryStats,
    LeaseInfo,
    ProjectUsage,
    RedisKeyGroup,
    RedisStats,
    RuntimeStats,
    SystemStats,
    TableStats,
    WorkerStats,
    WorkerTask,
)

if TYPE_CHECKING:
    from api.core.geo_lookup import GeoLookup
    from api.core.proxy_manager import ProxyManager
    from api.core.proxy_server import ProxyServer
    from api.core.tls_cert_manager import TLSCertManager

logger = structlog.get_logger()

# Stop counting keys after this many so one admin page view cannot walk a
# multi-million-key keyspace. The result is flagged as truncated instead.
REDIS_SCAN_LIMIT = 50_000
REDIS_SCAN_BATCH = 1_000

# Largest number of per-project rows returned; the UI shows the busiest ones.
MAX_PROJECT_ROWS = 50



# --- runtime ------------------------------------------------------------------------


def collect_runtime(
    settings: Settings, started_at: datetime | None, proxy_port: int | None = None
) -> RuntimeStats:
    """Describe the process that is serving this request.

    ``proxy_port`` is the port the listener actually bound, which is the
    interesting one when the configuration asked for 0 (OS-assigned).
    """
    uptime = (utc_now() - started_at).total_seconds() if started_at else 0.0
    return RuntimeStats(
        version=__version__,
        instance_id=settings.instance_id,
        role=settings.role,
        environment=settings.env,
        python_version=sys.version.split()[0],
        platform=f"{platform.system()} {platform.machine()}",
        pid=os.getpid(),
        started_at=started_at,
        uptime_seconds=round(uptime, 1),
        api_port=settings.api_port,
        proxy_port=proxy_port if proxy_port is not None else settings.proxy_port,
        log_level=settings.log_level.upper(),
        health_check_interval=settings.health_check_interval,
        metrics_flush_interval=settings.metrics_flush_interval,
        ip_refresh_interval=settings.ip_refresh_interval,
    )


# --- postgres -----------------------------------------------------------------------

_INVENTORY_SQL = text(
    """
    SELECT
        (SELECT count(*) FROM projects)                              AS projects,
        (SELECT count(*) FROM credentials)                           AS credentials,
        (SELECT count(*) FROM connectors)                            AS connectors,
        (SELECT count(*) FROM connectors WHERE enabled)              AS connectors_enabled,
        (SELECT count(*) FROM connectors WHERE consecutive_errors > 0) AS connectors_failing,
        (SELECT count(*) FROM proxies)                               AS proxies,
        (SELECT count(*) FROM users)                                 AS users,
        (SELECT count(*) FROM users WHERE is_active)                 AS users_active,
        (SELECT count(*) FROM provider_descriptors)                  AS providers_custom,
        (SELECT count(*) FROM provider_descriptors WHERE enabled)    AS providers_custom_enabled
    """
)

_USER_ROLES_SQL = text("SELECT role, count(*) FROM users GROUP BY role")

# count(DISTINCT ...) because joining credentials and connectors off the same
# project multiplies the proxy rows.
_PROJECT_USAGE_SQL = text(
    """
    SELECT p.id,
           p.name,
           count(DISTINCT cr.id) AS credentials,
           count(DISTINCT c.id)  AS connectors,
           count(DISTINCT px.id) AS proxies
    FROM projects p
    LEFT JOIN credentials cr ON cr.project_id = p.id
    LEFT JOIN connectors  c  ON c.project_id  = p.id
    LEFT JOIN proxies     px ON px.connector_id = c.id
    GROUP BY p.id, p.name
    ORDER BY count(DISTINCT px.id) DESC, p.name ASC
    LIMIT :limit
    """
)

_TABLE_SIZES_SQL = text(
    """
    SELECT c.relname AS name,
           c.reltuples::bigint              AS row_estimate,
           pg_total_relation_size(c.oid)    AS total_bytes,
           pg_table_size(c.oid)             AS table_bytes,
           pg_indexes_size(c.oid)           AS index_bytes
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'r' AND n.nspname = current_schema()
    ORDER BY pg_total_relation_size(c.oid) DESC
    """
)

_DB_SIZE_SQL = text(
    """
    SELECT current_database()                       AS name,
           pg_database_size(current_database())     AS size_bytes,
           (SELECT count(*) FROM pg_stat_activity
             WHERE datname = current_database())    AS backends
    """
)


async def collect_inventory(
    session: AsyncSession, proxy_manager: ProxyManager | None
) -> InventoryStats:
    """Exact entity counts from Postgres, plus live health from the pool."""
    row = (await session.execute(_INVENTORY_SQL)).one()
    roles = {str(r[0]): int(r[1]) for r in (await session.execute(_USER_ROLES_SQL)).all()}

    statuses: Counter[str] = Counter()
    builtin = 0
    if proxy_manager is not None:
        for proxy in proxy_manager.proxies:
            statuses[proxy.status.value] += 1
        builtin = sum(
            1 for ptype in proxy_manager.provider_registry.list() if ptype.source != "custom"
        )
    custom = int(row.providers_custom)

    return InventoryStats(
        projects=int(row.projects),
        credentials=int(row.credentials),
        connectors=int(row.connectors),
        connectors_enabled=int(row.connectors_enabled),
        connectors_failing=int(row.connectors_failing),
        proxies=int(row.proxies),
        users=int(row.users),
        users_active=int(row.users_active),
        users_by_role=roles,
        providers_total=builtin + custom,
        providers_builtin=builtin,
        providers_custom=custom,
        custom_providers_enabled=int(row.providers_custom_enabled),
        proxies_by_status=dict(sorted(statuses.items())),
    )


async def collect_project_usage(session: AsyncSession) -> list[ProjectUsage]:
    """Per-project inventory, busiest first."""
    rows = (await session.execute(_PROJECT_USAGE_SQL, {"limit": MAX_PROJECT_ROWS})).all()
    return [
        ProjectUsage(
            id=str(r[0]),
            name=str(r[1]),
            credentials=int(r[2]),
            connectors=int(r[3]),
            proxies=int(r[4]),
        )
        for r in rows
    ]


async def collect_database(session: AsyncSession) -> DatabaseStats:
    """Database and per-table sizes, with connection-pool usage for this process."""
    stats = DatabaseStats()
    try:
        row = (await session.execute(_DB_SIZE_SQL)).one()
        stats.name = str(row.name)
        stats.size_bytes = int(row.size_bytes)
        stats.backends = int(row.backends)
        stats.tables = [
            TableStats(
                name=str(r[0]),
                # Postgres stores -1 for a relation that has never been analysed.
                row_estimate=int(r[1]) if int(r[1]) >= 0 else None,
                total_bytes=int(r[2]),
                table_bytes=int(r[3]),
                index_bytes=int(r[4]),
            )
            for r in (await session.execute(_TABLE_SIZES_SQL)).all()
        ]
    except Exception as exc:  # pragma: no cover - depends on grants
        logger.warning("Database stats unavailable", error=str(exc))
        stats.error = str(exc)

    pool = getattr(session.get_bind(), "pool", None)
    try:
        if pool is not None:
            stats.pool_size = int(pool.size())
            stats.pool_checked_out = int(pool.checkedout())
    except Exception:  # pragma: no cover - pool class without size()/checkedout()
        logger.debug("Connection pool does not expose sizing", exc_info=True)

    return stats


# --- redis --------------------------------------------------------------------------


async def collect_redis(redis_client: RedisClient, *, scan_keyspace: bool = True) -> RedisStats:
    """Redis memory, throughput and a bounded breakdown of the keyspace.

    Everything but the breakdown comes from ``INFO`` and ``DBSIZE``, which are
    O(1). Pass ``scan_keyspace=False`` to skip the ``SCAN`` - the periodic
    snapshotter only records memory and key count, and walking the keyspace
    every few minutes to derive numbers it discards would not pay for itself.
    """
    stats = RedisStats()
    try:
        client = redis_client.client
        info: dict[str, Any] = await client.info()
        stats.version = str(info.get("redis_version", ""))
        stats.uptime_seconds = int(info.get("uptime_in_seconds", 0))
        stats.used_memory_bytes = int(info.get("used_memory", 0))
        stats.used_memory_peak_bytes = int(info.get("used_memory_peak", 0))
        stats.used_memory_rss_bytes = int(info.get("used_memory_rss", 0))
        stats.maxmemory_bytes = int(info.get("maxmemory", 0))
        stats.connected_clients = int(info.get("connected_clients", 0))
        stats.ops_per_sec = int(info.get("instantaneous_ops_per_sec", 0))
        stats.keyspace_hits = int(info.get("keyspace_hits", 0))
        stats.keyspace_misses = int(info.get("keyspace_misses", 0))
        stats.total_keys = int(await client.dbsize())

        if not scan_keyspace:
            return stats

        counts: Counter[str] = Counter()
        scanned = 0
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor=cursor, count=REDIS_SCAN_BATCH)
            for key in keys:
                counts[classify_key(key if isinstance(key, str) else key.decode())] += 1
            scanned += len(keys)
            if cursor == 0 or scanned >= REDIS_SCAN_LIMIT:
                break
        stats.scanned_keys = scanned
        stats.truncated = cursor != 0
        stats.groups = [
            RedisKeyGroup(label=label, keys=n)
            for label, n in sorted(counts.items(), key=lambda kv: -kv[1])
        ]
    except Exception as exc:
        logger.warning("Redis stats unavailable", error=str(exc))
        stats.error = str(exc)
    return stats


async def _collect_leases(redis_client: RedisClient, instance_id: str) -> list[LeaseInfo]:
    """Who currently holds each singleton-worker lease, cluster-wide."""
    client = redis_client.client
    keys = [k if isinstance(k, str) else k.decode() async for k in client.scan_iter(match=LEASE_SCAN)]
    if not keys:
        return []
    pipe = client.pipeline()
    for key in keys:
        pipe.get(key)
        pipe.pttl(key)
    results = await pipe.execute()

    leases: list[LeaseInfo] = []
    for i, key in enumerate(keys):
        holder, ttl = results[2 * i], results[2 * i + 1]
        if holder is None:
            continue  # expired between the scan and the read
        name = key.split(":", 1)[1] if ":" in key else key
        kind, _, target = name.partition(":")
        leases.append(
            LeaseInfo(
                name=name,
                kind=LEASE_KINDS.get(kind, kind),
                worker=LEASE_WORKERS.get(kind, ""),
                target=target or None,
                holder=str(holder),
                held_by_self=str(holder) == instance_id,
                ttl_ms=max(0, int(ttl)),
            )
        )
    return sorted(leases, key=lambda lease: (lease.kind, lease.target or ""))


async def _collect_instances(redis_client: RedisClient, instance_id: str) -> list[InstanceInfo]:
    """Every Octoprox process currently advertising itself in the registry."""
    client = redis_client.client
    keys = [
        k if isinstance(k, str) else k.decode()
        async for k in client.scan_iter(match=INSTANCE_REGISTRY_SCAN)
    ]
    if not keys:
        return []
    pipe = client.pipeline()
    for key in keys:
        pipe.get(key)
        pipe.ttl(key)
    results = await pipe.execute()

    instances: list[InstanceInfo] = []
    for i, key in enumerate(keys):
        role, ttl = results[2 * i], results[2 * i + 1]
        if role is None:
            continue
        ident = key.partition(":")[2]
        instances.append(
            InstanceInfo(
                instance_id=ident,
                role=str(role),
                is_self=ident == instance_id,
                ttl_seconds=max(0, int(ttl)),
            )
        )
    # This instance first, then stable by id so the list does not jump around.
    return sorted(instances, key=lambda inst: (not inst.is_self, inst.instance_id))


# --- process-local state ------------------------------------------------------------


def collect_cache(
    proxy_manager: ProxyManager | None, cert_manager: TLSCertManager | None
) -> CacheStats:
    """Sizes of the caches this instance keeps in memory."""
    if proxy_manager is None:
        return CacheStats()
    sizes = proxy_manager.cache_sizes()
    return CacheStats(
        **sizes,
        tls_contexts=cert_manager.cached_contexts if cert_manager else 0,
        provider_types=len(proxy_manager.provider_registry.list()),
    )


def _task_state(task: asyncio.Task[Any]) -> tuple[str, str | None]:
    if not task.done():
        return "running", None
    if task.cancelled():
        return "cancelled", None
    exc = task.exception()
    if exc is not None:
        return "failed", f"{type(exc).__name__}: {exc}"
    return "done", None


def collect_tasks(proxy_manager: ProxyManager | None) -> list[WorkerTask]:
    """The background loops of this process: alive, elected, and doing work.

    ``state`` comes from the asyncio task, the counters from
    :mod:`api.core.job_stats`. Both are needed: the task says the loop exists,
    the counters say the cycles inside it are completing. A singleton worker
    standing by without the lease is legitimately at zero runs - the lease
    list says who does have it.
    """
    if proxy_manager is None:
        return []
    runs = job_stats.snapshot()
    tasks: list[WorkerTask] = []
    for task in proxy_manager.background_tasks:
        name = task.get_name()
        state, error = _task_state(task)
        info = worker_info(name)
        stats = runs.get(name)
        tasks.append(
            WorkerTask(
                name=name,
                description=info.description if info else "",
                scope=info.scope if info else "instance",
                lease=info.lease if info else None,
                state=state,
                error=error,
                runs=stats.runs if stats else 0,
                failures=stats.failures if stats else 0,
                consecutive_failures=stats.consecutive_failures if stats else 0,
                last_run_at=stats.last_run_at if stats else None,
                last_duration_ms=_round_ms(stats.last_duration_ms) if stats else None,
                avg_duration_ms=_round_ms(stats.avg_duration_ms) if stats else None,
                max_duration_ms=_round_ms(stats.max_duration_ms) if stats else None,
                last_error=stats.last_error if stats else None,
                last_error_at=stats.last_error_at if stats else None,
            )
        )
    return tasks


def _round_ms(value: float | None) -> float | None:
    """Sub-microsecond precision on a timing nobody reads that closely."""
    return None if value is None else round(value, 3)


async def collect_workers(
    proxy_manager: ProxyManager | None,
    proxy_server: ProxyServer | None,
    geo_lookup: GeoLookup | None,
    redis_client: RedisClient | None,
    instance_id: str,
) -> WorkerStats:
    """Local task health plus the cluster-wide lease and membership picture."""
    leases: list[LeaseInfo] = []
    instances: list[InstanceInfo] = []
    if redis_client is not None:
        try:
            leases, instances = await asyncio.gather(
                _collect_leases(redis_client, instance_id),
                _collect_instances(redis_client, instance_id),
            )
        except Exception as exc:
            logger.warning("Worker lease/instance scan failed", error=str(exc))

    return WorkerStats(
        tasks=collect_tasks(proxy_manager),
        leases=leases,
        instances=instances,
        proxy_server_listening=proxy_server.is_listening if proxy_server else False,
        proxy_server_connections=proxy_server.active_connections if proxy_server else 0,
        geo_lookup_enabled=geo_lookup.enabled if geo_lookup else False,
        geo_lookups_in_flight=geo_lookup.in_flight if geo_lookup else 0,
    )


# --- entry point --------------------------------------------------------------------


async def collect_system_stats(
    session: AsyncSession,
    settings: Settings,
    *,
    proxy_manager: ProxyManager | None = None,
    proxy_server: ProxyServer | None = None,
    geo_lookup: GeoLookup | None = None,
    cert_manager: TLSCertManager | None = None,
    redis_client: RedisClient | None = None,
    started_at: datetime | None = None,
) -> SystemStats:
    """Gather every section of the admin system view.

    The Postgres queries share one session so they run in sequence; the Redis
    work happens alongside them.
    """

    async def _postgres() -> tuple[InventoryStats, list[ProjectUsage], DatabaseStats]:
        inventory = await collect_inventory(session, proxy_manager)
        projects = await collect_project_usage(session)
        database = await collect_database(session)
        return inventory, projects, database

    async def _redis() -> RedisStats:
        return await collect_redis(redis_client) if redis_client else RedisStats()

    (inventory, projects, database), redis_stats, workers = await asyncio.gather(
        _postgres(),
        _redis(),
        collect_workers(proxy_manager, proxy_server, geo_lookup, redis_client, settings.instance_id),
    )

    return SystemStats(
        generated_at=utc_now(),
        runtime=collect_runtime(
            settings, started_at, proxy_port=proxy_server.port if proxy_server else None
        ),
        inventory=inventory,
        projects=projects,
        database=database,
        redis=redis_stats,
        cache=collect_cache(proxy_manager, cert_manager),
        workers=workers,
    )
