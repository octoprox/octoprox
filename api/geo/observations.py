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
   bulk-inserts the raw rows, decides which sightings are hand-outs, upserts
   the per-connector exit table the accuracy and exit views read, and applies
   retention.

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
from api.geo.models import ExitJudgement, IpObservation
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
    """Per-instance buffer of observations and judgements, and the loop that publishes it to Redis."""

    def __init__(self, redis_client: RedisClient | None, *, max_buffer: int = 5000, interval: float = 5.0) -> None:
        self._redis = redis_client
        self._buffer: list[IpObservation | ExitJudgement] = []
        self._max_buffer = max_buffer
        self._interval = interval
        self._running = False
        self._early_push: asyncio.Task[None] | None = None
        self.dropped = 0
        self.published = 0

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def record(self, observation: IpObservation | ExitJudgement) -> None:
        """Append one observation or judgement. Never blocks; drops the oldest when the buffer is full.

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


def decide_hand_outs(
    observations: list[IpObservation],
    previous: dict[str, str | None],
    on_record: dict[tuple[str, str], str | None] | None = None,
) -> tuple[list[tuple[IpObservation, bool]], dict[str, str]]:
    """Mark which sightings are hand-outs: the proxy's exit differs from the last one counted.

    ``previous`` is the last counted exit per proxy (from the proxy status
    hash). Sightings are walked in time order and each updates the running
    exit, so two instances reporting the same IP for a proxy, however far
    apart, yield one hand-out: the second sees the first's IP already
    recorded.

    ``on_record`` is the exit table's answer for proxies Redis knows nothing
    about (after an upgrade or a Redis loss): the proxy that last held each
    (connector, ip). An exit already on record with this proxy as its holder,
    or with no holder recorded at all, is treated as counted before. That
    errs on purpose: a proxy that left an exit and came back to it while
    Redis knew nothing goes uncounted once, because the alternative is
    counting every existing exit again on every upgrade. Sightings without a
    proxy are never hand-outs; the count means hand-outs to a proxy. Returns
    the marked sightings and the exit to record for every proxy seen.
    """
    known: dict[str, str | None] = dict(previous)
    marked: list[tuple[IpObservation, bool]] = []
    for o in sorted(observations, key=lambda o: o.observed_at):
        if not o.proxy_id:
            marked.append((o, False))
            continue
        if known.get(o.proxy_id) is not None:
            hand_out = known[o.proxy_id] != o.ip
        else:
            # Nothing counted for this proxy yet: consult what the exit table has.
            key = (o.connector_id or "", o.ip)
            if on_record is None or key not in on_record:
                hand_out = True  # never seen this exit for this connector
            else:
                holder = on_record[key]
                hand_out = holder is not None and holder != o.proxy_id  # someone else held it: reuse
        known[o.proxy_id] = o.ip
        marked.append((o, hand_out))
    latest: dict[str, str] = {}
    for proxy_id, ip in known.items():
        if ip is not None and previous.get(proxy_id) != ip:
            latest[proxy_id] = ip
    return marked, latest


def _latest_state(sighting: ExitSighting, o: IpObservation) -> None:
    sighting.last_seen = o.observed_at
    sighting.country = o.resolved_country or sighting.country
    sighting.proxy_id = o.proxy_id
    sighting.source = o.source.value
    sighting.claimed_country = o.claimed_country
    sighting.resolved_source = o.resolved_source.value if o.resolved_source else None
    sighting.conflict = o.conflict
    sighting.disagreement = o.disagreement


def aggregate_exits(sightings: list[tuple[IpObservation, bool]]) -> dict[tuple[str, str], ExitSighting]:
    """Fold a batch into one ExitSighting per (connector, IP).

    Each item is an observation and whether :func:`decide_hand_outs` judged
    it a hand-out. Every observation refreshes the exit's latest state (who
    held it, what was claimed, how it was judged); only hand-outs move the
    count.
    """
    exits: dict[tuple[str, str], ExitSighting] = {}
    for o, hand_out in sightings:
        if not o.connector_id:
            continue
        key = (o.connector_id, o.ip)
        current = exits.get(key)
        if current is None:
            current = exits[key] = ExitSighting(
                first_seen=o.observed_at, last_seen=o.observed_at, count=1 if hand_out else 0
            )
            _latest_state(current, o)
            continue
        if hand_out:
            current.count += 1
        if o.observed_at < current.first_seen:
            current.first_seen = o.observed_at
        if o.observed_at >= current.last_seen:
            _latest_state(current, o)
    return exits


def decode_batch(raw: list[Any]) -> tuple[list[IpObservation], list[ExitJudgement]]:
    """Parse popped Redis payloads into sightings and judgements, dropping anything malformed.

    During a rolling upgrade the list can hold payloads from the other
    version: an old instance's re-attribution rows and a new instance's
    judgements are unreadable to the other side and are dropped here. Only
    verdict rewrites are lost, never sightings, and the next re-attribution
    restores them.
    """
    observations: list[IpObservation] = []
    judgements: list[ExitJudgement] = []
    for item in raw:
        if isinstance(item, bytes):
            item = item.decode("utf-8", errors="replace")
        try:
            data = json.loads(item)
            if isinstance(data, dict) and data.get("kind") == "judgement":
                judgements.append(ExitJudgement(**data))
            else:
                observations.append(IpObservation(**data))
        except (TypeError, ValueError, ValidationError):
            logger.debug("Skipping malformed IP observation")
    return observations, judgements


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
        observations, judgements = decode_batch(raw)
        if not observations and not judgements:
            return 0
        async with self._session_factory() as session:
            repo = ObservationRepository(session)
            written = await repo.insert_many(observations) if observations else 0
            # The aggregates cascade with their connector; a connector deleted
            # between sighting and flush must not fail the whole batch.
            live = await repo.existing_connector_ids(
                {o.connector_id for o in observations if o.connector_id}
                | {j.connector_id for j in judgements if j.connector_id}
            )
            tracked = [o for o in observations if o.connector_id in live]
            # Hand-outs are decided here, once, against each proxy's last counted
            # exit: every instance's sightings pass through this leader in order.
            previous = await self._redis.get_proxy_exit_ips(o.proxy_id for o in tracked if o.proxy_id)
            # Redis knows nothing about a proxy after an upgrade or a Redis
            # loss; the exit table, which only this flusher writes, then says
            # whether the exit was counted before.
            unknown = {
                (o.connector_id, o.ip)
                for o in tracked
                if o.proxy_id and o.connector_id and previous.get(o.proxy_id) is None
            }
            on_record = await repo.exit_holders(unknown)
            sightings, latest_exits = decide_hand_outs(tracked, previous, on_record)
            await repo.add_exit_ips(aggregate_exits(sightings))
            # Re-judgements rewrite verdicts on exits already on record: no log
            # row, no hand-out, no movement of first or last seen.
            judged = await repo.apply_judgements([j for j in judgements if j.connector_id in live])
            await session.commit()
        try:
            # Written only into status hashes that exist: a proxy removed between
            # sighting and flush has had its hash deleted and must stay deleted.
            await self._redis.set_proxy_exit_ips(latest_exits)
        except Exception as exc:
            # The counts are committed; at worst the next sighting of these
            # exits is counted once more. Logged rather than failing the batch.
            logger.warning("Could not record last counted exits", error=str(exc))
        logger.info("IP observations flushed", count=written, judged=judged)
        await self._maybe_retain()
        return written + judged

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
