# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the stream consumers (api.core.consumers): broadcast followers, and group consumers in the workers' group.

Toy subclasses run on test keys of the session's Redis, each worker on a client of its own as a process would be, with
the claim timings shrunk to tenths of a second.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from redis.exceptions import ConnectionError as RedisConnectionError

import api.core.consumers as consumers
import api.db.streams as streams
from api.core.consumers import BroadcastFollower, GroupConsumer, decode_items
from api.core.job_stats import job_stats
from api.core.leadership import Lease
from api.db.redis import WORKER_GROUP, RedisClient
from api.db.streams import FullRead, StreamEntry
from tests.core.test_loops import KEY as LEASE_KEY
from tests.core.test_loops import POLL_MS, REFRESH_MS, kept, leases
from tests.waits import running, until

__all__ = ["leases"]  # the fixture, shared with the loop tests

NAME: Any = "toy_consumer"
CHANGES = "o6:stream:test-changes"
WORK = "o6:stream:test-work"
# The claim timeout the toy workers run with, in seconds: three refresh intervals.
CLAIM = 0.6


@pytest.fixture(autouse=True)
def _quick(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Refreshes and reclaim checks every 0.2 s, a 0.3 s stop grace, and back-offs of 0.05 s doubling to 0.1 s."""
    monkeypatch.setattr(consumers, "CLAIM_REFRESH_SECONDS", 0.2)
    monkeypatch.setattr(consumers, "RECLAIM_CHECK_SECONDS", 0.2)
    monkeypatch.setattr(consumers, "STOP_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.05)
    monkeypatch.setattr(streams, "RETRY_MAX_SECONDS", 0.1)
    job_stats.reset()
    yield
    job_stats.reset()


@pytest.fixture
async def connect(test_redis_url: str) -> AsyncIterator[Callable[..., Any]]:
    """Connect a client of its own, as another process would have; every one is closed at the end."""
    made: list[RedisClient] = []

    async def client() -> RedisClient:
        made.append(RedisClient(test_redis_url))
        await made[-1].connect()
        return made[-1]

    yield client
    for made_client in made:
        await made_client.close()


async def _add(redis_client: RedisClient, *items: str, key: str = WORK) -> list[str]:
    """Append work entries, an ``item`` each, as a publisher would; their IDs."""
    return [str(await redis_client.client.xadd(key, {"item": item})) for item in items]


async def _deliver(redis_client: RedisClient, consumer: str, count: int) -> list[str]:
    """Hand ``count`` new entries to ``consumer``, which never acknowledges them: a worker that died holding them."""
    taken = streams.StreamGroup(redis_client, WORK, WORKER_GROUP, consumer)
    await taken.ensure()
    return [entry.id for entry in await taken.read(count)]


async def _spend(redis_client: RedisClient, entry_id: str, deliveries: int) -> None:
    """Deliver a pending entry ``deliveries`` times more, as the reclaims of workers that each died running it would."""
    for delivery in range(deliveries):
        await redis_client.client.xclaim(WORK, WORKER_GROUP, f"dead {delivery}", 0, [entry_id])


async def _pending(redis_client: RedisClient) -> dict[str, tuple[str, int]]:
    """The group's pending entries: ID -> (consumer, deliveries)."""
    rows = await redis_client.client.xpending_range(WORK, WORKER_GROUP, "-", "+", 1000)
    return {row["message_id"]: (row["consumer"], row["times_delivered"]) for row in rows}


async def _consumers(redis_client: RedisClient) -> list[str]:
    """The names of the group's consumers."""
    return [info["name"] for info in await redis_client.client.xinfo_consumers(WORK, WORKER_GROUP)]


# --- Broadcast followers ---------------------------------------------------------------------------


class Follower(BroadcastFollower):
    """Notes every full read, with why its positions were taken anew, and every entry; its entries ask for the full read while ``asks`` says so."""

    def __init__(self, redis_client: RedisClient, *, reconcile_interval: float = 3600, lease: Lease | None = None) -> None:
        super().__init__(redis_client, [CHANGES], name=NAME, reconcile_interval=reconcile_interval, lease=lease)
        # Each full read's reason, as the stream's snapshot counted it since the read before; None when entries asked.
        self.reads: list[FullRead | None] = []
        self.read_at: list[float] = []
        self.entries: list[str] = []
        self.asks = True
        self.failures_left = 0
        self._counted: Counter[str] = Counter()

    def _on_entries(self, key: str, entries: list[StreamEntry]) -> bool:
        self.entries += [entry.id for entry in entries]
        return self.asks

    async def _full_read(self) -> None:
        if self.failures_left:
            self.failures_left -= 1
            raise RuntimeError("Postgres is down")
        counted = Counter(self.streams()["test-changes"].full_reads)
        taken, self._counted = counted - self._counted, counted
        self.reads.append(FullRead(next(iter(taken))) if taken else None)
        self.read_at.append(time.monotonic())


class TestBroadcastFollower:
    async def test_it_reads_in_full_at_the_start_and_follows_on_from_there(self, redis_client: RedisClient) -> None:
        (before,) = await _add(redis_client, "x", key=CHANGES)
        follower = Follower(redis_client)
        async with running(follower):
            await until(lambda: follower.reads == [FullRead.START])
            (after,) = await _add(redis_client, "y", key=CHANGES)
            await until(lambda: len(follower.reads) == 2)

        # The start took the position at the entry before it; the entry after it asked for the read.
        assert follower.entries == [after] and follower.reads == [FullRead.START, None]
        assert before < after and follower.streams()["test-changes"].full_reads == {"start": 1}

    async def test_entries_that_ask_for_nothing_read_nothing(self, redis_client: RedisClient) -> None:
        follower = Follower(redis_client)
        follower.asks = False
        async with running(follower):
            await until(lambda: follower.reads == [FullRead.START])
            ids = await _add(redis_client, "x", "y", key=CHANGES)
            await until(lambda: follower.entries == ids)
            await asyncio.sleep(0.1)

        assert follower.reads == [FullRead.START]

    async def test_a_stream_recreated_past_its_position_reads_in_full(self, redis_client: RedisClient) -> None:
        await _add(redis_client, "x", key=CHANGES)
        follower = Follower(redis_client)
        async with running(follower):
            await until(lambda: follower.reads == [FullRead.START])
            await redis_client.client.delete(CHANGES)
            await redis_client.client.xadd(CHANGES, {"item": "after"}, id=f"{int(time.time() * 1000) + 60_000}-0")
            await until(lambda: len(follower.reads) == 2)

        assert follower.reads == [FullRead.START, FullRead.GAP] and follower.entries == []

    async def test_a_read_that_failed_reads_in_full_once_redis_answers(self, redis_client: RedisClient) -> None:
        follower = Follower(redis_client)
        async with running(follower):
            await until(lambda: follower.reads == [FullRead.START])
            await asyncio.sleep(0.1)  # its read waits now
            for connection in await redis_client.client.client_list():
                if connection["cmd"] == "xread":
                    await redis_client.client.client_kill_filter(_id=connection["id"])
            await until(lambda: len(follower.reads) == 2)

        assert follower.reads == [FullRead.START, FullRead.RECONNECT]

    async def test_the_reconcile_reads_in_full_every_interval_its_positions_taken_first(
        self, redis_client: RedisClient
    ) -> None:
        follower = Follower(redis_client, reconcile_interval=0.3)
        async with running(follower):
            await until(lambda: len(follower.reads) == 3)
            (published,) = await _add(redis_client, "x", key=CHANGES)
            await until(lambda: len(follower.reads) == 5)

        assert follower.reads[:3] == [FullRead.START, FullRead.RECONCILE, FullRead.RECONCILE]
        assert follower.streams()["test-changes"].full_reads["reconcile"] >= 3
        assert follower.entries == [published]  # read as it came, whatever the reconciles

    async def test_a_full_read_puts_the_reconcile_off_by_an_interval(self, redis_client: RedisClient) -> None:
        """It read everything already: a reconcile right behind it would read it all again for nothing."""
        follower = Follower(redis_client, reconcile_interval=0.6)
        async with running(follower):
            await until(lambda: follower.reads == [FullRead.START])
            await asyncio.sleep(0.3)
            for connection in await redis_client.client.client_list():
                if connection["cmd"] == "xread":
                    await redis_client.client.client_kill_filter(_id=connection["id"])
            await until(lambda: len(follower.reads) == 3, timeout=2)

        assert follower.reads == [FullRead.START, FullRead.RECONNECT, FullRead.RECONCILE]
        assert follower.read_at[2] - follower.read_at[1] >= 0.55

    async def test_a_full_read_that_fails_stays_owed_and_is_tried_again_after_the_back_off(
        self, redis_client: RedisClient
    ) -> None:
        follower = Follower(redis_client)
        follower.failures_left = 2
        started = time.monotonic()
        async with running(follower):
            await until(lambda: follower.reads == [FullRead.START])

        # 0.05 s after the first failure, 0.1 s after the second.
        assert time.monotonic() - started >= 0.15
        stats = job_stats.get(NAME)
        assert stats is not None and stats.failures == 2 and stats.consecutive_failures == 0

    async def test_a_stop_ends_a_blocked_read_at_once(self, redis_client: RedisClient) -> None:
        follower = Follower(redis_client)
        task = asyncio.create_task(follower.run())
        await until(lambda: follower.reads == [FullRead.START])
        await asyncio.sleep(0.1)  # its read waits now, for up to 20 s

        stopped = time.monotonic()
        await follower.stop()
        await asyncio.wait_for(task, timeout=1)

        assert time.monotonic() - stopped < 0.3

    async def test_a_standby_reads_nothing_and_a_new_lead_reads_in_full(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        await redis_client.client.set(LEASE_KEY, "peer", px=60_000)
        lease = kept(redis_client, leases)
        follower = Follower(redis_client, lease=lease)
        async with running(follower):
            await _add(redis_client, "while standing by", key=CHANGES)
            await asyncio.sleep(POLL_MS * 3 / 1000)
            assert follower.reads == [] and follower.entries == []
            await redis_client.client.delete(LEASE_KEY)
            await until(lambda: follower.reads == [FullRead.START], timeout=1)

        assert follower.entries == []  # the full read covered it

    async def test_a_wipe_ends_its_blocked_read_within_a_refresh_and_the_new_term_reads_in_full(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        """A blocked XREAD outlives a FLUSHALL of its stream: only the end of the lead can cut it short."""
        lease = kept(redis_client, leases)
        follower = Follower(redis_client, lease=lease)
        async with running(follower):
            await until(lambda: follower.reads == [FullRead.START], timeout=1)
            await asyncio.sleep(0.1)  # its read waits now, for up to 20 s
            wiped = time.monotonic()
            await redis_client.client.flushdb()
            await until(lambda: len(follower.reads) == 2, timeout=2)

        assert time.monotonic() - wiped < (REFRESH_MS + POLL_MS) / 1000 + 0.4
        assert follower.reads == [FullRead.START, FullRead.START] and lease.term == 2


# --- Group consumers -------------------------------------------------------------------------------


class Worker(GroupConsumer):
    """Settles batches in ``slots`` slots: a run waits on ``release`` and on its items' ``gates``, and fails while ``failing``.

    A give-up waits on its items' gates too, and fails ``give_up_failures`` times before it settles.
    """

    def __init__(self, redis_client: RedisClient, *, count: int = 10, slots: int = 1, claim: float = CLAIM) -> None:
        super().__init__(redis_client, WORK, name=NAME, count=count, concurrency=slots, claim_timeout=claim)
        self.handled: list[list[str]] = []
        self.handled_at: list[float] = []
        self.given_up_batches: list[list[str]] = []
        self.given_up_at: list[float] = []
        self.release = asyncio.Event()
        self.release.set()
        self.gates: dict[str, asyncio.Event] = {}
        self.failing = False
        self.give_up_failures = 0

    @property
    def items(self) -> list[str]:
        """Every item handed to the handler, in order, a repeat each time it was."""
        return [item for batch in self.handled for item in batch]

    @property
    def given_up(self) -> list[str]:
        """Every item handed to the give-up, in order, a repeat each time it was."""
        return [item for batch in self.given_up_batches for item in batch]

    async def _handle(self, entries: list[StreamEntry]) -> None:
        self.handled.append([entry.item for entry in entries])
        self.handled_at.append(time.monotonic())
        await self.release.wait()
        await self._through_gates(entries)
        if self.failing:
            raise RuntimeError("Postgres is down")

    async def _give_up(self, entries: list[StreamEntry]) -> None:
        self.given_up_batches.append([entry.item for entry in entries])
        self.given_up_at.append(time.monotonic())
        await self._through_gates(entries)
        if self.give_up_failures:
            self.give_up_failures -= 1
            raise RuntimeError("Postgres is down")

    async def _through_gates(self, entries: list[StreamEntry]) -> None:
        for entry in entries:
            if entry.item in self.gates:
                await self.gates[entry.item].wait()


class Plain(GroupConsumer):
    """A group consumer that says only how it handles a batch: it notes the items, then waits on ``release``."""

    def __init__(self, redis_client: RedisClient, *, count: int = 10) -> None:
        super().__init__(redis_client, WORK, name=NAME, count=count, claim_timeout=CLAIM)
        self.items: list[str] = []
        self.release = asyncio.Event()
        self.release.set()

    async def _handle(self, entries: list[StreamEntry]) -> None:
        self.items += [entry.item for entry in entries]
        await self.release.wait()


class TestGroupConsumer:
    async def test_by_default_it_settles_one_batch_at_a_time(self, redis_client: RedisClient, connect: Any) -> None:
        client = await connect()
        worker = Plain(client, count=1)
        worker.release.clear()
        first, _second = await _add(redis_client, "a", "b")
        async with running(worker):
            await until(lambda: worker.items == ["a"])
            await asyncio.sleep(0.2)
            assert worker.items == ["a"] and await _pending(redis_client) == {first: (client.writer_id, 1)}
            worker.release.set()
            await until(lambda: worker.items == ["a", "b"] and worker.in_flight == 0)

        assert worker.consumed == 2 and await redis_client.client.xlen(WORK) == 0

    async def test_by_default_a_give_up_only_completes_the_entries(self, redis_client: RedisClient, connect: Any) -> None:
        """An ingest whose batch was refused five times lets it go: logged, completed and counted dropped."""
        (spent,) = await _add(redis_client, "poison")
        await _deliver(redis_client, "dead", 1)
        await _spend(redis_client, spent, 4)
        await asyncio.sleep(CLAIM + 0.1)
        worker = Plain(await connect())
        async with running(worker):
            await until(lambda: worker.dropped == 1 and worker.in_flight == 0)

        assert worker.items == [] and worker.consumed == 0
        assert await redis_client.client.xlen(WORK) == 0 and await _pending(redis_client) == {}
        assert job_stats.get(NAME) is None  # a give-up is no run of the loop

    async def test_each_entry_is_handled_once_by_one_worker_then_deleted(
        self, redis_client: RedisClient, connect: Any
    ) -> None:
        """Entries appended before any worker existed are delivered too: the group starts at the stream's first."""
        items = [str(i) for i in range(60)]
        await _add(redis_client, *items[:10])
        one, other = Worker(await connect(), count=5), Worker(await connect(), count=5)
        async with running(one), running(other):
            await _add(redis_client, *items[10:])
            await until(lambda: len(one.items) + len(other.items) == 60)
            await until(lambda: one.in_flight == other.in_flight == 0)

        assert sorted(one.items + other.items, key=int) == items
        assert await redis_client.client.xlen(WORK) == 0 and await _pending(redis_client) == {}
        assert one.consumed + other.consumed == 60
        assert one.streams() == {}  # nothing read from a position of its own

    async def test_a_batch_is_completed_once_its_handler_returns_and_left_pending_when_it_raises(
        self, redis_client: RedisClient, connect: Any
    ) -> None:
        """Completed is acknowledged and deleted, so the stream holds only the work in hand."""
        client = await connect()
        worker = Worker(client, claim=60)
        worker.gates["done"] = asyncio.Event()
        async with running(worker):
            (done,) = await _add(redis_client, "done")
            await until(lambda: worker.items == ["done"])
            await asyncio.sleep(0.1)
            assert await redis_client.client.xlen(WORK) == 1
            assert await _pending(redis_client) == {done: (client.writer_id, 1)}
            worker.gates["done"].set()
            await until(lambda: worker.consumed == 1)
            assert await redis_client.client.xlen(WORK) == 0 and await _pending(redis_client) == {}

            worker.failing = True
            (refused,) = await _add(redis_client, "refused")
            await until(lambda: worker.items == ["done", "refused"] and worker.in_flight == 0)
            await asyncio.sleep(0.1)

        assert worker.consumed == 1 and worker.dropped == 0
        assert await redis_client.client.xlen(WORK) == 1
        assert await _pending(redis_client) == {refused: (client.writer_id, 1)}

    async def test_a_dead_worker_s_200_entries_are_taken_in_one_check_by_a_worker_with_20_free_slots(
        self, redis_client: RedisClient, connect: Any
    ) -> None:
        await _add(redis_client, *[str(i) for i in range(200)])
        await _deliver(redis_client, "dead", 200)
        await asyncio.sleep(CLAIM + 0.1)
        worker = Worker(await connect(), count=10, slots=20)
        worker.release.clear()
        async with running(worker):
            # All of it in the check at the start, not a batch a check: well within one check interval.
            assert await until(lambda: len(worker.items) == 200, timeout=2) < consumers.RECLAIM_CHECK_SECONDS
            await asyncio.sleep(0.1)
            assert len(worker.handled) == 20 and all(len(batch) == 10 for batch in worker.handled)
            worker.release.set()
            await until(lambda: worker.in_flight == 0)

        assert await _pending(redis_client) == {}

    async def test_a_dead_worker_s_entries_behind_150_busy_ones_are_reached_through_the_cursor(
        self, redis_client: RedisClient, connect: Any
    ) -> None:
        """One check looks at ten pending entries per entry it may take: only the kept cursor gets past the busy ones."""
        busy_items = [f"busy {i}" for i in range(150)]
        dead_items = [f"dead {i}" for i in range(10)]
        await _add(redis_client, *busy_items, *dead_items)
        busy = await _deliver(redis_client, "busy", 150)
        await _deliver(redis_client, "dead", 10)
        await asyncio.sleep(CLAIM + 0.1)

        async def stay_busy() -> None:
            group = streams.StreamGroup(redis_client, WORK, WORKER_GROUP, "busy")
            while True:
                await group.refresh(busy)
                await asyncio.sleep(0.1)

        refreshing = asyncio.create_task(stay_busy())
        try:
            await asyncio.sleep(0.05)
            worker = Worker(await connect(), count=10)
            async with running(worker):
                await until(lambda: len(worker.items) == 10, timeout=3)
        finally:
            refreshing.cancel()

        assert sorted(worker.items) == sorted(dead_items)

    async def test_its_own_entries_reclaimed_while_it_runs_them_are_not_started_again(
        self, redis_client: RedisClient, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With its refresh late, its own reclaim finds what it runs idle: that counts as a refresh, not a new start."""
        monkeypatch.setattr(consumers, "CLAIM_REFRESH_SECONDS", 60.0)
        monkeypatch.setattr(consumers, "RECLAIM_CHECK_SECONDS", 0.1)
        await _add(redis_client, "long")
        worker = Worker(await connect(), slots=2, claim=0.3)
        worker.release.clear()
        async with running(worker):
            await until(lambda: worker.items == ["long"])
            await asyncio.sleep(1.0)  # three claim timeouts, ten reclaim checks
            worker.release.set()
            await until(lambda: worker.in_flight == 0)

        assert worker.items == ["long"]

    async def test_a_long_batch_stays_with_its_worker_past_the_claim_timeout(
        self, redis_client: RedisClient, connect: Any
    ) -> None:
        """Refreshed meanwhile, so a worker with room never takes it and runs it again."""
        await _add(redis_client, "long 1", "long 2")
        owner = Worker(await connect(), count=1, slots=2)
        owner.release.clear()
        idle = Worker(await connect(), count=1, slots=2)
        async with running(owner):
            # Both slots busy: the owner makes no reclaim check, so only its refresh keeps what it holds.
            await until(lambda: sorted(owner.items) == ["long 1", "long 2"])
            async with running(idle):
                await asyncio.sleep(CLAIM * 2)
                owner.release.set()
                await until(lambda: owner.in_flight == 0, timeout=2)

        assert sorted(owner.items) == ["long 1", "long 2"] and idle.items == []
        assert await _pending(redis_client) == {} and await redis_client.client.xlen(WORK) == 0

    async def test_an_entry_another_worker_took_is_logged_once_and_refreshed_no_more(
        self, redis_client: RedisClient, connect: Any
    ) -> None:
        """A stall let another worker take it: this one never takes it back, and no longer counts it as its own."""
        (entry_id,) = await _add(redis_client, "long")
        worker = Worker(await connect(), claim=60)
        worker.gates["long"] = asyncio.Event()
        with patch("api.core.consumers.logger") as log:
            async with running(worker):
                await until(lambda: worker.items == ["long"])
                await redis_client.client.xclaim(WORK, WORKER_GROUP, "other", 0, [entry_id])
                await until(lambda: worker.in_flight == 0)
                await asyncio.sleep(consumers.CLAIM_REFRESH_SECONDS * 2)
                assert await _pending(redis_client) == {entry_id: ("other", 2)}
                worker.gates["long"].set()
                await until(lambda: worker.consumed == 1)

        assert [call.args for call in log.warning.call_args_list] == [
            ("Stream entries in hand were taken by another consumer",)
        ]

    async def test_a_refused_acknowledgement_leaves_its_entries_pending_and_they_run_again_after_the_claim_timeout(
        self, redis_client: RedisClient, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Acknowledged once: nothing keeps it or sends it again, so the reclaim hands the entries out for a second run."""
        client = await connect()
        worker = Worker(client, count=2)
        refusing = True
        acknowledge = worker._group.ack
        sent: list[list[str]] = []

        async def flaky_ack(ids: Any) -> int:
            sent.append(list(ids))
            if refusing:
                raise RedisConnectionError("Connection reset by peer")
            return await acknowledge(ids)

        monkeypatch.setattr(worker._group, "ack", flaky_ack)
        ids = await _add(redis_client, "a", "b")
        with patch("api.core.consumers.logger") as log:
            async with running(worker):
                await until(lambda: worker.consumed == 2 and worker.in_flight == 0)
                refusing = False
                await asyncio.sleep(CLAIM / 2)
                # Neither sent again nor held: pending for this worker after one delivery, and still on the stream.
                assert sent == [ids]
                assert await _pending(redis_client) == dict.fromkeys(ids, (client.writer_id, 1))
                assert await redis_client.client.xlen(WORK) == 2
                await until(lambda: worker.items == ["a", "b", "a", "b"] and worker.in_flight == 0, timeout=3)

        assert sent == [ids, ids] and worker.consumed == 4
        assert await _pending(redis_client) == {} and await redis_client.client.xlen(WORK) == 0
        assert [call.args for call in log.warning.call_args_list] == [
            ("Stream entries not acknowledged; they run again after the claim timeout",)
        ]
        assert log.warning.call_args.kwargs["count"] == 2

    async def test_an_entry_delivered_more_than_five_times_is_given_up_not_run(
        self, redis_client: RedisClient, connect: Any
    ) -> None:
        spent, fifth = await _add(redis_client, "poison", "fifth")
        await _deliver(redis_client, "dead", 2)
        await _spend(redis_client, spent, 4)
        await _spend(redis_client, fifth, 3)
        # Their next deliveries are the sixth and the fifth: the fifth still runs.
        assert {entry: count for entry, (_, count) in (await _pending(redis_client)).items()} == {spent: 5, fifth: 4}
        await asyncio.sleep(CLAIM + 0.1)
        worker = Worker(await connect())
        with patch("api.core.consumers.logger") as log:
            async with running(worker):
                await until(lambda: worker.items == ["fifth"] and worker.in_flight == 0)

        assert worker.given_up == ["poison"] and worker.dropped == 1
        assert await _pending(redis_client) == {}
        assert [call.args for call in log.warning.call_args_list] == [("Stream entries given up after repeated deliveries",)]

    async def test_a_batch_given_up_is_held_until_its_give_up_returns_then_completed_and_counted_dropped(
        self, redis_client: RedisClient, connect: Any
    ) -> None:
        """Refreshed meanwhile like any batch: a worker with room never takes it and gives it up a second time."""
        (spent,) = await _add(redis_client, "poison")
        await _deliver(redis_client, "dead", 1)
        await _spend(redis_client, spent, 4)
        await asyncio.sleep(CLAIM + 0.1)
        owner, idle = Worker(await connect()), Worker(await connect())
        owner.gates["poison"] = asyncio.Event()
        async with running(owner):
            await until(lambda: owner.given_up == ["poison"])
            async with running(idle):
                await asyncio.sleep(CLAIM * 2)
                assert owner.dropped == 0 and await redis_client.client.xlen(WORK) == 1
                owner.gates["poison"].set()
                await until(lambda: owner.in_flight == 0)

        assert owner.given_up == ["poison"] and owner.items == [] and owner.dropped == 1
        assert idle.given_up == [] and idle.items == []
        assert await redis_client.client.xlen(WORK) == 0 and await _pending(redis_client) == {}

    async def test_a_give_up_that_raises_leaves_its_entries_pending_and_the_rest_of_its_reclaim_still_runs(
        self, redis_client: RedisClient, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The next reclaim gives them up again: a flush that cannot tell whether it committed tries again later."""
        # A run that raised would hold reads back a second; a give-up that raised holds nothing back.
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 1.0)
        monkeypatch.setattr(streams, "RETRY_MAX_SECONDS", 1.0)
        poison, _fine = await _add(redis_client, "poison", "fine")
        await _deliver(redis_client, "dead", 2)
        await _spend(redis_client, poison, 4)
        await asyncio.sleep(CLAIM + 0.1)
        worker = Worker(await connect(), count=1, slots=2)
        worker.gates["poison"] = asyncio.Event()
        worker.give_up_failures = 1
        with patch("api.core.consumers.logger") as log:
            async with running(worker):
                await until(lambda: worker.consumed == 1 and worker.given_up == ["poison"])
                worker.gates["poison"].set()
                await until(lambda: worker.in_flight == 0)
                # Not given up: still on the stream, and pending until a reclaim takes it again.
                assert await redis_client.client.xlen(WORK) == 1 and list(await _pending(redis_client)) == [poison]
                assert worker.dropped == 0
                await _add(redis_client, "next")
                await until(lambda: worker.items == ["fine", "next"], timeout=0.5)
                await until(lambda: worker.given_up == ["poison", "poison"] and worker.in_flight == 0, timeout=3)

        # The entry that could still run ran in the reclaim that took them both, not after the give-up came good.
        assert worker.handled_at[0] < worker.given_up_at[1]
        assert (worker.consumed, worker.dropped) == (2, 1)
        assert await redis_client.client.xlen(WORK) == 0 and await _pending(redis_client) == {}
        assert [call.args for call in log.warning.call_args_list] == [
            ("Stream entries given up after repeated deliveries",),
            ("Stream entries not given up; they are given up again after the claim timeout",),
            ("Stream entries given up after repeated deliveries",),
        ]

    async def test_entries_given_up_together_are_cut_into_batches_of_the_read_size(
        self, redis_client: RedisClient, connect: Any
    ) -> None:
        """A job that settles one entry a run, such as a metrics flush, is given up one entry at a time too."""
        ids = await _add(redis_client, "p1", "p2", "p3")
        await _deliver(redis_client, "dead", 3)
        for entry_id in ids:
            await _spend(redis_client, entry_id, 4)
        await asyncio.sleep(CLAIM + 0.1)
        worker = Worker(await connect(), count=2, slots=2)
        async with running(worker):
            await until(lambda: len(worker.given_up) == 3 and worker.in_flight == 0)

        assert worker.given_up_batches == [["p1", "p2"], ["p3"]] and worker.dropped == 3
        assert await _pending(redis_client) == {}

    async def test_a_batch_that_raises_is_delivered_again_after_the_claim_timeout_and_given_up_after_five(
        self, redis_client: RedisClient, connect: Any
    ) -> None:
        """A failed sighting batch is not lost to a Postgres blip, and a poison one is not retried forever."""
        await _add(redis_client, "refused")
        worker = Worker(await connect(), claim=0.2)
        worker.failing = True
        async with running(worker):
            await until(lambda: worker.given_up == ["refused"] and worker.in_flight == 0, timeout=5)

        assert worker.items == ["refused"] * 5
        assert await redis_client.client.xlen(WORK) == 0 and await _pending(redis_client) == {}

    async def test_reads_back_off_after_a_batch_that_raised(self, redis_client: RedisClient, connect: Any) -> None:
        """Otherwise a cause that lasts would pull the whole stream into pending, a batch at network speed."""
        await _add(redis_client, "a", "b", "c")
        worker = Worker(await connect(), count=1, claim=60)
        worker.failing = True
        async with running(worker):
            await until(lambda: len(worker.items) == 3, timeout=2)

        assert worker.handled == [["a"], ["b"], ["c"]]
        gaps = [later - earlier for earlier, later in zip(worker.handled_at, worker.handled_at[1:], strict=False)]
        # 0.05 s after the first failed batch, 0.1 s after the second.
        assert gaps[0] >= 0.045 and gaps[1] >= 0.095

    async def test_a_stop_leaves_the_group_only_with_nothing_pending(self, redis_client: RedisClient, connect: Any) -> None:
        """What a stopped worker leaves pending, a batch cut at the grace or a read whose reply was lost, goes to another."""
        settled_client, cut_client = await connect(), await connect()
        settled = Worker(settled_client, claim=60)
        async with running(settled):
            await _add(redis_client, "done")
            await until(lambda: settled.consumed == 1 and settled.in_flight == 0)
        assert await _consumers(redis_client) == []

        await _add(redis_client, "cut 1", "cut 2")
        await _deliver(redis_client, cut_client.writer_id, 2)
        cut = Worker(cut_client, claim=60)
        cut.release.clear()
        async with running(cut):
            await _add(redis_client, "running")
            await until(lambda: cut.items == ["running"])

        assert [consumer for consumer, _ in (await _pending(redis_client)).values()] == [cut_client.writer_id] * 3
        assert await _consumers(redis_client) == [cut_client.writer_id]
        assert cut.consumed == 0

    async def test_a_batch_that_finishes_within_the_grace_is_completed_at_stop(
        self, redis_client: RedisClient, connect: Any
    ) -> None:
        worker = Worker(await connect(), claim=60)
        worker.release.clear()
        task = asyncio.create_task(worker.run())
        await _add(redis_client, "nearly done")
        await until(lambda: worker.items == ["nearly done"])

        stopping = asyncio.create_task(worker.stop())
        await asyncio.sleep(0.1)
        worker.release.set()
        await stopping
        await asyncio.wait_for(task, timeout=1)

        assert worker.consumed == 1 and await _pending(redis_client) == {}
        assert await _consumers(redis_client) == []

    async def test_a_stop_never_raises_when_redis_refuses_it(
        self, redis_client: RedisClient, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A batch done within the grace whose acknowledgement is refused, then a refused leave: each logged, none sent twice."""
        worker = Worker(await connect(), claim=60)
        worker.release.clear()
        sent: list[list[str]] = []

        async def refused_ack(ids: Any) -> int:
            sent.append(list(ids))
            raise RedisConnectionError("Connection refused")

        async def refused_leave(idle_ms: int) -> Any:
            raise RedisConnectionError("Connection refused")

        monkeypatch.setattr(worker._group, "ack", refused_ack)
        monkeypatch.setattr(worker._group, "delete_consumer_if_idle", refused_leave)
        (entry_id,) = await _add(redis_client, "a")
        task = asyncio.create_task(worker.run())
        await until(lambda: worker.items == ["a"])

        with patch("api.core.consumers.logger") as log:
            stopping = asyncio.create_task(worker.stop())
            await asyncio.sleep(0.1)
            worker.release.set()
            await stopping
        await asyncio.wait_for(task, timeout=1)

        assert worker.consumed == 1 and sent == [[entry_id]]
        assert [call.args for call in log.warning.call_args_list] == [
            ("Stream entries not acknowledged; they run again after the claim timeout",),
            ("Stream consumer not removed from its group at stop",),
        ]
        # Still pending for it: another worker takes it after the claim timeout.
        assert await _pending(redis_client) == {entry_id: (worker._group.consumer, 1)}

    async def test_a_group_wiped_under_the_workers_is_made_again_and_the_work_goes_on(
        self, redis_client: RedisClient, connect: Any
    ) -> None:
        worker = Worker(await connect())
        async with running(worker):
            await _add(redis_client, "before")
            await until(lambda: worker.items == ["before"] and worker.in_flight == 0)
            await redis_client.client.flushdb()
            await asyncio.sleep(0.1)
            await _add(redis_client, "after")
            await until(lambda: worker.items == ["before", "after"], timeout=2)

    async def test_after_a_redis_outage_the_reclaim_check_runs_on_its_timer(
        self, redis_client: RedisClient, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tried each check interval through the outage, and made within one after it: nothing waits for the holders."""
        # Half a minute: a check held back until the holders refreshed after the outage would come long after the test.
        monkeypatch.setattr(consumers, "CLAIM_REFRESH_SECONDS", 30.0)
        worker = Worker(await connect())
        reclaim, read = worker._group.reclaim, worker._group.read
        outage = [0.0, 0.0]
        tried: list[float] = []
        checked: list[float] = []

        def down() -> bool:
            return outage[0] <= time.monotonic() < outage[1]

        async def flaky_reclaim(idle_ms: int, count: int) -> Any:
            tried.append(time.monotonic())
            if down():
                raise RedisConnectionError("Connection refused")
            entries = await reclaim(idle_ms, count)
            checked.append(time.monotonic())
            return entries

        async def flaky_read(*args: Any, **kwargs: Any) -> Any:
            if down():
                raise RedisConnectionError("Connection refused")
            return await read(*args, **kwargs)

        monkeypatch.setattr(worker._group, "reclaim", flaky_reclaim)
        monkeypatch.setattr(worker._group, "read", flaky_read)
        async with running(worker):
            await until(lambda: len(checked) == 1)  # the check at the start
            outage[:] = [time.monotonic(), time.monotonic() + 0.8]  # four check intervals
            await until(lambda: any(at >= outage[1] for at in checked), timeout=2)

        failed = [at for at in tried if outage[0] <= at < outage[1]]
        first_after = min(at for at in checked if at >= outage[1])
        assert len(failed) >= 2, (tried, outage)
        # An interval apart whether the check before failed or not: its timer, never at once after the back-off.
        gaps = [later - earlier for earlier, later in zip(tried, tried[1:], strict=False)]
        assert min(gaps) >= consumers.RECLAIM_CHECK_SECONDS - 0.02, gaps
        assert first_after - outage[1] < consumers.RECLAIM_CHECK_SECONDS + 0.25, (tried, outage)

    async def test_a_refresh_that_failed_is_made_again_on_its_timer(
        self, redis_client: RedisClient, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not at once when Redis answers again: the claim timeout leaves room for two refreshes in a row to fail."""
        monkeypatch.setattr(consumers, "CLAIM_REFRESH_SECONDS", 0.5)
        await _add(redis_client, "long")
        worker = Worker(await connect(), slots=2, claim=60)
        worker.gates["long"] = asyncio.Event()
        refresh = worker._group.refresh
        calls: list[tuple[float, bool]] = []

        async def flaky_refresh(ids: Any) -> Any:
            if len(calls) == 1:
                calls.append((time.monotonic(), False))
                raise RedisConnectionError("Connection reset by peer")
            calls.append((time.monotonic(), True))
            return await refresh(ids)

        monkeypatch.setattr(worker._group, "refresh", flaky_refresh)
        async with running(worker):
            await until(lambda: worker.items == ["long"])
            await until(lambda: len(calls) >= 3, timeout=3)
            worker.gates["long"].set()

        (failed_at, failed), (refreshed_at, worked) = calls[1], calls[2]
        assert not failed and worked
        assert refreshed_at - failed_at >= consumers.CLAIM_REFRESH_SECONDS - 0.02

    async def test_its_back_off_is_the_loops_whatever_the_refresh_interval(
        self, redis_client: RedisClient, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 s doubling to 30 s, as for every loop: nothing holds it under the refresh interval."""
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.1)
        monkeypatch.setattr(streams, "RETRY_MAX_SECONDS", 30.0)
        worker = Worker(await connect())
        attempts: list[float] = []

        async def unreachable() -> bool:
            attempts.append(time.monotonic())
            raise RedisConnectionError("Connection refused")

        monkeypatch.setattr(worker._group, "ensure", unreachable)
        async with running(worker):
            await until(lambda: len(attempts) >= 4, timeout=2)

        # 0.1 s, 0.2 s, then 0.4 s apart, where a refresh interval is 0.2 s.
        gaps = [later - earlier for earlier, later in zip(attempts, attempts[1:], strict=False)]
        assert gaps[1] >= 0.18 and gaps[2] >= 0.38, gaps

    async def test_its_runs_are_its_batches(self, redis_client: RedisClient, connect: Any) -> None:
        """Reads, refreshes and reclaim checks are no runs: a quiet worker shows none, and each batch it runs is one."""
        worker = Worker(await connect(), count=1, claim=60)
        async with running(worker):
            await asyncio.sleep(0.5)
            assert job_stats.get(NAME) is None
            await _add(redis_client, "a")
            await until(lambda: worker.consumed == 1)
            worker.failing = True
            await _add(redis_client, "b")
            await until(lambda: worker.items == ["a", "b"] and worker.in_flight == 0)

        stats = job_stats.get(NAME)
        assert stats is not None and (stats.runs, stats.failures) == (2, 1)
        assert stats.last_error == "RuntimeError: Postgres is down"

    async def test_a_quiet_worker_whose_reads_failed_stops_failing_once_redis_answers(
        self, redis_client: RedisClient, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No batch comes to end its streak, so the first turn that reads again does; the failures stay on record."""
        worker = Worker(await connect())
        read = worker._group.read
        down = [True]

        async def flaky_read(*args: Any, **kwargs: Any) -> Any:
            if down[0]:
                raise RedisConnectionError("Connection refused")
            return await read(*args, **kwargs)

        monkeypatch.setattr(worker._group, "read", flaky_read)
        async with running(worker):
            await until(lambda: (stats := job_stats.get(NAME)) is not None and stats.consecutive_failures >= 2)
            down[0] = False
            await until(lambda: job_stats.get(NAME).consecutive_failures == 0, timeout=2)  # type: ignore[union-attr]

        stats = job_stats.get(NAME)
        assert stats is not None and stats.failures >= 2 and worker.consumed == 0
        assert stats.last_error == "ConnectionError: Connection refused"

    async def test_a_read_that_works_again_leaves_a_failing_batch_s_streak_standing(
        self, redis_client: RedisClient, connect: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Postgres down through a Redis blip: reading again says nothing of the batches, so only one that runs ends their streak."""
        worker = Worker(await connect(), count=1, claim=60)
        worker.failing = True
        read = worker._group.read
        down = [False]

        async def flaky_read(*args: Any, **kwargs: Any) -> Any:
            if down[0]:
                raise RedisConnectionError("Connection refused")
            return await read(*args, **kwargs)

        monkeypatch.setattr(worker._group, "read", flaky_read)
        async with running(worker):
            await _add(redis_client, "a")
            await until(lambda: (stats := job_stats.get(NAME)) is not None and stats.failures == 1)
            down[0] = True
            await until(lambda: job_stats.get(NAME).consecutive_failures >= 2)  # type: ignore[union-attr]
            down[0] = False
            await until(lambda: worker._failures == 0)

        stats = job_stats.get(NAME)
        assert stats is not None and stats.consecutive_failures == stats.failures >= 2


# --- The small utilities ---------------------------------------------------------------------------


class Sighting(BaseModel):
    proxy: str


def test_items_are_decoded_in_order_and_malformed_ones_set_aside() -> None:
    entries = [
        StreamEntry("1-0", {"item": '{"proxy": "p1"}'}),
        StreamEntry("2-0", {}),
        StreamEntry("3-0", {"item": "not json"}),
        StreamEntry("4-0", {"item": '{"other": 1}'}),
        StreamEntry("5-0", {"item": '{"proxy": "p2"}'}),
    ]

    parsed, malformed = decode_items(entries, Sighting.model_validate_json)

    assert parsed == [Sighting(proxy="p1"), Sighting(proxy="p2")]
    assert [entry.id for entry in malformed] == ["2-0", "3-0", "4-0"]
    # An entry deleted while pending has no item, whatever the parser would make of nothing.
    assert decode_items(entries[:2], str) == (['{"proxy": "p1"}'], [entries[1]])
