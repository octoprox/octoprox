# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Housekeeping jobs: the install-wide upkeep the controller's timers publish, each safe to run again or twice at once."""

from __future__ import annotations

import time
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING

import structlog

from api.core import utc_now
from api.core.roles import JobKind
from api.core.system_stats import SystemReader
from api.db.openvpn_repository import OpenVpnPeerRepository
from api.db.redis import (
    INSTANCE_INDEX_KEY,
    INSTANCE_REGISTRY_KEY,
    QUARANTINE_KEY,
    TUNNEL_PEER_STATUS_INDEX_KEY,
    WORK_STREAMS,
    WORKER_GROUP,
    tunnel_carrier_key,
)
from api.db.repository import TUNNEL_PEER_TABLES, SystemMetricsRepository
from api.db.streams import StreamGroup
from api.db.wireguard_repository import WireGuardPeerRepository
from api.feed.state import RuntimeStateReader
from api.jobs.job import Job, JobBatch
from api.models.openvpn import OpenVpnSessionReading
from api.models.wireguard import WireGuardPeerReading

if TYPE_CHECKING:
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.geo.updater import GeoDatabaseUpdater
    from api.jobs.queue import JobQueue

logger = structlog.get_logger()

# The shortest interval between gauge readings, in seconds: each one runs size
# queries over every table, so a misconfiguration cannot hammer Postgres.
MIN_SNAPSHOT_INTERVAL_SECONDS = 60
# A reading younger than this share of the interval stands for the current
# one, so a snapshot published twice or a little early adds no row.
_DUE_TOLERANCE = 0.9

# Seconds a work stream consumer with nothing pending idles before index
# hygiene removes it. A live worker reads, claims or reclaims at least every
# 20 s, and each resets its idle time, so only a stopped worker gets here;
# its name, fresh per process, never comes back.
CONSUMER_PRUNE_SECONDS = 3600


class SnapshotJob(Job):
    """Stores one install-wide gauge reading in ``system_metrics`` when one is due, then deletes those past retention.

    Args:
        interval: Seconds between readings (``system_metrics_interval``), at least ``MIN_SNAPSHOT_INTERVAL_SECONDS``; 0 takes none.
        retention_days: Days readings are kept (``system_metrics_retention_days``); 0 keeps them all.
    """

    kind = JobKind.SNAPSHOT

    def __init__(
        self, session_factory: SessionFactory, redis_client: RedisClient, *, interval: int, retention_days: int
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._interval = interval
        self._retention_days = retention_days

    async def run(self, batch: JobBatch) -> None:
        """Take a reading unless the newest one is younger than the interval, and apply retention after it."""
        if self._interval <= 0 or not await self._due():
            return
        await self._take()
        await self._retain()

    async def _due(self) -> bool:
        """Whether the newest stored reading is old enough for another: the readings set the cadence, not a clock here."""
        async with self._session_factory() as session:
            latest = await SystemMetricsRepository(session).get_latest_timestamp()
        if latest is None:
            return True
        age = (utc_now() - latest).total_seconds()
        if age >= max(MIN_SNAPSHOT_INTERVAL_SECONDS, self._interval) * _DUE_TOLERANCE:
            return True
        logger.debug("System snapshot not due yet", age_seconds=round(age))
        return False

    async def _take(self) -> None:
        """Read the gauges from Postgres and Redis and store them as one row."""
        async with self._session_factory() as session:
            reader = SystemReader(session, self._redis)
            inventory = await reader.inventory()
            database = await reader.database()
        # Memory and key count only: no walk of the keyspace, and no use of the session.
        redis_stats = await reader.redis(scan_keyspace=False)
        async with self._session_factory() as session:
            await SystemMetricsRepository(session).save_snapshot(
                database_size_bytes=database.size_bytes,
                redis_memory_bytes=redis_stats.used_memory_bytes,
                redis_keys=redis_stats.total_keys,
                projects=inventory.projects,
                credentials=inventory.credentials,
                connectors=inventory.connectors,
                connectors_enabled=inventory.connectors_enabled,
                users=inventory.users,
                proxies_total=inventory.proxies,
                proxies_healthy=inventory.proxies_by_status.get("healthy", 0),
                proxies_unhealthy=inventory.proxies_by_status.get("unhealthy", 0),
                table_sizes={table.name: table.total_bytes for table in database.tables},
                proxy_status_counts=inventory.proxies_by_status,
            )
            await session.commit()
        logger.debug(
            "System snapshot stored",
            database_size_bytes=database.size_bytes,
            redis_keys=redis_stats.total_keys,
            proxies=inventory.proxies,
        )

    async def _retain(self) -> None:
        """Delete the readings past the retention window, unless it is 0."""
        if self._retention_days <= 0:
            return
        cutoff = utc_now() - timedelta(days=self._retention_days)
        async with self._session_factory() as session:
            deleted = await SystemMetricsRepository(session).delete_older_than(cutoff)
            await session.commit()
        if deleted:
            logger.info("Pruned system metrics", rows_deleted=deleted)


class GeoUpdateJob(Job):
    """Downloads the IP database updates that are due; the updater asks for a re-attribution once a new file is stored."""

    kind = JobKind.GEO_UPDATE

    def __init__(self, updater: GeoDatabaseUpdater) -> None:
        self._updater = updater

    async def run(self, batch: JobBatch) -> None:
        """One tick of the updater: give back the space replaced files hold, then download every database due."""
        await self._updater.tick()


class TunnelLastSeenJob(Job):
    """Records when each tunnel device was last seen, from the statuses the gateways publish, so it outlives restarts."""

    kind = JobKind.TUNNEL_LAST_SEEN

    def __init__(self, session_factory: SessionFactory, redis_client: RedisClient) -> None:
        self._session_factory = session_factory
        self._redis = redis_client

    async def run(self, batch: JobBatch) -> None:
        """Move each device's last sighting forward to the newest one published: WireGuard's by public key, OpenVPN's by ID."""
        wireguard = WireGuardPeerReading.latest(await self._redis.get_tunnel_peer_status("wireguard"))
        openvpn = OpenVpnSessionReading.latest(await self._redis.get_tunnel_peer_status("openvpn"))
        handshakes = [
            (public_key, seen, reading.endpoint)
            for public_key, reading in wireguard.items()
            if (seen := reading.handshake_at) is not None
        ]
        sessions = [
            (peer_id, seen, reading.endpoint)
            for peer_id, reading in openvpn.items()
            if (seen := reading.connected_at) is not None
        ]
        if not handshakes and not sessions:
            return
        async with self._session_factory() as session:
            moved = await WireGuardPeerRepository(session).record_last_seen(handshakes)
            moved += await OpenVpnPeerRepository(session).record_last_seen(sessions)
            await session.commit()
        if moved:
            logger.debug("Tunnel last sightings recorded", count=moved)


class IndexHygieneJob(Job):
    """Prunes what stopped processes left in Redis and the ended quarantines, and asks for a traffic decision of every connector blocked or waiting for one."""

    kind = JobKind.INDEX_HYGIENE

    def __init__(self, redis_client: RedisClient, jobs: JobQueue) -> None:
        self._redis = redis_client
        self._jobs = jobs
        self._runtime = RuntimeStateReader(redis_client)

    async def run(self, batch: JobBatch) -> None:
        """Prune the process indexes and the work streams' stopped consumers, trim the quarantines, then queue the decisions."""
        ids = await self._prune_indexes()
        consumers = await self._prune_consumers()
        ended = int(await self._redis.client.zremrangebyscore(QUARANTINE_KEY, "-inf", time.time()))
        # Every block and every request no decision has settled, decided again: a lost or stale decision lasts a minute.
        to_decide = list(dict.fromkeys([*await self._runtime.blocked(), *await self._runtime.pending()]))
        await self._jobs.publish(JobKind.TRAFFIC, to_decide)
        logger.debug("Index hygiene done", ids=ids, consumers=consumers, quarantines=ended, traffic=len(to_decide))

    async def _prune_indexes(self) -> int:
        """Drop the processes and tunnel carriers whose keys expired from the indexes listing them; how many went."""
        dropped = await self._redis.prune_index(
            INSTANCE_INDEX_KEY, lambda instance_id: INSTANCE_REGISTRY_KEY.format(instance_id=instance_id)
        )
        for protocol in TUNNEL_PEER_TABLES:
            dropped += await self._redis.prune_index(
                TUNNEL_PEER_STATUS_INDEX_KEY.format(protocol=protocol), partial(tunnel_carrier_key, protocol)
            )
        return dropped

    async def _prune_consumers(self) -> int:
        """Remove each work stream consumer with nothing pending that idled ``CONSUMER_PRUNE_SECONDS``; how many went."""
        removed = 0
        for key in WORK_STREAMS.values():
            group = StreamGroup(self._redis, key, WORKER_GROUP, self._redis.writer_id)
            for consumer, dropped in (await group.prune_idle_consumers(int(CONSUMER_PRUNE_SECONDS * 1000))).items():
                removed += 1
                if dropped:
                    logger.warning(
                        "Entries pending for a stopped consumer were dropped with it",
                        stream=group.name,
                        consumer=consumer,
                        entries=dropped,
                    )
        if removed:
            logger.info("Stopped workers removed from the work streams", consumers=removed)
        return removed
