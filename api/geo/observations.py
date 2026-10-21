# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""IP observations from the path that saw an exit to Postgres: each process's recorder appends them to the observations stream, and the workers ingest them.

Every write the ingest makes only moves forward, so workers take batches in any order and a hand-out counts once whichever worker sees it first.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from enum import IntEnum
from typing import TYPE_CHECKING

import structlog

from api.core.consumers import GroupConsumer, decode_items
from api.core.logging import error_text
from api.core.publishers import ItemPublisher
from api.core.workers import WorkerName
from api.db.geo_repository import ExitSighting, ObservationRepository
from api.db.redis import (
    OBSERVATION_STREAM_KEY,
    OBSERVATION_STREAM_MAXLEN,
    PROXY_LAST_EXIT_KEY,
    PROXY_LAST_EXIT_TTL_SECONDS,
    BatchChannel,
    RedisClient,
)
from api.geo.models import ExitJudgement, IpObservation

if TYPE_CHECKING:
    from redis.commands.core import AsyncScript

    from api.db.session import SessionFactory
    from api.db.streams import StreamEntry

logger = structlog.get_logger()

# Entries one read of the observations stream takes, ingested in one
# transaction. A batch costs about a dozen round trips plus one per thousand
# exits upserted or judged, so rows per second grow with rows per batch.
INGEST_BATCH_SIZE = 5000
# Entry IDs a log line names at most.
_LOGGED_IDS = 5
# Naive UTC's epoch, and the unit a proxy's last counted exit is timed in.
_EPOCH = datetime(1970, 1, 1)
_MILLISECOND = timedelta(milliseconds=1)

# Lua: the hand-out verdict of each observation of a proxy against the proxy's
# last counted exit (PROXY_LAST_EXIT_KEY, "<observed_at epoch ms> <ip>"),
# oldest first. KEYS[i] is observation i's key; ARGV[1] the key's TTL in
# seconds, then observation i's IP at ARGV[2i] and its observed_at in epoch ms
# at ARGV[2i + 1]. Returns per observation 1 when it is newer than the record
# and of another IP, 0 when it is of the same IP or not newer, and -1 when
# there is no record; the record moves to every newer observation, and to the
# first one when there was none. Each verdict is made and recorded together,
# so batches on any number of workers count each transition once.
_HAND_OUTS_SCRIPT = """
local verdicts = {}
for i = 1, #KEYS do
  local ip, at = ARGV[2 * i], ARGV[2 * i + 1]
  local seen, held = string.match(redis.call('GET', KEYS[i]) or '', '^(%d+) (.+)$')
  local newer = not seen or tonumber(at) > tonumber(seen)
  if not seen then
    verdicts[i] = -1
  elseif newer and held ~= ip then
    verdicts[i] = 1
  else
    verdicts[i] = 0
  end
  if newer then
    redis.call('SET', KEYS[i], at .. ' ' .. ip, 'EX', ARGV[1])
  end
end
return verdicts
"""


class HandOut(IntEnum):
    """The hand-out script's verdict on one observation of a proxy, against the proxy's last counted exit."""

    YES = 1  # newer than the record and of another IP: the proxy was handed a new exit
    NO = 0  # of the same IP, or not newer: a repeat, or a late or redelivered sighting
    NO_RECORD = -1  # nothing counted for the proxy (new, idle for a day, Redis wiped): the exit table decides


class ObservationRecorder(ItemPublisher[IpObservation | ExitJudgement]):
    """This process's observations and judgements, buffered off the request path and appended to the observations stream each interval.

    Args:
        max_buffer: the most items held, through a Redis outage too; when full the oldest goes, counted in ``dropped``.
    """

    def __init__(self, redis_client: RedisClient | None, *, max_buffer: int = 5000, interval: float = 5.0) -> None:
        super().__init__(
            redis_client,
            name=WorkerName.GEO_OBSERVATION_PUBLISHER,
            interval=interval,
            channel=BatchChannel.OBSERVATION_APPENDS,
            key=OBSERVATION_STREAM_KEY,
            max_items=OBSERVATION_STREAM_MAXLEN,
            max_pending=max_buffer,
        )

    def record(self, item: IpObservation | ExitJudgement) -> None:
        """Buffer one observation or judgement, never waiting; at half a buffer the loop sends at once, unless a part is owed.

        So only an unreachable Redis drops anything, and a Redis in trouble is sent to at the loop's pace, not per item.
        """
        self.push(item)
        if self._windowed() >= self._max_pending // 2 and not self.owed:
            self.wake()

    async def queued(self) -> int | None:
        """Entries on the observations stream, published and not ingested yet; None without Redis or when it cannot say."""
        if self._redis is None:
            return None
        try:
            return int(await self._redis.client.xlen(OBSERVATION_STREAM_KEY))
        except Exception as exc:
            logger.warning("Could not read the IP observation stream's length", error=error_text(exc))
            return None


class ObservationIngest(GroupConsumer):
    """Writes the observations stream into Postgres on the workers, a batch at a time in one transaction.

    A batch's hand-outs are decided first, by one script against each proxy's last counted exit; then its log rows, the
    exit table and the judgements are written, each of them only moving forward.
    """

    def __init__(self, redis_client: RedisClient, session_factory: SessionFactory) -> None:
        super().__init__(
            redis_client, OBSERVATION_STREAM_KEY, name=WorkerName.OBSERVATION_INGEST, count=INGEST_BATCH_SIZE
        )
        self._session_factory = session_factory
        self._script: AsyncScript | None = None

    async def _handle(self, entries: list[StreamEntry]) -> None:
        """Ingest one batch, its malformed entries logged and completed with it; raising leaves the whole batch pending."""
        items, malformed = decode_items(entries, self.parse)
        if malformed:
            logger.warning(
                "Malformed IP observations skipped",
                count=len(malformed),
                ids=[entry.id for entry in malformed[:_LOGGED_IDS]],
            )
        observations = [item for item in items if isinstance(item, IpObservation)]
        judgements = [item for item in items if isinstance(item, ExitJudgement)]
        if not observations and not judgements:
            return
        started = time.monotonic()
        verdicts = await self._verdicts(observations)
        async with self._session_factory() as session:
            repo = ObservationRepository(session)
            written = await repo.insert_many(observations)
            # The exit table cascades with its connector: one deleted since the sighting must not fail the batch,
            # and a judgement of its exits finds no row to rewrite.
            live = await repo.existing_connector_ids({o.connector_id for o in observations if o.connector_id})
            sightings = await self._sightings(repo, observations, verdicts, live)
            await repo.add_exit_ips(ExitSighting.fold(sightings))
            judged = await repo.apply_judgements(judgements)
            await session.commit()
        logger.debug(
            "IP observations ingested",
            count=written,
            hand_outs=sum(hand_out for _observation, hand_out in sightings),
            judged=judged,
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    async def _verdicts(self, observations: list[IpObservation]) -> list[tuple[IpObservation, HandOut]]:
        """Each observation of a proxy, oldest first, with its hand-out verdict, every record moved in one script call."""
        timed = sorted(
            ((_epoch_ms(o.observed_at), o) for o in observations if o.proxy_id and o.connector_id),
            key=lambda pair: pair[0],
        )
        if not timed:
            return []
        if self._script is None:
            self._script = self._redis.client.register_script(_HAND_OUTS_SCRIPT)
        args: list[str | int] = [PROXY_LAST_EXIT_TTL_SECONDS]
        for ms, o in timed:
            args += [o.ip, ms]
        replies = await self._script(keys=[PROXY_LAST_EXIT_KEY.format(proxy_id=o.proxy_id) for _ms, o in timed], args=args)
        return [(o, HandOut(int(reply))) for (_ms, o), reply in zip(timed, replies, strict=True)]

    async def _sightings(
        self,
        repo: ObservationRepository,
        observations: list[IpObservation],
        verdicts: list[tuple[IpObservation, HandOut]],
        live: set[str],
    ) -> list[tuple[IpObservation, bool]]:
        """Each observation of a connector that still exists, with whether it is a hand-out; one without a proxy never is."""
        unknown = {
            (o.connector_id or "", o.ip)
            for o, verdict in verdicts
            if verdict is HandOut.NO_RECORD and o.connector_id in live
        }
        holders = await repo.exit_holders(unknown)
        sightings = [(o, self._hand_out(o, verdict, holders)) for o, verdict in verdicts if o.connector_id in live]
        return sightings + [(o, False) for o in observations if not o.proxy_id and o.connector_id in live]

    @staticmethod
    def parse(text: str) -> IpObservation | ExitJudgement:
        """One observations stream item: a judgement when its ``kind`` says so, else an observation; ValueError when malformed."""
        data = json.loads(text)
        if isinstance(data, dict) and data.get("kind") == "judgement":
            return ExitJudgement.model_validate(data)
        return IpObservation.model_validate(data)

    @staticmethod
    def _hand_out(observation: IpObservation, verdict: HandOut, holders: dict[tuple[str, str], str | None]) -> bool:
        """Whether an observation is a hand-out: its verdict, or with no record kept, whether its exit is new to the connector or another proxy's.

        An exit on record with this proxy as its holder, or with no holder (a row from before holders were kept), counts as counted before.
        """
        if verdict is not HandOut.NO_RECORD:
            return verdict is HandOut.YES
        key = (observation.connector_id or "", observation.ip)
        if key not in holders:
            return True
        holder = holders[key]
        return holder is not None and holder != observation.proxy_id


def _epoch_ms(moment: datetime) -> int:
    """A naive UTC time in epoch milliseconds, the unit a proxy's last counted exit is timed in."""
    return (moment - _EPOCH) // _MILLISECOND
