# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The observation pipeline: request path to Postgres without touching Postgres on the way.

Three stages, mirroring the request-metrics pipeline:

1. :class:`ObservationRecorder` collects :class:`IpObservation` values in
   memory on whichever path produced them (a lookup, a health check, a
   preflight). Appending is a list append; nothing awaits.
2. The recorder's publisher loop (every instance) pushes the buffer to one
   Redis list in a single ``RPUSH`` every few seconds.
3. :class:`ObservationFlusher` (leader only) pops batches off the list,
   bulk-inserts the raw rows, upserts the per-connector daily aggregates the
   accuracy view reads, and applies retention to the raw rows.

Losing Redis loses at most a few seconds of observations. Nothing on the
request path waits for a database.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, cast

import structlog
from pydantic import ValidationError

from api.core import utc_now
from api.core.job_stats import job_stats
from api.core.leadership import Lease
from api.core.workers import LeaseName, WorkerName
from api.db.geo_repository import ExitSighting, ObservationRepository
from api.db.redis import GEO_OBSERVATIONS_KEY, RedisClient
from api.geo.models import IpObservation, ObservationSource
from api.geo.store import SessionFactory

logger = structlog.get_logger()

# Standby instances poll this often to see whether the flush lease has freed up.
_LEASE_RETRY_SECONDS = 30.0
# Rows popped from Redis per flush cycle. Bounded so one cycle never holds a
# transaction open for a minute after a traffic burst.
FLUSH_BATCH_SIZE = 2000
# Retention runs at most this often.
_RETENTION_EVERY = timedelta(hours=1)


class ObservationRecorder:
    """Per-instance buffer of observations and the loop that publishes it to Redis."""

    def __init__(self, redis_client: RedisClient | None, *, max_buffer: int = 5000, interval: float = 5.0) -> None:
        self._redis = redis_client
        self._buffer: list[IpObservation] = []
        self._max_buffer = max_buffer
        self._interval = interval
        self._running = False
        self._early_push: asyncio.Task[None] | None = None
        self.dropped = 0
        self.published = 0

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def record(self, observation: IpObservation) -> None:
        """Append one observation. Never blocks; drops the oldest when the buffer is full.

        A burst that fills half the buffer triggers a push right away rather
        than waiting for the next tick, so only an unreachable Redis can drop.
        """
        if len(self._buffer) >= self._max_buffer:
            del self._buffer[0]
            self.dropped += 1
        self._buffer.append(observation)
        if len(self._buffer) >= self._max_buffer // 2 and self._redis is not None and self._early_push is None:
            try:
                self._early_push = asyncio.create_task(self._push_early())
            except RuntimeError:
                self._early_push = None  # no running loop (tests calling record() synchronously)

    async def _push_early(self) -> None:
        try:
            await self.publish()
        finally:
            self._early_push = None

    async def publish(self) -> int:
        """Push everything buffered so far to the Redis list. Returns the count pushed."""
        if not self._buffer or self._redis is None:
            return 0
        batch, self._buffer = self._buffer, []
        payloads = [o.model_dump_json() for o in batch]
        try:
            await cast("Awaitable[Any]", self._redis.client.rpush(GEO_OBSERVATIONS_KEY, *payloads))
        except Exception as exc:
            # Put them back (bounded) and try again next tick.
            self._buffer = (batch + self._buffer)[-self._max_buffer :]
            logger.warning("Publishing IP observations failed", error=str(exc), count=len(batch))
            return 0
        self.published += len(batch)
        return len(batch)

    async def run(self) -> None:
        """Publisher loop: every instance runs one."""
        self._running = True
        job_stats.declare_interval(WorkerName.GEO_OBSERVATION_PUBLISHER, self._interval)
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                with job_stats.track(WorkerName.GEO_OBSERVATION_PUBLISHER) as run:
                    if await self.publish() == 0:
                        run.idle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("IP observation publisher error", error=str(exc))

    def stop(self) -> None:
        self._running = False


# Two instances briefly owning the same proxy under rendezvous hashing (a
# membership change, a restart) both check it and both report the same exit.
# Sightings of one proxy, one IP and one source this close together are one
# event for the aggregates; the raw rows keep both for the record.
DUPLICATE_WINDOW = timedelta(seconds=15)


def dedupe_sightings(observations: list[IpObservation]) -> list[IpObservation]:
    """Drop repeats of the same (proxy, ip, source) that fall within DUPLICATE_WINDOW of the kept one."""
    kept: list[IpObservation] = []
    last_seen: dict[tuple[str | None, str, ObservationSource], IpObservation] = {}
    for o in sorted(observations, key=lambda o: o.observed_at):
        key = (o.proxy_id, o.ip, o.source)
        previous = last_seen.get(key)
        if previous is not None and o.observed_at - previous.observed_at <= DUPLICATE_WINDOW:
            continue
        last_seen[key] = o
        kept.append(o)
    return kept


def _latest_state(sighting: ExitSighting, o: IpObservation) -> None:
    sighting.last_seen = o.observed_at
    sighting.country = o.resolved_country or sighting.country
    sighting.proxy_id = o.proxy_id
    sighting.source = o.source.value
    sighting.claimed_country = o.claimed_country
    sighting.resolved_source = o.resolved_source.value if o.resolved_source else None
    sighting.conflict = o.conflict
    sighting.disagreement = o.disagreement


def aggregate_exits(observations: list[IpObservation]) -> dict[tuple[str, str], ExitSighting]:
    """Fold a batch into one ExitSighting per (connector, IP).

    Every observation refreshes the exit's latest state (who held it, what
    was claimed, how it was judged), but only sightings flagged ``new_exit``
    count as hand-outs. Re-attribution, Detect on an unchanged proxy and
    preflight verify an exit the proxy already had; they update the state and
    leave the count alone.
    """
    exits: dict[tuple[str, str], ExitSighting] = {}
    for o in observations:
        if not o.connector_id:
            continue
        key = (o.connector_id, o.ip)
        current = exits.get(key)
        if current is None:
            current = exits[key] = ExitSighting(
                first_seen=o.observed_at, last_seen=o.observed_at, count=1 if o.new_exit else 0
            )
            _latest_state(current, o)
            continue
        if o.new_exit:
            current.count += 1
        if o.observed_at < current.first_seen:
            current.first_seen = o.observed_at
        if o.observed_at >= current.last_seen:
            _latest_state(current, o)
    return exits


def decode_batch(raw: list[Any]) -> list[IpObservation]:
    """Parse popped Redis payloads, dropping anything that is not an observation."""
    parsed: list[IpObservation] = []
    for item in raw:
        if isinstance(item, bytes):
            item = item.decode("utf-8", errors="replace")
        try:
            parsed.append(IpObservation(**json.loads(item)))
        except (TypeError, ValueError, ValidationError):
            logger.debug("Skipping malformed IP observation")
    return parsed


class ObservationFlusher:
    """Leader-elected: Redis list to Postgres rows and aggregates, plus retention."""

    def __init__(
        self,
        session_factory: SessionFactory,
        redis_client: RedisClient,
        instance_id: str,
        *,
        interval: float = 30.0,
        retention_days: Callable[[], int] | int = 7,
        exit_ip_retention_days: Callable[[], int] | int = 0,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._instance_id = instance_id
        self._interval = interval
        self._retention_days = retention_days
        self._exit_ip_retention_days = exit_ip_retention_days
        self._running = False
        self._last_retention = utc_now() - _RETENTION_EVERY

    def _retention(self) -> int:
        value = self._retention_days() if callable(self._retention_days) else self._retention_days
        return int(value)

    def _exit_retention(self) -> int:
        value = self._exit_ip_retention_days() if callable(self._exit_ip_retention_days) else self._exit_ip_retention_days
        return int(value)

    async def run(self) -> None:
        self._running = True
        job_stats.declare_interval(WorkerName.GEO_OBSERVATION_FLUSHER, self._interval)
        lease = Lease(self._redis, name=LeaseName.GEO_OBSERVATION_FLUSHER, owner_id=self._instance_id)
        try:
            while self._running:
                try:
                    if not lease.is_held and not await lease.try_acquire():
                        await asyncio.sleep(_LEASE_RETRY_SECONDS)
                        continue
                    await asyncio.sleep(self._interval)
                    if lease.is_held and self._running:
                        with job_stats.track(WorkerName.GEO_OBSERVATION_FLUSHER) as run:
                            if await self.flush_once() == 0:
                                run.idle()
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.error("IP observation flush error", error=str(exc))
        finally:
            await lease.release()

    async def flush_once(self) -> int:
        """Drain up to ``FLUSH_BATCH_SIZE`` observations. Returns how many were written."""
        try:
            raw = await cast("Awaitable[Any]", self._redis.client.lpop(GEO_OBSERVATIONS_KEY, FLUSH_BATCH_SIZE))
        except Exception as exc:
            logger.warning("Could not pop IP observations", error=str(exc))
            return 0
        if not raw:
            await self._maybe_retain()
            return 0
        if not isinstance(raw, list):
            raw = [raw]
        observations = decode_batch(raw)
        if not observations:
            return 0
        async with self._session_factory() as session:
            repo = ObservationRepository(session)
            written = await repo.insert_many(observations)
            # The aggregates cascade with their connector; a connector deleted
            # between sighting and flush must not fail the whole batch.
            live = await repo.existing_connector_ids({o.connector_id for o in observations if o.connector_id})
            aggregable = dedupe_sightings([o for o in observations if o.connector_id in live])
            await repo.add_exit_ips(aggregate_exits(aggregable))
            await session.commit()
        logger.info("IP observations flushed", count=written)
        await self._maybe_retain()
        return written

    async def _maybe_retain(self) -> None:
        if utc_now() - self._last_retention < _RETENTION_EVERY:
            return
        self._last_retention = utc_now()
        days = self._retention()
        exit_days = self._exit_retention()
        if days <= 0 and exit_days <= 0:
            return
        async with self._session_factory() as session:
            repo = ObservationRepository(session)
            removed = await repo.delete_older_than(utc_now() - timedelta(days=days)) if days > 0 else 0
            forgotten = (
                await repo.delete_exit_ips_last_seen_before(utc_now() - timedelta(days=exit_days)) if exit_days > 0 else 0
            )
            await session.commit()
        if removed or forgotten:
            logger.info(
                "Attribution history pruned", observations=removed, exit_ips=forgotten,
                retention_days=days, exit_ip_retention_days=exit_days,
            )

    def stop(self) -> None:
        self._running = False
