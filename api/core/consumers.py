# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Stream consumers: loops whose work arrives on Redis streams, followed by every process or shared through a group.

Broadcast followers read every entry from their own position; group consumers share a work stream's entries among the
worker processes, each entry going to one of them.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from api.core.job_stats import job_stats
from api.core.logging import error_text
from api.core.loops import Loop
from api.core.tasks import HeldTasks
from api.db.redis import WORKER_GROUP
from api.db.streams import (
    STREAM_BLOCK_SECONDS,
    FullRead,
    StreamCursor,
    StreamEntry,
    StreamGroup,
    StreamReader,
    retry_delay,
)

if TYPE_CHECKING:
    from api.core.leadership import Lease
    from api.core.workers import WorkerName
    from api.db.redis import RedisClient
    from api.models.system import StreamState

logger = structlog.get_logger()

# How long an entry may sit in a group consumer's hands without a refresh
# before another takes it, in seconds: the runs of a worker that crashed go to
# the others after this.
CLAIM_TIMEOUT_SECONDS = 60.0
# How often a group consumer with room looks for entries idle past the claim
# timeout, in seconds, before it reads new ones.
RECLAIM_CHECK_SECONDS = 20.0
# How often a group consumer resets the idle time of every entry it holds, in
# seconds: a third of the claim timeout, so two refreshes in a row can fail
# before an entry is taken from it.
CLAIM_REFRESH_SECONDS = 20.0
# How many deliveries an entry gets before it is given up rather than handed
# out again: a job that kills its worker does not go round the fleet, and a
# batch the ingest keeps refusing is given up rather than retried forever.
MAX_DELIVERIES = 5
# How long a stopping group consumer waits for the batches it settles before
# it cancels them, in seconds: a cloud create under way can save its row, and
# one cut short keeps its creating entry, as a crash does.
STOP_GRACE_SECONDS = 30.0
# Entry IDs a log line names at most.
_LOGGED_IDS = 5


def decode_items[T](entries: Sequence[StreamEntry], parse: Callable[[str], T]) -> tuple[list[T], list[StreamEntry]]:
    """Each work entry's ``item`` parsed, in order, and the entries whose item was missing or would not parse."""
    parsed: list[T] = []
    malformed: list[StreamEntry] = []
    for entry in entries:
        try:
            if not entry.item:
                raise ValueError("no item")
            parsed.append(parse(entry.item))
        except (ValueError, TypeError):
            malformed.append(entry)
    return parsed, malformed


class StreamConsumer(Loop):
    """A loop whose work arrives on Redis streams: it waits on a read of them rather than on a timer."""

    def __init__(self, redis_client: RedisClient, *, name: WorkerName, lease: Lease | None = None) -> None:
        super().__init__(name=name, lease=lease)
        self._redis = redis_client

    def _block(self) -> float:
        """Seconds the next read may wait for an entry: until the next duty, capped by the read itself."""
        duty = self._next_duty()
        return STREAM_BLOCK_SECONDS if duty is None else max(0.0, duty)


class BroadcastFollower(StreamConsumer):
    """Follows streams from its own positions, held in memory, every process alike; reads in full when it may have missed entries.

    It reads in full at the start, at each new lead, on a gap, after a failed read and every ``reconcile_interval``,
    its positions taken first each time, so an entry published meanwhile is read again rather than missed.
    """

    def __init__(
        self,
        redis_client: RedisClient,
        keys: Sequence[str],
        *,
        name: WorkerName,
        reconcile_interval: float,
        lease: Lease | None = None,
    ) -> None:
        super().__init__(redis_client, name=name, lease=lease)
        self._cursors = [StreamCursor(redis_client, key) for key in keys]
        self._reader = StreamReader(redis_client, self._cursors)
        self._reconcile_interval = reconcile_interval
        # When the next reconcile is due (monotonic): an interval after the last full read was owed.
        self._reconcile_at = time.monotonic() + reconcile_interval
        # Whether a full read is owed.
        self._owed = False

    def streams(self) -> dict[str, StreamState]:
        """How far this follower has read each of its streams, by stream name."""
        return {cursor.name: cursor.state() for cursor in self._cursors}

    @abstractmethod
    def _on_entries(self, key: str, entries: list[StreamEntry]) -> bool:
        """Note what new entries of the stream at ``key`` ask for, without I/O; True when they ask for the full read."""

    @abstractmethod
    async def _full_read(self) -> None:
        """Read again everything this follower derives from its streams; raising leaves it owed, for after the back-off."""

    async def _act(self) -> bool:
        """Do what is owed: by default the full read; whether anything was done."""
        if not self._owed:
            return False
        await self._full_read()
        self._owed = False
        return True

    async def _on_term(self, term: int) -> None:
        """A new lead reads in full, its positions taken first: the leader before may have left anything unread."""
        await super()._on_term(term)
        await self._reader.start(FullRead.START)
        self._owe(FullRead.START)

    async def _wait(self) -> None:
        """Read every stream until an entry or the next duty, and note what the entries and gaps ask for."""
        for key, read in (await self._reader.read(self._block())).items():
            if read.gap is not None:
                self._owe(read.gap)
            elif read.entries and self._on_entries(key, read.entries):
                self._owe(None)

    async def _cycle(self) -> bool:
        """Take the positions for the reconcile when it is due, then do what is owed."""
        if time.monotonic() >= self._reconcile_at:
            await self._reader.start(FullRead.RECONCILE)
            self._owe(FullRead.RECONCILE)
        return await self._act()

    def _next_duty(self) -> float:
        """Seconds until the reconcile; 0 while a full read is owed."""
        return 0.0 if self._owed else max(0.0, self._reconcile_at - time.monotonic())

    def _owe(self, reason: FullRead | None) -> None:
        """Owe the full read; one whose positions were taken anew (a FullRead reason) covers the reconcile, which comes an interval later."""
        if reason is not None:
            self._reconcile_at = time.monotonic() + self._reconcile_interval
        self._owed = True


@dataclass(eq=False)
class _Batch:
    """Entries a group consumer settles together, one read or one cut of a reclaim: run, or given up."""

    entries: list[StreamEntry]
    # Delivered more than MAX_DELIVERIES times: given up rather than run again.
    given_up: bool = False


class GroupConsumer(StreamConsumer):
    """One per worker process and work stream, in the workers' group: each entry goes to one worker with room, a dead worker's to another.

    It keeps the entries of the batches it settles claimed, acknowledges each batch once, takes entries idle past the
    claim timeout, and gives up an entry delivered more than ``MAX_DELIVERIES`` times rather than run it again.

    Args:
        count: the most entries one read takes, which run as one batch.
        concurrency: how many batches it settles at once; times ``count``, its room when none runs.
        claim_timeout: seconds an entry idles before another consumer takes it; None for ``CLAIM_TIMEOUT_SECONDS``.
    """

    def __init__(
        self,
        redis_client: RedisClient,
        key: str,
        *,
        name: WorkerName,
        count: int,
        concurrency: int = 1,
        claim_timeout: float | None = None,
    ) -> None:
        super().__init__(redis_client, name=name)
        self._group = StreamGroup(redis_client, key, WORKER_GROUP, redis_client.writer_id)
        self._count = count
        self._concurrency = concurrency
        self._claim_timeout = claim_timeout
        # Whether the group was made sure of since the start.
        self._joined = False
        # The batches being settled, and their tasks.
        self._batches: set[_Batch] = set()
        self._tasks = HeldTasks()
        # Entries it holds that a refresh found in another consumer's hands: logged once, refreshed no more.
        self._taken: set[str] = set()
        # When the next refresh and the next reclaim check are due, and no new
        # entries are read before _read_at (monotonic): a check at the start
        # takes what a crashed predecessor left.
        self._refresh_at = 0.0
        self._reclaim_at = 0.0
        self._read_at = 0.0
        # Batches whose handler raised in a row: the back-off before the next read.
        self._batch_failures = 0
        # Entries handled, and entries given up or dropped, since start.
        self.consumed = 0
        self.dropped = 0

    @property
    def in_flight(self) -> int:
        """Entries this consumer holds: those of the batches it settles, less any another consumer took."""
        return len(self._held())

    async def stop(self) -> None:
        """End the loop once its cycle under way is done, then settle what it holds and leave the group if nothing is pending; never raises."""
        await super().stop()
        await self._run_ended()
        await self._settle()

    @abstractmethod
    async def _handle(self, entries: list[StreamEntry]) -> None:
        """Handle one batch the group gave this consumer, oldest first; the base completes it once this returns.

        Raising leaves its entries pending, delivered again after the claim timeout.
        """

    async def _give_up(self, entries: list[StreamEntry]) -> None:
        """Settle entries delivered more than ``MAX_DELIVERIES`` times instead of running them; by default nothing.

        The base completes them once this returns; raising leaves them pending, given up again after the claim timeout.
        """

    async def _wait(self) -> None:
        """Read one batch of new entries while there is room and no reclaim check is due, else wait for room or a duty."""
        if not self._may_read():
            await super()._wait()
            return
        await self._join()
        entries = await self._group.read(self._count, self._block())
        if entries:
            self._start(entries)

    async def _cycle(self) -> bool:
        """Refresh what it holds when due, then take entries idle past the claim timeout when due and it has room."""
        await self._join()
        if time.monotonic() >= self._refresh_at:
            await self._refresh()
        if self._reclaim_due() and self._capacity() > 0:
            await self._reclaim()
        return True

    def _count_cycle(self, duration_ms: float, worked: bool) -> None:
        """Nothing: a group consumer's runs are its batches, each timed as it runs."""

    def _next_duty(self) -> float | None:
        """Seconds until the next refresh of what it holds, reclaim check, or end of a read back-off; None while none is timed."""
        now = time.monotonic()
        duties: list[float] = []
        if self._held():
            duties.append(self._refresh_at)
        if self._capacity() > 0:
            duties.append(self._reclaim_at)
            if self._read_at > now:
                duties.append(self._read_at)
        return max(0.0, min(duties) - now) if duties else None

    async def _join(self) -> None:
        """Make sure of the group, from the stream's first entry, before this consumer first uses it."""
        if not self._joined:
            await self._group.ensure()
            self._joined = True

    async def _acknowledge(self, ids: Sequence[str]) -> None:
        """Acknowledge and delete settled entries in one transaction, once; never raises: what Redis refuses stays pending."""
        try:
            await self._group.ack(ids)
        except Exception as exc:
            logger.warning(
                "Stream entries not acknowledged; they run again after the claim timeout",
                loop=self.name,
                count=len(ids),
                error=error_text(exc),
            )

    async def _settle(self) -> None:
        """At stop: give the batches the grace, cancel the rest, then leave the group unless entries are pending for it.

        Entries still pending for it are taken by another consumer after the claim timeout.
        """
        await self._tasks.settle(timeout=STOP_GRACE_SECONDS)
        try:
            dropped = await self._group.delete_consumer_if_idle(0)
        except Exception as exc:
            logger.warning("Stream consumer not removed from its group at stop", loop=self.name, error=error_text(exc))
            return
        if dropped:
            logger.warning("Stream consumer left the group with entries pending", loop=self.name, count=dropped)

    def _held(self) -> set[str]:
        """The IDs of the entries it holds and keeps claimed: those of the batches it settles, less any another consumer took."""
        return self._running() - self._taken

    def _running(self) -> set[str]:
        """The IDs of the entries in the batches being settled."""
        return {entry.id for batch in self._batches for entry in batch.entries}

    def _capacity(self) -> int:
        """How many entries it can take now: a read's worth for each batch it could settle beside those it settles."""
        return (self._concurrency - len(self._batches)) * self._count

    def _may_read(self) -> bool:
        """Whether a read of new entries comes next: room, no back-off, and no reclaim check due."""
        return self._capacity() > 0 and time.monotonic() >= self._read_at and not self._reclaim_due()

    def _reclaim_due(self) -> bool:
        """Whether a reclaim check is due."""
        return time.monotonic() >= self._reclaim_at

    async def _refresh(self) -> None:
        """Reset the idle time of what it holds; an entry another consumer took is logged once and refreshed no more."""
        self._refresh_at = time.monotonic() + CLAIM_REFRESH_SECONDS
        held = sorted(self._held())
        if not held:
            return
        lost = set(await self._group.refresh(held)) - self._taken
        if lost:
            self._taken |= lost
            logger.warning(
                "Stream entries in hand were taken by another consumer", loop=self.name, ids=sorted(lost)[:_LOGGED_IDS]
            )

    async def _reclaim(self) -> None:
        """Take entries idle past the claim timeout, up to its room, following the group's cursor while replies come back full."""
        self._reclaim_at = time.monotonic() + RECLAIM_CHECK_SECONDS
        timeout = CLAIM_TIMEOUT_SECONDS if self._claim_timeout is None else self._claim_timeout
        capacity = self._capacity()
        while capacity > 0 and not self.stopped:
            entries = await self._group.reclaim(int(timeout * 1000), capacity)
            mine = self._running()
            # Its own entries, idle while it still runs them: the reclaim refreshed them.
            self._taken -= {entry.id for entry in entries if entry.id in mine}
            fresh = [entry for entry in entries if entry.id not in mine]
            if fresh:
                await self._take(fresh)
            if len(entries) < capacity:
                return
            capacity = self._capacity()

    async def _take(self, entries: list[StreamEntry]) -> None:
        """Start reclaimed entries in batches: those delivered too often to be given up, the rest to run."""
        counts = await self._group.deliveries([entry.id for entry in entries])
        spent = [entry for entry in entries if counts.get(entry.id, 0) > MAX_DELIVERIES]
        if spent:
            logger.warning(
                "Stream entries given up after repeated deliveries",
                loop=self.name,
                count=len(spent),
                ids=[entry.id for entry in spent[:_LOGGED_IDS]],
            )
        runnable = [entry for entry in entries if counts.get(entry.id, 0) <= MAX_DELIVERIES]
        for given_up, part in ((True, spent), (False, runnable)):
            for start in range(0, len(part), self._count):
                self._start(part[start : start + self._count], given_up=given_up)

    def _start(self, entries: list[StreamEntry], *, given_up: bool = False) -> None:
        """Settle one batch as a held task: its entries are held, and refreshed, until it is settled."""
        if not self._held():
            self._refresh_at = time.monotonic() + CLAIM_REFRESH_SECONDS
        batch = _Batch(entries, given_up)
        self._batches.add(batch)
        self._tasks.spawn(self._run(batch), name=f"{self.name}_batch")

    async def _run(self, batch: _Batch) -> None:
        """Run one batch, or give it up, and complete it once that returns; raising leaves its entries pending.

        A run is filed in the job stats as a run of the loop, failed when it raised; a give-up is none.
        """
        ids = [entry.id for entry in batch.entries]
        try:
            if batch.given_up:
                await self._give_up(batch.entries)
            else:
                with job_stats.track(self.name):
                    await self._handle(batch.entries)
        except Exception as exc:
            self._unsettled(batch, error_text(exc))
        else:
            await self._acknowledge(ids)
            if batch.given_up:
                self.dropped += len(ids)
            else:
                self.consumed += len(ids)
                self._batch_failures = 0
                self._read_at = 0.0
        finally:
            self._batches.discard(batch)
            self._taken -= set(ids)
            self.wake()

    def _unsettled(self, batch: _Batch, error: str) -> None:
        """Log a batch whose handler or give-up raised; a failed run makes reads back off, 1 s doubling to 30 s."""
        if batch.given_up:
            logger.warning(
                "Stream entries not given up; they are given up again after the claim timeout",
                loop=self.name,
                count=len(batch.entries),
                error=error,
            )
            return
        # Reads wait meanwhile: a cause that lasts would otherwise pull every entry into pending at network speed.
        self._batch_failures += 1
        self._read_at = time.monotonic() + retry_delay(self._batch_failures)
        log = logger.warning if self._batch_failures == 1 else logger.debug
        log(
            "Stream batch failed; its entries are delivered again after the claim timeout",
            loop=self.name,
            count=len(batch.entries),
            error=error,
        )
