# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Collectors behind the admin System view: the cluster and the stores as Postgres and Redis describe them.

The Redis sections and the database sizes each carry an ``error`` rather than fail the page, and nothing comes from
the answering process's memory.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from itertools import batched
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError
from sqlalchemy import text

from api.core import utc_now
from api.core.logging import error_text
from api.core.roles import Role
from api.core.workers import LeaseName
from api.db.redis import (
    CHANGES_STREAM_KEY,
    FEED_HEAD_KEY,
    INSTANCE_INDEX_KEY,
    INSTANCE_REGISTRY_KEY,
    INSTANCE_STATS_KEY,
    INSTANCE_TTL_SECONDS,
    LEASE_KEY,
    WORK_STREAMS,
    WORKER_GROUP,
    StreamId,
    classify_key,
    is_missing_key,
)
from api.db.repository import ProxyRepository
from api.feed.models import FeedHead
from api.models.system import (
    ControllerStatus,
    DatabaseStats,
    FeedStatus,
    GatewayFeedStatus,
    GatewaySnapshot,
    InstanceInfo,
    InstanceSnapshot,
    InventoryStats,
    ProjectUsage,
    RedisKeyGroup,
    RedisStats,
    SystemStats,
    SystemTopology,
    TableStats,
    WorkStream,
    WorkStreams,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.redis import RedisClient
    from api.providers.registry import ProviderRegistry

logger = structlog.get_logger()

# Stop counting keys after this many so one admin page view cannot walk a
# multi-million-key keyspace. The result is flagged as truncated instead.
REDIS_SCAN_LIMIT = 50_000
REDIS_SCAN_BATCH = 1_000

# Largest number of per-project rows returned; the UI shows the busiest ones.
MAX_PROJECT_ROWS = 50


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

# Dead tuples and vacuum times of the table and its TOAST relation together,
# since large values live in the latter and autovacuum visits the two separately.
_TABLE_SIZES_SQL = text(
    """
    SELECT c.relname AS name,
           c.reltuples::bigint              AS row_estimate,
           pg_total_relation_size(c.oid)    AS total_bytes,
           pg_table_size(c.oid)             AS table_bytes,
           pg_indexes_size(c.oid)           AS index_bytes,
           coalesce(s.n_dead_tup, 0) + coalesce(t.n_dead_tup, 0) AS dead_rows,
           greatest(s.last_vacuum, s.last_autovacuum,
                    t.last_vacuum, t.last_autovacuum)            AS last_vacuum_at
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid
    LEFT JOIN pg_stat_all_tables t ON t.relid = c.reltoastrelid
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


# --- the cluster ---------------------------------------------------------------------


@dataclass(frozen=True)
class ClusterReading:
    """What Redis holds about the cluster's processes and their coordination, as one read found it."""

    # Every listed process whose registry key is alive, sorted by instance id.
    instances: list[InstanceInfo] = field(default_factory=list)
    # Who holds the controller lease.
    leader: str | None = None
    # The routing feed's head, and why it could not be read when it is there.
    head: FeedHead | None = None
    head_error: str | None = None
    # The changes stream's newest entry ID; None while there is no stream.
    changes_head: str | None = None
    # Why nothing could be read: Redis did not answer.
    error: str | None = None

    def by_role(self) -> dict[str, list[InstanceInfo]]:
        """The processes by the role they registered with: roles in :class:`Role` order (any other last), ids sorted."""
        rank = {str(role): position for position, role in enumerate(Role)}
        groups: dict[str, list[InstanceInfo]] = {}
        for instance in sorted(self.instances, key=lambda i: (rank.get(i.role, len(rank)), i.role, i.instance_id)):
            groups.setdefault(instance.role, []).append(instance)
        return groups

    def controller_status(self) -> ControllerStatus:
        """The lease holder and, from its heartbeat, how far its last compile read the changes stream, against its head."""
        leader = next(
            (
                instance.snapshot.controller
                for instance in self.instances
                if instance.instance_id == self.leader
                and instance.snapshot is not None
                and instance.snapshot.controller is not None
            ),
            None,
        )
        return ControllerStatus(
            leader=self.leader,
            changes=leader.changes if leader is not None else None,
            compiled_at=leader.compiled_at if leader is not None else None,
            changes_head=self.changes_head,
            error=self.error,
        )

    def feed_status(self) -> FeedStatus:
        """The head in Redis and every live gateway against it."""
        head = self.head
        gateways = [
            self._gateway_feed(instance.instance_id, instance.snapshot.gateway, instance.age_seconds, head)
            for instance in self.instances
            if instance.snapshot is not None and instance.snapshot.gateway is not None
        ]
        return FeedStatus(
            digest=head.digest if head is not None else None,
            changes=head.changes if head is not None else None,
            compiled_at=head.compiled_at if head is not None else None,
            sections=head.sections if head is not None else None,
            gateways=gateways,
            error=self.error or self.head_error,
        )

    @staticmethod
    def _gateway_feed(
        instance_id: str, gateway: GatewaySnapshot, snapshot_age: float | None, head: FeedHead | None
    ) -> GatewayFeedStatus:
        """One gateway's applied feed against the head; its age carried forward by its snapshot's own age."""
        age = None
        if gateway.feed_age_seconds is not None:
            age = round(gateway.feed_age_seconds + (snapshot_age or 0.0), 1)
        return GatewayFeedStatus(
            instance_id=instance_id,
            digest=gateway.feed_digest,
            changes=gateway.feed_changes,
            in_sync=head is not None and gateway.feed_digest == head.digest,
            age_seconds=age,
        )


class ClusterReader:
    """Reads what Redis holds about the cluster: heartbeats, the controller lease, the feed head, the work streams."""

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis_client = redis_client

    async def read(self) -> ClusterReading:
        """Every live process's heartbeat, the controller lease, the feed head and the changes stream's head, in three round trips.

        A process whose registry key has expired is left out, and one whose snapshot is missing or unreadable is listed
        without it.
        """
        try:
            pipe = self._redis_client.client.pipeline(transaction=False)
            pipe.smembers(INSTANCE_INDEX_KEY)
            pipe.get(LEASE_KEY.format(name=LeaseName.CONTROLLER))
            pipe.get(FEED_HEAD_KEY)
            members, leader, raw_head = await pipe.execute()
            changes_head = await self._redis_client.stream_head(CHANGES_STREAM_KEY)
            instances = await self._read_instances(sorted(str(member) for member in members))
        except Exception as exc:
            logger.warning("Cluster state unavailable", error=error_text(exc))
            return ClusterReading(error=error_text(exc))
        head, head_error = self._parse_head(raw_head)
        return ClusterReading(
            instances=instances,
            leader=str(leader) if leader is not None else None,
            head=head,
            head_error=head_error,
            changes_head=changes_head,
        )

    async def instances(self) -> list[InstanceInfo]:
        """Every live process's heartbeat, sorted by id: its role and, when this release can read one, its snapshot.

        Every reader of the heartbeats goes through it, so all agree on who is alive; two round trips whatever the
        number of processes, raising when Redis cannot be read.
        """
        members = await self._redis_client.client.smembers(INSTANCE_INDEX_KEY)
        return await self._read_instances(sorted(str(member) for member in members))

    async def streams(self) -> WorkStreams:
        """Each work stream's length, the workers' group on it and its oldest entry's age, in one pipeline with Redis's clock.

        A missing stream or group reads as zeros; any other failure leaves the section empty, with its ``error``.
        """
        try:
            pipe = self._redis_client.client.pipeline(transaction=False)
            for key in WORK_STREAMS.values():
                pipe.xlen(key)
                pipe.xinfo_groups(key)
                pipe.xrange(key, count=1)
            pipe.time()
            *replies, clock = await pipe.execute(raise_on_error=False)
            seconds, microseconds = self._checked(clock)
            now_ms = int(seconds) * 1000 + int(microseconds) // 1000
            by_name = {
                name: self._work_stream(length, groups, first, now_ms=now_ms)
                for name, (length, groups, first) in zip(WORK_STREAMS, batched(replies, 3), strict=True)
            }
        except Exception as exc:
            logger.warning("Work streams unavailable", error=error_text(exc))
            return WorkStreams(error=error_text(exc))
        return WorkStreams(by_name=by_name)

    async def topology(self) -> SystemTopology:
        """The cluster as one answer: :meth:`read` and :meth:`streams` side by side, each failure kept in its section."""
        reading, streams = await asyncio.gather(self.read(), self.streams())
        return SystemTopology(
            generated_at=utc_now(),
            instances=reading.by_role(),
            controller=reading.controller_status(),
            feed=reading.feed_status(),
            streams=streams,
        )

    async def _read_instances(self, members: list[str]) -> list[InstanceInfo]:
        """The listed processes whose registry key is alive, with their snapshots; one pipeline."""
        if not members:
            return []
        pipe = self._redis_client.client.pipeline(transaction=False)
        for member in members:
            pipe.get(INSTANCE_REGISTRY_KEY.format(instance_id=member))
            pipe.get(INSTANCE_STATS_KEY.format(instance_id=member))
            pipe.pttl(INSTANCE_STATS_KEY.format(instance_id=member))
        results = await pipe.execute()
        instances: list[InstanceInfo] = []
        for i, member in enumerate(members):
            role, raw_snapshot, ttl_ms = results[3 * i : 3 * i + 3]
            if role is None:
                continue  # expired since it was listed
            snapshot = self._parse_snapshot(member, raw_snapshot)
            instances.append(
                InstanceInfo(
                    instance_id=member,
                    role=str(role),
                    snapshot=snapshot,
                    age_seconds=self._snapshot_age(int(ttl_ms)) if snapshot is not None else None,
                )
            )
        return instances

    @staticmethod
    def _parse_snapshot(instance_id: str, raw: str | None) -> InstanceSnapshot | None:
        """A process's published snapshot, or None when it published none or this release cannot read it.

        An unreadable payload costs that process its details, not the page, and is logged at debug: the page says so on
        every poll.
        """
        if raw is None:
            return None
        try:
            return InstanceSnapshot.model_validate_json(raw)
        except ValidationError as exc:
            logger.debug("Unreadable instance snapshot", peer=instance_id, errors=exc.error_count())
            return None

    @staticmethod
    def _snapshot_age(ttl_ms: int) -> float | None:
        """How long ago a snapshot was written, read off the TTL it was written with; None for a key without one.

        No clock comparison, so host skew cannot make a fresh snapshot look stale.
        """
        if ttl_ms < 0:
            return None
        return round(max(0.0, INSTANCE_TTL_SECONDS - ttl_ms / 1000), 1)

    @staticmethod
    def _parse_head(raw: str | None) -> tuple[FeedHead | None, str | None]:
        """The feed head and, when one is there that this release cannot read, why not."""
        if raw is None:
            return None, None
        try:
            return FeedHead.model_validate_json(raw), None
        except ValidationError as exc:
            logger.debug("Unreadable routing feed head", errors=exc.error_count())
            return None, "The routing feed head in Redis cannot be read by this release"

    @classmethod
    def _work_stream(cls, length: Any, groups: Any, first: Any, *, now_ms: int) -> WorkStream:
        """One stream from its ``XLEN``, ``XINFO GROUPS`` and first-entry replies, its age against ``now_ms`` on Redis's clock."""
        if is_missing_key(groups):
            groups = []  # no stream: its length reads 0 and it has no entry
        group = next((info for info in cls._checked(groups) if info["name"] == WORKER_GROUP), None)
        entries = cls._checked(first)
        oldest = max(0.0, (now_ms - StreamId.of(str(entries[0][0])).ms) / 1000) if entries else None
        return WorkStream(
            length=int(cls._checked(length)),
            pending=int(group["pending"]) if group is not None else 0,
            consumers=int(group["consumers"]) if group is not None else 0,
            oldest_seconds=oldest,
        )

    @staticmethod
    def _checked(reply: Any) -> Any:
        """A pipelined reply read with ``raise_on_error=False``; the error Redis answered with is raised."""
        if isinstance(reply, Exception):
            raise reply
        return reply


# --- the System view -----------------------------------------------------------------


class SystemReader:
    """Reads the admin System view: the inventory and the stores from Postgres and Redis, and the cluster from Redis.

    Args:
        registry: knows the provider types not stored as rows (built-in, file and plugin ones); without it they count as none.
    """

    def __init__(
        self, session: AsyncSession, redis_client: RedisClient, registry: ProviderRegistry | None = None
    ) -> None:
        self._session = session
        self._redis_client = redis_client
        self._registry = registry

    async def read(self) -> SystemStats:
        """Every section of the view: the Postgres queries share the session, so they run in sequence (the database sizes
        last), and the Redis reads run alongside them.
        """
        (inventory, projects, database), redis_stats, topology = await asyncio.gather(
            self._postgres(),
            self.redis(),
            ClusterReader(self._redis_client).topology(),
        )
        return SystemStats(
            **dict(topology),
            inventory=inventory,
            projects=projects,
            database=database,
            redis=redis_stats,
        )

    async def inventory(self) -> InventoryStats:
        """Exact entity counts from Postgres, proxies per status from their column, and the provider types known."""
        session = self._session
        row = (await session.execute(_INVENTORY_SQL)).one()
        roles = {str(r[0]): int(r[1]) for r in (await session.execute(_USER_ROLES_SQL)).all()}
        statuses = await ProxyRepository(session).count_by_status()
        registry = self._registry
        builtin = 0 if registry is None else sum(1 for ptype in registry.list() if ptype.source != "custom")
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

    async def project_usage(self) -> list[ProjectUsage]:
        """Per-project inventory, busiest first."""
        rows = (await self._session.execute(_PROJECT_USAGE_SQL, {"limit": MAX_PROJECT_ROWS})).all()
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

    async def database(self) -> DatabaseStats:
        """Database and per-table sizes, and the connections to the database.

        Run it last on a shared session: a role without the rights it needs
        leaves the transaction aborted, which only this section reports.
        """
        session = self._session
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
                    dead_rows=int(r[5]),
                    last_vacuum_at=r[6],
                )
                for r in (await session.execute(_TABLE_SIZES_SQL)).all()
            ]
        except Exception as exc:  # pragma: no cover - depends on grants
            logger.warning("Database stats unavailable", error=str(exc))
            stats.error = str(exc)
        return stats

    async def redis(self, *, scan_keyspace: bool = True) -> RedisStats:
        """Redis memory, throughput and, unless ``scan_keyspace`` is False, a bounded breakdown of the keyspace.

        Everything but the breakdown comes from ``INFO`` and ``DBSIZE``, which are O(1); the snapshot job skips the
        ``SCAN``, since it records only memory and key count.
        """
        stats = RedisStats()
        try:
            client = self._redis_client.client
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

    async def _postgres(self) -> tuple[InventoryStats, list[ProjectUsage], DatabaseStats]:
        """The Postgres sections, one after another on the shared session, the database sizes last."""
        inventory = await self.inventory()
        projects = await self.project_usage()
        database = await self.database()
        return inventory, projects, database
