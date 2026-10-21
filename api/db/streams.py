# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Redis streams: the change log writers publish to, the cursors broadcast consumers follow, and the groups work is taken through.

A broadcast entry says only what to read again; a work entry goes to one consumer of a group.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple

import structlog
from redis.exceptions import ResponseError

from api.core import utc_now
from api.core.logging import error_text
from api.core.tasks import HeldTasks
from api.db.redis import (
    CHANGES_STREAM_KEY,
    CHANGES_STREAM_MAXLEN,
    REPLY_TIMEOUT_SECONDS,
    RUNTIME_STREAM_KEY,
    RUNTIME_STREAM_MAXLEN,
    WORK_STREAM_NAMES,
    RedisClient,
    StreamId,
    StreamInfo,
    is_missing_key,
)
from api.models.system import StreamState

if TYPE_CHECKING:
    from redis.asyncio.client import Pipeline

logger = structlog.get_logger()

# The longest one read waits for an entry, in seconds. It stays below the
# client's reply bound, with room for a loop stall, so that bound never cuts
# a healthy wait.
STREAM_BLOCK_SECONDS = 20.0
if STREAM_BLOCK_SECONDS >= REPLY_TIMEOUT_SECONDS:
    raise RuntimeError("STREAM_BLOCK_SECONDS must stay below the client's reply bound, REPLY_TIMEOUT_SECONDS")
# Entries one XREAD returns at most; a read that fills it reads on at once.
STREAM_READ_COUNT = 1000
# The wait after a failed stream read or change entry, in seconds, doubled
# for each failure in a row up to the second value.
RETRY_FIRST_SECONDS = 1.0
RETRY_MAX_SECONDS = 30.0

# The position before any entry: where a consumer of a missing stream reads from.
_START_ID = "0-0"
# The fields of a changes or runtime stream entry: what changed, and the row it is about ("" for none or several).
KIND_FIELD = "kind"
REF_FIELD = "ref"


def ref_of(ids: Sequence[str]) -> str:
    """The ``ref`` of an entry about the rows ``ids``: the one id, or "" for none or several (logs only)."""
    return ids[0] if len(ids) == 1 else ""


def retry_delay(failures: int) -> float:
    """Seconds to wait after ``failures`` failures in a row: 1 s, doubling each time, at most 30 s."""
    # The exponent is bounded: a float overflows past 2 ** 1023, about nine hours of failures at the cap.
    return min(RETRY_FIRST_SECONDS * 2.0 ** min(max(failures - 1, 0), 32), RETRY_MAX_SECONDS)


def _block_ms(redis_client: RedisClient, block: float) -> int | None:
    """A wait of up to ``block`` seconds as a ``BLOCK`` argument, capped below the client's reply bound; None for none.

    A wait the reply bound cut short would read as a failed read; ``BLOCK 0``, which waits forever, is never sent.
    """
    cap = min(STREAM_BLOCK_SECONDS, redis_client.socket_timeout * STREAM_BLOCK_SECONDS / REPLY_TIMEOUT_SECONDS)
    wait = min(block, cap)
    return max(1, int(wait * 1000)) if wait > 0 else None


class ChangeKind(StrEnum):
    """What a committed write changed: the ``kind`` of a changes stream entry."""

    PROJECT = "project"
    CREDENTIAL = "credential"
    CONNECTOR = "connector"
    PROXY = "proxy"
    DESCRIPTOR = "descriptor"
    GEO_SETTINGS = "geo_settings"
    GEO_DATABASE = "geo_database"
    WIREGUARD = "wireguard"
    OPENVPN = "openvpn"
    # Anything may have changed: a backup import, migrations applied at start.
    ALL = "all"


class RuntimeSignal(StrEnum):
    """What a runtime stream entry tells the leading controller."""

    # A rate limiter tripped a quarantine or an admin lifted one; ref is the proxy.
    QUARANTINE = "quarantine"
    # A traffic job set or lifted a connector's block; ref is the connector.
    TRAFFIC = "traffic"

    def append(self, pipe: Pipeline, *ids: str) -> None:
        """Queue on ``pipe``, the transaction that made the change, this signal's runtime entry about the rows ``ids``."""
        pipe.xadd(
            RUNTIME_STREAM_KEY,
            {KIND_FIELD: self.value, REF_FIELD: ref_of(ids)},
            maxlen=RUNTIME_STREAM_MAXLEN,
            approximate=True,
        )


class FullRead(StrEnum):
    """Why a consumer read in full instead of following entries; ``StreamState.full_reads`` counts each."""

    START = "start"
    GAP = "gap"  # entries after the position were lost: a trim past it, a wipe, a stream recreated
    RECONNECT = "reconnect"  # a read failed, and Redis may have restarted empty meanwhile
    RECONCILE = "reconcile"  # the periodic full read, which catches writes that published nothing


class StreamEntry(NamedTuple):
    """One entry of a stream: its ID and its fields."""

    id: str
    fields: dict[str, str]

    @property
    def kind(self) -> str:
        """The ``kind`` field, "" when absent."""
        return self.fields.get(KIND_FIELD, "")

    @property
    def item(self) -> str:
        """The ``item`` field of a work stream entry, "" when absent: an entry deleted while pending has no fields."""
        return self.fields.get("item", "")

    @property
    def key(self) -> str:
        """The ``key`` field of a job stream entry, "" when absent: what the job is about, such as a proxy ID or ``all``."""
        return self.fields.get("key", "")


@dataclass(frozen=True)
class StreamRead:
    """What one read of one stream found: entries after the position, or why the consumer must read in full."""

    entries: list[StreamEntry] = field(default_factory=list)
    # Set instead of entries when the consumer must read in full; the cursor
    # has already moved to where that full read starts.
    gap: FullRead | None = None


class StreamCursor:
    """One consumer's position in one stream, held in memory, and the counts its snapshot reports.

    Only a :class:`StreamReader` moves it: past the entries a read returns, or to where a full read starts.
    """

    def __init__(self, redis_client: RedisClient, key: str) -> None:
        self._redis = redis_client
        self.key = key
        self._position: str | None = None
        # The full read the next read reports instead of reading: START until
        # the first, RECONNECT after a read that failed.
        self._due: FullRead | None = FullRead.START
        # Failed reads in a row: the back-off, and one log line per streak.
        self._failures = 0
        self._entries = 0
        self._full_reads: Counter[FullRead] = Counter()
        self._last_full_read_at: datetime | None = None
        self._lag: float | None = None

    @property
    def name(self) -> str:
        """The stream's short name (``changes``, ``runtime``, ``feed``), its key in a snapshot."""
        return self.key.rpartition(":")[2]

    @property
    def position(self) -> str | None:
        """The last entry ID handled (0-0 for none yet); None before the first full read."""
        return self._position

    def state(self) -> StreamState:
        """How far this process has read the stream, for its snapshot."""
        return StreamState(
            position=self._position,
            lag_seconds=self._lag,
            entries=self._entries,
            full_reads={reason.value: n for reason, n in self._full_reads.items()},
            last_full_read_at=self._last_full_read_at,
        )

    def _take(self, info: StreamInfo | None, reason: FullRead) -> str:
        """Move to where a full read starts, the stream's last entry, and count the full read; the new position."""
        before = self._position
        self._position = _START_ID if info is None else info.last_id
        self._due = None
        self._failures = 0
        self._full_reads[reason] += 1
        self._last_full_read_at = utc_now()
        self._lag = 0.0
        if reason is FullRead.GAP:
            logger.warning("Stream gap; reading in full", stream=self.name, position=before, reason=reason.value)
        elif reason is FullRead.RECONNECT:
            logger.info("Stream gap; reading in full", stream=self.name, position=before, reason=reason.value)
        return self._position

    def _lost(self, info: StreamInfo | None) -> bool:
        """Whether entries after the position may be gone, by ``XINFO STREAM`` as the read ended: the gap rule.

        ``max-deleted-entry-id`` is no help: no trim sets it.
        """
        at = StreamId.of(self._position or _START_ID)
        at_start = at == StreamId(0, 0)
        if info is None:
            return not at_start  # a wipe
        last = StreamId.of(info.last_id)
        if last < at:
            return True  # recreated with IDs below the position
        if not at_start and info.first_id is not None and StreamId.of(info.first_id) > at:
            return True  # trimmed past the position, or recreated above it
        if at_start and info.entries_added > info.length:
            return True  # trimmed before this consumer's first read
        return info.length == 0 and last > at  # emptied after the position

    def _advance(self, entries: Sequence[StreamEntry]) -> None:
        """Move past entries a read returned."""
        if entries:
            self._position = entries[-1].id
            self._entries += len(entries)

    def _caught_up(self, entries: Sequence[StreamEntry], head: StreamInfo | None) -> None:
        """End a read that found no gap: the failure streak is over, and the lag is what that read found."""
        self._failures = 0
        self._lag = 0.0
        if entries and head is not None:
            # From the first entry returned to the stream's newest, both by Redis's clock.
            self._lag = max(0, StreamId.of(head.last_id).ms - StreamId.of(entries[0].id).ms) / 1000

    def _failed(self, error: Exception) -> int:
        """Count a failed read, logged once per streak, and owe a full read; the failures in a row."""
        if self._failures == 0:
            logger.warning("Stream read failed", stream=self.name, error=error_text(error))
        self._failures += 1
        if self._due is None:
            self._due = FullRead.RECONNECT
        return self._failures


class StreamReader:
    """Reads one or more streams in one round trip, each from its own cursor's position."""

    def __init__(self, redis_client: RedisClient, cursors: Sequence[StreamCursor]) -> None:
        if any(cursor._redis is not redis_client for cursor in cursors):
            raise ValueError("a stream reader reads its cursors on its own client")
        if len({cursor.key for cursor in cursors}) != len(cursors):
            raise ValueError("a stream reader takes one cursor per stream")
        self._redis = redis_client
        self._cursors = list(cursors)

    async def start(self, reason: FullRead = FullRead.START) -> dict[str, str]:
        """Take every cursor's position for a full read, in one round trip; raises when Redis cannot be read."""
        return await self._take_positions(dict.fromkeys(self._cursors, reason))

    async def read(self, block: float) -> dict[str, StreamRead]:
        """One read of every stream, by key, waiting up to ``block`` seconds for an entry; never raises for Redis.

        A cursor owed a full read takes its position instead while the others report nothing, and a read that
        fails reports nothing, the next one reporting ``gap=RECONNECT`` after a back-off.
        """
        due = {cursor: cursor._due for cursor in self._cursors if cursor._due is not None}
        positions = {cursor: cursor._position for cursor in self._cursors}
        try:
            if not due:
                return await self._follow(block)
            await self._take_positions(due)
        except asyncio.CancelledError:
            # Cut off between two rounds of a backlog, a cursor has moved past
            # entries it never returned: it reads in full next time.
            for cursor, position in positions.items():
                if cursor._position != position and cursor._due is None:
                    cursor._due = FullRead.RECONNECT
            raise
        except Exception as exc:
            await self._back_off(exc)
            due = {}
        return {cursor.key: StreamRead(gap=due.get(cursor)) for cursor in self._cursors}

    async def _take_positions(self, reasons: Mapping[StreamCursor, FullRead]) -> dict[str, str]:
        """Move each cursor to where its full read starts, in one round trip; the positions by key."""
        infos = await self._redis.stream_infos([cursor.key for cursor in reasons])
        return {
            cursor.key: cursor._take(info, reason)
            for (cursor, reason), info in zip(reasons.items(), infos, strict=True)
        }

    async def _follow(self, block: float) -> dict[str, StreamRead]:
        """Read every stream from its cursor until no backlog is left, judging each round by the gap rule."""
        wait = _block_ms(self._redis, block)
        entries: dict[str, list[StreamEntry]] = {cursor.key: [] for cursor in self._cursors}
        heads: dict[str, StreamInfo | None] = {}
        gaps: set[str] = set()
        live = self._cursors
        while live:
            pipe = self._redis.client.pipeline(transaction=False)
            pipe.xread(
                {cursor.key: cursor._position or _START_ID for cursor in live}, count=STREAM_READ_COUNT, block=wait
            )
            for cursor in live:
                pipe.xinfo_stream(cursor.key)
            read, *replies = await pipe.execute(raise_on_error=False)
            if isinstance(read, Exception):
                raise read
            found = {
                str(key): [StreamEntry(str(entry_id), dict(values)) for entry_id, values in items]
                for key, items in read or []
            }
            backlog: list[StreamCursor] = []
            for cursor, reply in zip(live, replies, strict=True):
                info = StreamInfo.of(reply)
                if cursor._lost(info):
                    cursor._take(info, FullRead.GAP)
                    gaps.add(cursor.key)
                    continue
                got = found.get(cursor.key, [])
                cursor._advance(got)
                entries[cursor.key] += got
                heads[cursor.key] = info
                if len(got) >= STREAM_READ_COUNT:
                    backlog.append(cursor)
            live, wait = backlog, None
        reads: dict[str, StreamRead] = {}
        for cursor in self._cursors:
            if cursor.key in gaps:
                reads[cursor.key] = StreamRead(gap=FullRead.GAP)
            else:
                cursor._caught_up(entries[cursor.key], heads.get(cursor.key))
                reads[cursor.key] = StreamRead(entries=entries[cursor.key])
        return reads

    async def _back_off(self, error: Exception) -> None:
        """A read failed: count it on every cursor, then wait before the next try."""
        failures = max(cursor._failed(error) for cursor in self._cursors)
        await asyncio.sleep(retry_delay(failures))


# The most entry IDs one REFRESH call checks: the script unpacks them into one
# XCLAIM, and Lua unpacks a few thousand values at most.
REFRESH_BATCH = 500

# Lua: KEYS[1] the stream; ARGV[1] the group, ARGV[2] this consumer, then the
# entry IDs. Resets the idle time of each entry still pending for this
# consumer (XCLAIM JUSTID counts no delivery), so no reclaim takes it while it
# runs, and returns the IDs that are not (all of them when the group is gone).
# An XPENDING error, NOGROUP among them, reads as a table without [1].
_REFRESH_SCRIPT = """
local claim, lost = {'XCLAIM', KEYS[1], ARGV[1], ARGV[2], 0}, {}
for i = 3, #ARGV do
  local p = redis.pcall('XPENDING', KEYS[1], ARGV[1], ARGV[i], ARGV[i], 1)
  if type(p) == 'table' and p[1] and p[1][2] == ARGV[2] then
    claim[#claim + 1] = ARGV[i]
  else
    lost[#lost + 1] = ARGV[i]
  end
end
if #claim > 5 then
  claim[#claim + 1] = 'JUSTID'
  redis.call(unpack(claim))
end
return lost
"""


def is_missing_group(reply: object) -> bool:
    """Whether a reply says the group or its stream does not exist: a wipe, or a stream nobody created yet."""
    return isinstance(reply, ResponseError) and str(reply).startswith("NOGROUP")


class StreamGroup:
    """One consumer of a Redis consumer group on one stream: what it reads, reclaims, keeps claimed and acknowledges."""

    def __init__(self, redis_client: RedisClient, key: str, group: str, consumer: str) -> None:
        self._redis = redis_client
        self.key = key
        self.group = group
        self.consumer = consumer
        # The stream's name in logs: a work stream's name in the System view
        # (``jobs.health``), since every job stream key ends alike; another
        # stream's key.
        self.name = WORK_STREAM_NAMES.get(key, key)
        # Where the next reclaim looks from: the cursor the last one returned,
        # 0-0 once a reclaim reached the end of the group's pending entries.
        self._cursor = _START_ID
        self._refresh_script: Any = None

    async def ensure(self) -> bool:
        """Create the group at the stream's first entry, and the stream when missing; whether it was created now."""
        try:
            await self._redis.client.xgroup_create(self.key, self.group, id=_START_ID, mkstream=True)
        except ResponseError as exc:
            if str(exc).startswith("BUSYGROUP"):
                return False
            raise
        return True

    async def read(self, count: int, block: float = 0.0) -> list[StreamEntry]:
        """Up to ``count`` entries no consumer was given yet, waiting up to ``block`` seconds for one; none without a group.

        A missing group is created again from the stream's first entry, so what was appended meanwhile is delivered.
        """
        try:
            # [[key, [(id, fields), ...]]] (redis-py keeps RESP2's shape), or [] when the wait ran out.
            reply: Any = await self._redis.client.xreadgroup(
                self.group, self.consumer, {self.key: ">"}, count=count, block=_block_ms(self._redis, block)
            )
        except ResponseError as exc:
            if not is_missing_group(exc):
                raise
            await self._recreate()
            return []
        return [StreamEntry(str(entry_id), dict(fields or {})) for _key, items in reply or [] for entry_id, fields in items]

    async def reclaim(self, idle_ms: int, count: int) -> list[StreamEntry]:
        """Take up to ``count`` entries idle ``idle_ms`` or longer from any consumer, this one too, after the last reclaim.

        One call looks at up to ten pending entries per entry of ``count``; the next goes on from there, and wraps at the end.
        """
        try:
            cursor, entries, _deleted = await self._redis.client.xautoclaim(
                self.key, self.group, self.consumer, idle_ms, self._cursor, count=count
            )
        except ResponseError as exc:
            if not is_missing_group(exc):
                raise
            await self._recreate()
            return []
        self._cursor = str(cursor)
        return [StreamEntry(str(entry_id), dict(fields)) for entry_id, fields in entries]

    async def refresh(self, ids: Sequence[str]) -> list[str]:
        """Keep claimed the entries still pending for this consumer, counting no delivery; the IDs that are not.

        An entry another consumer reclaimed is not taken back from it; one deleted while pending leaves the group's
        pending list, and is not reported.
        """
        if self._refresh_script is None:
            self._refresh_script = self._redis.client.register_script(_REFRESH_SCRIPT)
        lost: list[str] = []
        for start in range(0, len(ids), REFRESH_BATCH):
            chunk = ids[start : start + REFRESH_BATCH]
            reply = await self._refresh_script(keys=[self.key], args=[self.group, self.consumer, *chunk])
            lost += [str(entry_id) for entry_id in reply]
        return lost

    async def deliveries(self, ids: Sequence[str]) -> dict[str, int]:
        """How many times each entry pending for this consumer was delivered, in one round trip; others are left out."""
        if not ids:
            return {}
        pipe = self._redis.client.pipeline(transaction=False)
        for entry_id in ids:
            pipe.xpending_range(self.key, self.group, entry_id, entry_id, 1, consumername=self.consumer)
        counts: dict[str, int] = {}
        for reply in await pipe.execute(raise_on_error=False):
            if isinstance(reply, Exception):
                if is_missing_group(reply):
                    continue
                raise reply
            for row in reply:
                counts[str(row["message_id"])] = int(row["times_delivered"])
        return counts

    async def ack(self, ids: Sequence[str]) -> int:
        """Acknowledge and delete entries in one transaction; how many were pending (none when the group is gone)."""
        if not ids:
            return 0
        pipe = self._redis.client.pipeline(transaction=True)
        pipe.xack(self.key, self.group, *ids)
        pipe.xdel(self.key, *ids)
        acked, _deleted = await pipe.execute()
        return int(acked)

    async def delete_consumer_if_idle(self, idle_ms: int) -> int | None:
        """Remove this consumer from the group when nothing is pending for it and it idled ``idle_ms``; None if it stays.

        Returns how many pending entries the removal dropped (see :meth:`prune_idle_consumers`).
        """
        return (await self._prune(idle_ms, self.consumer)).get(self.consumer)

    async def prune_idle_consumers(self, idle_ms: int) -> dict[str, int]:
        """Remove every consumer of the group with nothing pending that idled ``idle_ms``; the entries each removal dropped.

        A removal drops pending entries only in a race with a read, which leaves them never delivered again; a missing
        group or stream has no consumer to remove.
        """
        return await self._prune(idle_ms, None)

    async def _prune(self, idle_ms: int, only: str | None) -> dict[str, int]:
        """Remove the idle consumers with nothing pending, ``only`` that one or any (None); the entries each removal dropped."""
        try:
            consumers = await self._redis.client.xinfo_consumers(self.key, self.group)
        except ResponseError as exc:
            # A missing stream reads as "no such key", a missing group as NOGROUP.
            if is_missing_group(exc) or is_missing_key(exc):
                return {}
            raise
        idle = [
            str(info["name"])
            for info in consumers
            if only in (None, info["name"]) and int(info["pending"]) == 0 and int(info["idle"]) >= idle_ms
        ]
        return {name: int(await self._redis.client.xgroup_delconsumer(self.key, self.group, name)) for name in idle}

    async def _recreate(self) -> None:
        """The group or its stream is gone (a wipe, a stream deleted): create it again, and reclaim from the start."""
        self._cursor = _START_ID
        if await self.ensure():
            logger.warning("Stream consumer group was missing; created again", stream=self.name, group=self.group)


class ChangeLog:
    """Publishes one changes stream entry per committed write; one per process, closed at its stop.

    An entry Redis does not take within the request deadline is kept and
    sent again in the background, one per kind, until it lands.
    """

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis = redis_client
        # Kinds Redis has not taken yet, each with the ref its entry carries:
        # "" once the kind stands for more than one row.
        self._pending: dict[ChangeKind, str] = {}
        # The pending entries a retry is sending now.
        self._sending: dict[ChangeKind, str] = {}
        self._retrying = HeldTasks()
        # Set when an entry lands, so the retry tries at once rather than
        # after its back-off.
        self._landed = asyncio.Event()
        self._closed = False

    @property
    def pending(self) -> frozenset[ChangeKind]:
        """The kinds whose entry Redis has not taken yet."""
        return frozenset(self._pending) | frozenset(self._sending)

    async def record(self, kind: ChangeKind, *ids: str) -> None:
        """Append an entry for a committed write of the rows ``ids``, none for a write of every row; never raises."""
        ref = ref_of(ids)
        try:
            async with self._redis.request_deadline():
                await self._append(self._redis.client, kind, ref)
        except asyncio.CancelledError:
            # The caller went away mid-send: whether Redis took the entry is
            # unknown, and a duplicate is harmless.
            self._keep(kind, ref, None)
            raise
        except Exception as exc:
            self._keep(kind, ref, exc)
            return
        if self._pending:
            self._landed.set()

    async def close(self) -> None:
        """Stop the retry and try once more to send what is pending; what Redis still refuses is logged and lost."""
        self._closed = True
        await self._retrying.cancel()
        if self._pending and not await self._send_pending():
            lost = sorted(kind.value for kind in self._pending)
            logger.warning("Change events not published at shutdown", kinds=lost)

    @staticmethod
    def _append(target: Any, kind: ChangeKind, ref: str) -> Any:
        """One XADD of an entry on ``target``, a client or a pipeline."""
        return target.xadd(
            CHANGES_STREAM_KEY, {KIND_FIELD: kind.value, REF_FIELD: ref}, maxlen=CHANGES_STREAM_MAXLEN, approximate=True
        )

    def _keep(self, kind: ChangeKind, ref: str, error: Exception | None) -> None:
        """Hold an entry Redis did not take, and start the retry unless one runs."""
        reason = error_text(error) if error is not None else "cancelled"
        if self._closed:
            logger.warning("Change events not published at shutdown", kinds=[kind.value], error=reason)
            return
        if not self.pending:
            logger.warning("Change event not published; retrying", kind=kind.value, ref=ref, error=reason)
        self._merge({kind: ref})
        if not self._retrying.busy:
            self._retrying.spawn(self._retry(), name="change_log_retry")

    def _merge(self, entries: Mapping[ChangeKind, str]) -> None:
        """Add entries to the pending ones: one per kind, its ref kept while every entry of the kind names one row."""
        for kind, ref in entries.items():
            self._pending[kind] = ref if self._pending.get(kind, ref) == ref else ""

    async def _retry(self) -> None:
        """Send the pending entries with a back-off, at once when another entry lands, until none is left."""
        failures = 0
        while self._pending:
            self._landed.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(retry_delay(failures + 1)):
                    await self._landed.wait()
            if not await self._send_pending():
                failures += 1

    async def _send_pending(self) -> bool:
        """One entry per pending kind, in one round trip under the request deadline; whether every one landed."""
        batch = self._sending = self._pending
        self._pending = {}
        try:
            async with self._redis.request_deadline():
                pipe = self._redis.client.pipeline(transaction=False)
                for kind, ref in batch.items():
                    self._append(pipe, kind, ref)
                replies = await pipe.execute(raise_on_error=False)
        except asyncio.CancelledError:
            self._merge(batch)
            raise
        except Exception as exc:
            self._merge(batch)
            kinds = sorted(kind.value for kind in batch)
            logger.debug("Change events still not published", kinds=kinds, error=error_text(exc))
            return False
        finally:
            self._sending = {}
        refused = {
            kind: ref for (kind, ref), reply in zip(batch.items(), replies, strict=True) if isinstance(reply, Exception)
        }
        self._merge(refused)
        landed = sorted(kind.value for kind in batch if kind not in refused)
        if landed:
            logger.info("Change events published after a retry", kinds=landed)
        return not refused
