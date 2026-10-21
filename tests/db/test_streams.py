# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Redis streams (api.db.streams): cursors and their gap rule, the reader, consumer groups, the change log.

Against the session's Redis, emptied per test, with retry waits shrunk to hundredths of a second; a test that pauses
Redis waits the pause out, since the whole session shares the container.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.asyncio.client import Pipeline

import api.db.streams as streams
from api.core.roles import JobKind
from api.db.redis import (
    CHANGES_STREAM_KEY,
    JOB_STREAM_KEY,
    OBSERVATION_STREAM_KEY,
    REPLY_TIMEOUT_SECONDS,
    RUNTIME_STREAM_KEY,
    RedisClient,
)
from api.db.streams import (
    REF_FIELD,
    REFRESH_BATCH,
    STREAM_BLOCK_SECONDS,
    ChangeKind,
    ChangeLog,
    FullRead,
    RuntimeSignal,
    StreamCursor,
    StreamEntry,
    StreamGroup,
    StreamRead,
    StreamReader,
    retry_delay,
)
from api.models.system import StreamState
from tests.waits import until

KEY = CHANGES_STREAM_KEY


@pytest.fixture(autouse=True)
def _quick_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries a hundredth of a second apart rather than a second."""
    monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.01)
    monkeypatch.setattr(streams, "RETRY_MAX_SECONDS", 0.05)


async def _add(
    redis_client: RedisClient, entry_id: str = "*", key: str = KEY, kind: str = "proxy", ref: str = ""
) -> str:
    """Append one entry, as a writer would; its ID."""
    return str(await redis_client.client.xadd(key, {"kind": kind, "ref": ref}, id=entry_id))


class Followed:
    """One stream followed alone: a cursor and a reader of it, as a follower of that stream holds them."""

    def __init__(self, redis_client: RedisClient, key: str = KEY) -> None:
        self.cursor = StreamCursor(redis_client, key)
        self._reader = StreamReader(redis_client, [self.cursor])

    async def start(self, reason: FullRead = FullRead.START) -> str:
        """Take the position a full read starts from."""
        return (await self._reader.start(reason))[self.cursor.key]

    async def read(self, block: float) -> StreamRead:
        """One read of the stream after the position, waiting up to ``block`` seconds."""
        return (await self._reader.read(block))[self.cursor.key]


async def _started(redis_client: RedisClient, key: str = KEY) -> Followed:
    stream = Followed(redis_client, key)
    await stream.start()
    return stream


def test_a_read_never_waits_as_long_as_a_reply_may_take() -> None:
    """The client's reply bound would cut a healthy wait, and every quiet stream would read as a failed read."""
    assert STREAM_BLOCK_SECONDS < REPLY_TIMEOUT_SECONDS


def test_the_back_off_doubles_up_to_its_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 1.0)
    monkeypatch.setattr(streams, "RETRY_MAX_SECONDS", 30.0)

    assert [retry_delay(failures) for failures in range(1, 8)] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_the_back_off_stays_at_its_cap_through_an_outage_of_any_length() -> None:
    """Past 1024 failures in a row, nine hours at the cap, an unbounded exponent would overflow and end the loop."""
    assert retry_delay(2000) == streams.RETRY_MAX_SECONDS


class TestEntries:
    def test_a_job_entry_names_its_key_and_one_without_a_key_names_none(self) -> None:
        """An entry deleted while pending comes back with no fields at all."""
        assert StreamEntry("1-0", {"key": "proxy-1"}).key == "proxy-1"
        assert StreamEntry("2-0", {"item": "{}"}).key == ""
        assert StreamEntry("3-0", {}).key == ""

    def test_a_traffic_block_set_or_lifted_is_told_on_the_runtime_stream_as_traffic(self) -> None:
        """The kind the traffic jobs append and the feed builder reads: a value on the wire, not a name in the code."""
        assert RuntimeSignal.TRAFFIC == "traffic"

    async def test_a_signal_appends_its_entry_on_the_callers_transaction_and_caps_the_stream(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pipe = redis_client.client.pipeline(transaction=False)
        for n in range(250):
            pipe.xadd(RUNTIME_STREAM_KEY, {"kind": "quarantine", "ref": f"proxy-{n}"})
        await pipe.execute()
        monkeypatch.setattr(streams, "RUNTIME_STREAM_MAXLEN", 1)

        pipe = redis_client.client.pipeline(transaction=True)
        RuntimeSignal.QUARANTINE.append(pipe, "proxy-1")
        assert await redis_client.client.xlen(RUNTIME_STREAM_KEY) == 250
        await pipe.execute()

        # Trimmed a whole stream node at a time, the newest entry kept and read as the feed builder reads it.
        assert await redis_client.client.xlen(RUNTIME_STREAM_KEY) < 251
        (newest,) = await redis_client.client.xrevrange(RUNTIME_STREAM_KEY, count=1)
        entry = StreamEntry(str(newest[0]), dict(newest[1]))
        assert (entry.kind, entry.fields[REF_FIELD]) == (RuntimeSignal.QUARANTINE.value, "proxy-1")

    async def test_a_signal_about_several_rows_names_none(self, redis_client: RedisClient) -> None:
        pipe = redis_client.client.pipeline(transaction=True)
        RuntimeSignal.QUARANTINE.append(pipe, "proxy-1", "proxy-2")
        await pipe.execute()

        (newest,) = await redis_client.client.xrevrange(RUNTIME_STREAM_KEY, count=1)
        assert StreamEntry(str(newest[0]), dict(newest[1])).fields[REF_FIELD] == ""


class TestFollowing:
    async def test_a_cursor_starts_at_the_last_entry_and_reads_what_follows(self, redis_client: RedisClient) -> None:
        await _add(redis_client, "1000-0")
        stream = Followed(redis_client)

        assert await stream.start() == "1000-0"
        await _add(redis_client, "2000-0", kind="connector", ref="c1")
        read = await stream.read(block=0)

        assert read.gap is None
        assert [(entry.id, entry.kind, entry.fields[REF_FIELD]) for entry in read.entries] == [("2000-0", "connector", "c1")]
        assert stream.cursor.position == "2000-0"
        assert await stream.read(block=0) == StreamRead()

    async def test_a_waiting_read_wakes_as_an_entry_lands(self, redis_client: RedisClient) -> None:
        stream = await _started(redis_client)
        reading = asyncio.create_task(stream.read(block=10))
        await asyncio.sleep(0.2)

        landed = time.monotonic()
        await _add(redis_client)
        read = await reading

        assert len(read.entries) == 1
        assert time.monotonic() - landed < 1

    async def test_a_backlog_is_read_whole_without_waiting_between_batches(self, redis_client: RedisClient) -> None:
        stream = await _started(redis_client)  # no stream yet
        pipe = redis_client.client.pipeline(transaction=False)
        for i in range(2500):
            pipe.xadd(KEY, {"kind": "proxy", "ref": str(i)})
        await pipe.execute()

        started = time.monotonic()
        read = await stream.read(block=10)

        assert [entry.fields[REF_FIELD] for entry in read.entries] == [str(i) for i in range(2500)]
        assert time.monotonic() - started < 2
        assert stream.cursor.state().entries == 2500

    async def test_a_read_given_no_time_returns_at_once(self, redis_client: RedisClient) -> None:
        stream = await _started(redis_client)
        started = time.monotonic()

        assert await stream.read(block=0) == StreamRead()
        assert time.monotonic() - started < 0.2

    async def test_a_wait_is_capped_at_the_block_limit(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A consumer whose next duty is an hour away still reads again within the limit."""
        monkeypatch.setattr(streams, "STREAM_BLOCK_SECONDS", 0.3)
        stream = await _started(redis_client)
        started = time.monotonic()

        assert await stream.read(block=3600) == StreamRead()
        assert 0.25 < time.monotonic() - started < 2

    async def test_a_wait_stays_under_the_client_s_reply_bound(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """A wait the bound cut short would read as a failed read, and a full read after it, every time."""
        client = RedisClient(test_redis_url, socket_timeout=0.6)
        await client.connect()
        try:
            stream = await _started(client)
            started = time.monotonic()

            assert await stream.read(block=3600) == StreamRead()
            assert time.monotonic() - started < 0.6
            assert await stream.read(block=0) == StreamRead()
            assert stream.cursor.state().full_reads == {"start": 1}
        finally:
            await client.close()


class TestGapRule:
    """A read that may have missed entries says so, and the consumer reads in full."""

    async def _gap_after(self, stream: Followed) -> None:
        with patch("api.db.streams.logger") as log:
            assert await stream.read(block=0) == StreamRead(gap=FullRead.GAP)
        assert log.warning.call_args.args == ("Stream gap; reading in full",)

    async def test_a_trim_past_the_position_is_a_gap(self, redis_client: RedisClient) -> None:
        await _add(redis_client, "1000-0")
        stream = await _started(redis_client)
        for ms in (2000, 3000, 4000):
            await _add(redis_client, f"{ms}-0")
        await redis_client.client.xtrim(KEY, maxlen=1, approximate=False)  # 2000-0 and 3000-0 are gone unread

        await self._gap_after(stream)
        assert stream.cursor.position == "4000-0"  # where the full read starts

    async def test_a_stream_gone_is_a_gap(self, redis_client: RedisClient) -> None:
        """A wipe: whatever was published since the position may be gone with it."""
        await _add(redis_client, "1000-0")
        stream = await _started(redis_client)
        await redis_client.client.delete(KEY)

        await self._gap_after(stream)
        assert stream.cursor.position == "0-0"

    async def test_a_stream_recreated_below_the_position_is_a_gap(self, redis_client: RedisClient) -> None:
        """A wipe, then a clock behind: every new entry would read as one already handled."""
        await _add(redis_client, "5000-0")
        stream = await _started(redis_client)
        await redis_client.client.delete(KEY)
        await _add(redis_client, "1000-0")

        await self._gap_after(stream)
        assert stream.cursor.position == "1000-0"

    async def test_a_stream_recreated_above_the_position_is_a_gap(self, redis_client: RedisClient) -> None:
        await _add(redis_client, "1000-0")
        stream = await _started(redis_client)
        await redis_client.client.delete(KEY)
        await _add(redis_client, "5000-0")

        await self._gap_after(stream)
        assert stream.cursor.position == "5000-0"

    async def test_a_trim_before_the_first_read_of_a_stream_that_was_missing_is_a_gap(
        self, redis_client: RedisClient
    ) -> None:
        stream = await _started(redis_client)  # 0-0
        for ms in (1000, 2000, 3000):
            await _add(redis_client, f"{ms}-0")
        await redis_client.client.xtrim(KEY, maxlen=1, approximate=False)

        await self._gap_after(stream)

    async def test_an_emptied_stream_is_a_gap(self, redis_client: RedisClient) -> None:
        await _add(redis_client, "1000-0")
        stream = await _started(redis_client)
        await _add(redis_client, "2000-0")
        await redis_client.client.xtrim(KEY, maxlen=0, approximate=False)

        await self._gap_after(stream)
        assert stream.cursor.position == "2000-0"

    async def test_a_trim_below_the_position_is_no_gap(self, redis_client: RedisClient) -> None:
        for ms in (1000, 2000, 3000):
            await _add(redis_client, f"{ms}-0")
        stream = await _started(redis_client)
        await _add(redis_client, "4000-0")
        await redis_client.client.xtrim(KEY, maxlen=2, approximate=False)  # 3000-0 and 4000-0 left

        read = await stream.read(block=0)

        assert read.gap is None
        assert [entry.id for entry in read.entries] == ["4000-0"]

    async def test_a_stream_nobody_writes_is_no_gap_and_costs_no_full_read(self, redis_client: RedisClient) -> None:
        """The runtime stream of an install without rate or traffic limits."""
        stream = await _started(redis_client, RUNTIME_STREAM_KEY)

        for _ in range(3):
            assert await stream.read(block=0) == StreamRead()
        assert stream.cursor.state().full_reads == {"start": 1}


class TestFullReads:
    async def test_the_position_is_taken_before_the_full_read(self, redis_client: RedisClient) -> None:
        """An entry published while the full read runs is read again afterwards, never missed."""
        await _add(redis_client, "1000-0")
        stream = Followed(redis_client)
        await stream.start()
        # The consumer's full read runs here, while a write publishes.
        published = await _add(redis_client, "2000-0")

        assert [entry.id for entry in (await stream.read(block=0)).entries] == [published]

    async def test_after_a_full_read_of_a_missing_stream_the_next_read_waits(self, redis_client: RedisClient) -> None:
        """Rather than reading in full again and again: a read of a missing stream waits for its first entry."""
        stream = await _started(redis_client)
        started = time.monotonic()

        assert await stream.read(block=0.3) == StreamRead()
        assert time.monotonic() - started >= 0.25
        assert stream.cursor.state().full_reads == {"start": 1}

    async def test_a_cursor_never_started_reports_the_start_on_its_first_read(self, redis_client: RedisClient) -> None:
        await _add(redis_client, "1000-0")
        stream = Followed(redis_client)
        started = time.monotonic()

        assert await stream.read(block=10) == StreamRead(gap=FullRead.START)
        assert time.monotonic() - started < 1
        assert stream.cursor.position == "1000-0"

    async def test_a_reconcile_takes_the_position_again(self, redis_client: RedisClient) -> None:
        stream = await _started(redis_client)
        await _add(redis_client, "2000-0")

        assert await stream.start(FullRead.RECONCILE) == "2000-0"

        assert await stream.read(block=0) == StreamRead()  # the full read covered it
        assert stream.cursor.state().full_reads == {"start": 1, "reconcile": 1}

    async def test_a_read_that_failed_leads_to_a_full_read(self, redis_client: RedisClient) -> None:
        """Redis answered an error, or the connection broke: it may have restarted empty meanwhile."""
        await _add(redis_client, "1000-0")
        stream = await _started(redis_client)
        await redis_client.client.delete(KEY)
        await redis_client.client.set(KEY, "not a stream")

        with patch("api.db.streams.logger") as log:
            assert await stream.read(block=0) == StreamRead()
            assert await stream.read(block=0) == StreamRead()
        assert [call.args for call in log.warning.call_args_list] == [("Stream read failed",)]  # once per streak

        await redis_client.client.delete(KEY)
        await _add(redis_client, "3000-0")
        assert await stream.read(block=0) == StreamRead(gap=FullRead.RECONNECT)
        assert stream.cursor.position == "3000-0"
        assert stream.cursor.state().full_reads == {"start": 1, "reconnect": 1}

    async def test_a_read_failed_without_text_is_logged_by_its_type(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare TimeoutError has no text, so the log line names its type."""
        stream = await _started(redis_client)
        pipe = MagicMock(spec=Pipeline)
        pipe.execute = AsyncMock(side_effect=TimeoutError)
        monkeypatch.setattr(redis_client.client, "pipeline", lambda **_: pipe)

        with patch("api.db.streams.logger") as log:
            assert await stream.read(block=0) == StreamRead()

        log.warning.assert_called_once_with("Stream read failed", stream="changes", error="TimeoutError")

    async def test_a_connection_dropped_while_waiting_leads_to_a_full_read(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        client = RedisClient(test_redis_url)
        await client.connect()
        try:
            stream = await _started(client)
            connection = await client.client.client_id()
            reading = asyncio.create_task(stream.read(block=10))
            await asyncio.sleep(0.2)
            await redis_client.client.client_kill_filter(_id=connection)

            assert await reading == StreamRead()
            assert await stream.read(block=0) == StreamRead(gap=FullRead.RECONNECT)
        finally:
            await client.close()

    async def test_a_failing_read_waits_longer_each_time(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.05)
        monkeypatch.setattr(streams, "RETRY_MAX_SECONDS", 0.2)
        stream = await _started(redis_client)
        await redis_client.client.set(KEY, "not a stream")
        waits: list[float] = []
        for _ in range(4):
            started = time.monotonic()
            await stream.read(block=0)
            waits.append(time.monotonic() - started)

        # 0.05 s, doubled each time, up to 0.2 s; a wait never ends early, and may end a little late.
        assert 0.045 <= waits[0] < 0.095
        assert 0.095 <= waits[1] < 0.18
        assert 0.195 <= waits[2] < 0.3
        assert 0.195 <= waits[3] < 0.3


class TestStreamReader:
    async def test_two_streams_in_one_read_each_from_its_own_position(self, redis_client: RedisClient) -> None:
        await _add(redis_client, "1000-0")
        cursors = [StreamCursor(redis_client, CHANGES_STREAM_KEY), StreamCursor(redis_client, RUNTIME_STREAM_KEY)]
        reader = StreamReader(redis_client, cursors)
        await reader.start()  # the runtime stream not written yet
        await _add(redis_client, "2000-0")
        await _add(redis_client, key=RUNTIME_STREAM_KEY, kind="quarantine", ref="p1")

        reads = await reader.read(block=10)

        assert [entry.id for entry in reads[CHANGES_STREAM_KEY].entries] == ["2000-0"]
        assert [(entry.kind, entry.fields[REF_FIELD]) for entry in reads[RUNTIME_STREAM_KEY].entries] == [("quarantine", "p1")]
        assert reads[CHANGES_STREAM_KEY].gap is None and reads[RUNTIME_STREAM_KEY].gap is None

    async def test_a_gap_on_one_stream_leaves_the_other_s_entries(self, redis_client: RedisClient) -> None:
        await _add(redis_client, "1000-0")
        cursors = [StreamCursor(redis_client, CHANGES_STREAM_KEY), StreamCursor(redis_client, RUNTIME_STREAM_KEY)]
        reader = StreamReader(redis_client, cursors)
        await reader.start()
        await redis_client.client.delete(CHANGES_STREAM_KEY)
        await _add(redis_client, key=RUNTIME_STREAM_KEY, kind="traffic")

        reads = await reader.read(block=0)

        assert reads[CHANGES_STREAM_KEY] == StreamRead(gap=FullRead.GAP)
        assert [entry.kind for entry in reads[RUNTIME_STREAM_KEY].entries] == ["traffic"]

    async def test_a_cursor_owed_its_full_read_reports_it_at_once_and_the_other_reads_next_time(
        self, redis_client: RedisClient
    ) -> None:
        changes = (await _started(redis_client, CHANGES_STREAM_KEY)).cursor
        runtime = StreamCursor(redis_client, RUNTIME_STREAM_KEY)  # never started
        await _add(redis_client, "2000-0")

        reader = StreamReader(redis_client, [changes, runtime])
        first = await reader.read(block=10)
        second = await reader.read(block=0)

        assert first == {CHANGES_STREAM_KEY: StreamRead(), RUNTIME_STREAM_KEY: StreamRead(gap=FullRead.START)}
        assert [entry.id for entry in second[CHANGES_STREAM_KEY].entries] == ["2000-0"]

    async def test_a_start_takes_every_position_in_one_round_trip(self, redis_client: RedisClient) -> None:
        await _add(redis_client, "1000-0")
        changes = StreamCursor(redis_client, CHANGES_STREAM_KEY)
        runtime = StreamCursor(redis_client, RUNTIME_STREAM_KEY)  # not written yet

        positions = await StreamReader(redis_client, [changes, runtime]).start(FullRead.RECONCILE)

        assert positions == {CHANGES_STREAM_KEY: "1000-0", RUNTIME_STREAM_KEY: "0-0"}
        assert changes.state().full_reads == runtime.state().full_reads == {"reconcile": 1}

    def test_a_reader_takes_distinct_streams_on_its_own_client(self, redis_client: RedisClient) -> None:
        with pytest.raises(ValueError):
            StreamReader(redis_client, [StreamCursor(redis_client, KEY), StreamCursor(redis_client, KEY)])
        with pytest.raises(ValueError):
            StreamReader(redis_client, [StreamCursor(RedisClient("redis://redis.invalid"), KEY)])


class TestCutReads:
    """A read its caller cuts off (a lead that ended, a stop) loses nothing it never returned."""

    @staticmethod
    def _second_round_hangs(client: RedisClient, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        """Make the client's second pipeline of a read wait forever; the rounds sent so far, by count."""
        rounds = [0]
        original = client.client.pipeline

        def pipeline(*args: object, **kwargs: object) -> object:
            pipe = original(*args, **kwargs)  # type: ignore[arg-type]
            execute = pipe.execute

            async def gated(*a: object, **k: object) -> object:
                rounds[0] += 1
                if rounds[0] == 2:
                    await asyncio.Event().wait()
                return await execute(*a, **k)  # type: ignore[arg-type]

            pipe.execute = gated  # type: ignore[method-assign]
            return pipe

        monkeypatch.setattr(client.client, "pipeline", pipeline)
        return rounds

    async def test_a_read_cut_between_two_rounds_of_a_backlog_reads_in_full_next_time(
        self, redis_client: RedisClient, test_redis_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first round moved the position past entries the read never returned."""
        monkeypatch.setattr(streams, "STREAM_READ_COUNT", 2)
        client = RedisClient(test_redis_url)
        await client.connect()
        try:
            stream = await _started(client)
            for ms in range(1000, 6000, 1000):
                await _add(redis_client, f"{ms}-0")
            with monkeypatch.context() as patched:
                rounds = self._second_round_hangs(client, patched)
                reading = asyncio.create_task(stream.read(block=0))
                await until(lambda: rounds[0] == 2)
                reading.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await reading

            assert await stream.read(block=0) == StreamRead(gap=FullRead.RECONNECT)
            assert stream.cursor.position == "5000-0"
        finally:
            await client.close()

    async def test_a_waiting_read_cut_off_follows_on_from_its_position(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """Nothing moved: the next read follows on, with no full read for the System page to count."""
        client = RedisClient(test_redis_url)
        await client.connect()
        try:
            stream = await _started(client)
            reading = asyncio.create_task(stream.read(block=10))
            await asyncio.sleep(0.2)
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading
            await _add(redis_client, "1000-0")

            assert [entry.id for entry in (await stream.read(block=0)).entries] == ["1000-0"]
            assert stream.cursor.state().full_reads == {"start": 1}
        finally:
            await client.close()


GROUP_KEY = "o6:stream:test-work"
GROUP = "workers"


def _group(redis_client: RedisClient, consumer: str = "c1") -> StreamGroup:
    return StreamGroup(redis_client, GROUP_KEY, GROUP, consumer)


async def _work(redis_client: RedisClient, *items: str) -> list[str]:
    """Append work entries, an ``item`` each, as a publisher would; their IDs."""
    return [str(await redis_client.client.xadd(GROUP_KEY, {"item": item})) for item in items]


async def _pending(redis_client: RedisClient) -> dict[str, tuple[str, int]]:
    """The group's pending entries: ID -> (consumer, deliveries)."""
    rows = await redis_client.client.xpending_range(GROUP_KEY, GROUP, "-", "+", 1000)
    return {row["message_id"]: (row["consumer"], row["times_delivered"]) for row in rows}


class TestStreamGroup:
    async def test_a_group_starts_at_the_stream_s_first_entry(self, redis_client: RedisClient) -> None:
        """What producers appended before any consumer existed is delivered all the same."""
        first, second = await _work(redis_client, "a", "b")
        group = _group(redis_client)

        assert await group.ensure()
        assert not await group.ensure()  # BUSYGROUP: there already
        assert [(entry.id, entry.item) for entry in await group.read(10)] == [(first, "a"), (second, "b")]

    async def test_ensure_creates_a_missing_stream(self, redis_client: RedisClient) -> None:
        assert await _group(redis_client).ensure()
        assert await redis_client.client.xlen(GROUP_KEY) == 0

    async def test_a_read_without_a_group_creates_it_and_the_next_read_is_given_everything(
        self, redis_client: RedisClient
    ) -> None:
        await _work(redis_client, "a")
        group = _group(redis_client)

        assert await group.read(10) == []
        assert [entry.item for entry in await group.read(10)] == ["a"]

    async def test_each_entry_goes_to_one_consumer(self, redis_client: RedisClient) -> None:
        await _work(redis_client, "a", "b", "c")
        one, other = _group(redis_client, "c1"), _group(redis_client, "c2")
        await one.ensure()

        taken = [entry.item for entry in await one.read(2)] + [entry.item for entry in await other.read(2)]

        assert taken == ["a", "b", "c"]
        assert {consumer for consumer, _ in (await _pending(redis_client)).values()} == {"c1", "c2"}

    async def test_a_waiting_read_wakes_as_an_entry_lands(self, redis_client: RedisClient) -> None:
        group = _group(redis_client)
        await group.ensure()
        reading = asyncio.create_task(group.read(10, block=10))
        await asyncio.sleep(0.2)

        landed = time.monotonic()
        await _work(redis_client, "a")

        assert [entry.item for entry in await reading] == ["a"]
        assert time.monotonic() - landed < 1

    async def test_a_wait_stays_under_the_client_s_reply_bound(self, redis_client: RedisClient, test_redis_url: str) -> None:
        client = RedisClient(test_redis_url, socket_timeout=0.6)
        await client.connect()
        try:
            group = _group(client)
            await group.ensure()
            started = time.monotonic()

            assert await group.read(10, block=3600) == []
            assert time.monotonic() - started < 0.6
        finally:
            await client.close()

    async def test_a_wipe_under_a_waiting_read_ends_it_and_the_group_is_made_again(
        self, redis_client: RedisClient
    ) -> None:
        """Redis ends a blocked group read with NOGROUP when its stream goes: the consumer reads again at once."""
        group = _group(redis_client)
        await group.ensure()
        reading = asyncio.create_task(group.read(10, block=10))
        await asyncio.sleep(0.2)

        started = time.monotonic()
        with patch("api.db.streams.logger") as log:
            await redis_client.client.flushdb()
            assert await reading == []
        assert time.monotonic() - started < 1
        assert log.warning.call_args.args == ("Stream consumer group was missing; created again",)
        assert log.warning.call_args.kwargs == {"stream": GROUP_KEY, "group": GROUP}
        await _work(redis_client, "after the wipe")
        assert [entry.item for entry in await group.read(10)] == ["after the wipe"]

    def test_a_work_stream_is_named_as_the_system_view_names_it_and_another_by_its_key(
        self, redis_client: RedisClient
    ) -> None:
        """Every job stream key ends alike, so its kind names it in the logs."""
        named = {
            key: StreamGroup(redis_client, key, GROUP, "c1").name
            for key in (JOB_STREAM_KEY.format(kind=JobKind.SYNC), OBSERVATION_STREAM_KEY, GROUP_KEY)
        }

        assert list(named.values()) == ["jobs.sync", "observations", GROUP_KEY]

    async def test_a_reclaim_takes_entries_idle_past_the_timeout_from_any_consumer(self, redis_client: RedisClient) -> None:
        ids = await _work(redis_client, "a", "b")
        dead, live = _group(redis_client, "dead"), _group(redis_client, "live")
        await dead.ensure()
        await dead.read(2)
        await asyncio.sleep(0.15)

        assert await live.reclaim(10_000, 10) == []  # not idle long enough
        assert [entry.id for entry in await live.reclaim(100, 10)] == ids
        assert {consumer for consumer, _ in (await _pending(redis_client)).values()} == {"live"}

    async def test_a_reclaim_follows_its_cursor_past_entries_that_are_not_idle(self, redis_client: RedisClient) -> None:
        """One call looks at ten pending entries per unit of its count: without the cursor, these would never be reached."""
        items = [str(i) for i in range(150)]
        await _work(redis_client, *items)
        dead_ids = await _work(redis_client, *[f"dead {i}" for i in range(10)])
        busy, dead, me = _group(redis_client, "busy"), _group(redis_client, "dead"), _group(redis_client, "me")
        await busy.ensure()
        busy_ids = [entry.id for entry in await busy.read(150)]
        await dead.read(10)
        await asyncio.sleep(0.15)
        await busy.refresh(busy_ids)

        assert await me.reclaim(100, 10) == []  # the first hundred are busy's, and not idle
        assert [entry.id for entry in await me.reclaim(100, 10)] == dead_ids

    async def test_a_refresh_keeps_this_consumer_s_entries_and_names_the_rest(self, redis_client: RedisClient) -> None:
        """The idle time starts again and no delivery is counted; another consumer's entry is not taken back from it."""
        mine_id, theirs_id = await _work(redis_client, "mine", "theirs")
        mine, theirs = _group(redis_client, "c1"), _group(redis_client, "c2")
        await mine.ensure()
        await mine.read(1)
        await theirs.read(1)
        await asyncio.sleep(0.2)

        assert await mine.refresh([mine_id, theirs_id, "9-9"]) == [theirs_id, "9-9"]
        rows = {row["message_id"]: row for row in await redis_client.client.xpending_range(GROUP_KEY, GROUP, "-", "+", 10)}
        assert rows[mine_id]["time_since_delivered"] < 100
        assert rows[mine_id]["times_delivered"] == 1
        assert rows[theirs_id]["consumer"] == "c2" and rows[theirs_id]["time_since_delivered"] >= 200

    async def test_a_refresh_after_a_wipe_names_every_entry(self, redis_client: RedisClient) -> None:
        mine = _group(redis_client)
        (entry_id,) = await _work(redis_client, "a")
        await mine.ensure()
        await mine.read(1)
        await redis_client.client.flushdb()

        assert await mine.refresh([entry_id]) == [entry_id]

    async def test_a_refresh_of_more_entries_than_one_call_takes_keeps_them_all(self, redis_client: RedisClient) -> None:
        ids = await _work(redis_client, *[str(i) for i in range(REFRESH_BATCH + 20)])
        mine = _group(redis_client)
        await mine.ensure()
        await mine.read(len(ids))
        await asyncio.sleep(0.2)

        assert await mine.refresh(ids) == []
        rows = await redis_client.client.xpending_range(GROUP_KEY, GROUP, "-", "+", len(ids))
        assert max(row["time_since_delivered"] for row in rows) < 200

    async def test_deliveries_count_each_delivery_of_this_consumer_s_entries(self, redis_client: RedisClient) -> None:
        first, second = await _work(redis_client, "a", "b")
        mine = _group(redis_client)
        await mine.ensure()
        await mine.read(2)
        await mine.reclaim(0, 1)  # a reclaim counts a delivery, even of its own entry

        assert await mine.deliveries([first, second, "9-9"]) == {first: 2, second: 1}
        assert await _group(redis_client, "other").deliveries([first]) == {}
        assert await mine.deliveries([]) == {}

    async def test_an_acknowledgement_also_deletes_the_entry(self, redis_client: RedisClient) -> None:
        first, second = await _work(redis_client, "a", "b")
        mine = _group(redis_client)
        await mine.ensure()
        await mine.read(2)

        assert await mine.ack([first]) == 1
        assert await mine.ack([first]) == 0  # sent again: nothing left to do
        assert [entry_id for entry_id, _ in await redis_client.client.xrange(GROUP_KEY)] == [second]
        assert list(await _pending(redis_client)) == [second]
        await redis_client.client.flushdb()
        assert await mine.ack([second]) == 0  # no stream, no group: no error

    async def test_a_consumer_leaves_the_group_only_idle_and_with_nothing_pending(self, redis_client: RedisClient) -> None:
        (entry_id,) = await _work(redis_client, "a")
        mine = _group(redis_client)
        await mine.ensure()
        await mine.read(1)

        assert await mine.delete_consumer_if_idle(0) is None  # an entry pending
        await mine.ack([entry_id])
        assert await mine.delete_consumer_if_idle(60_000) is None  # not idle long enough
        assert await mine.delete_consumer_if_idle(0) == 0
        assert await redis_client.client.xinfo_consumers(GROUP_KEY, GROUP) == []
        assert await mine.delete_consumer_if_idle(0) is None  # gone already
        await redis_client.client.flushdb()
        assert await mine.delete_consumer_if_idle(0) is None  # no stream

    async def test_a_prune_removes_every_consumer_idle_with_nothing_pending_whoever_asks(
        self, redis_client: RedisClient
    ) -> None:
        await _work(redis_client, "a")
        asking = _group(redis_client, "hygiene")
        await asking.ensure()
        await _group(redis_client, "busy").read(1)
        for name in ("stopped-1", "stopped-2"):
            await redis_client.client.xgroup_createconsumer(GROUP_KEY, GROUP, name)
        await asyncio.sleep(0.15)
        await redis_client.client.xgroup_createconsumer(GROUP_KEY, GROUP, "fresh")

        assert await asking.prune_idle_consumers(100) == {"stopped-1": 0, "stopped-2": 0}

        consumers = {str(info["name"]) for info in await redis_client.client.xinfo_consumers(GROUP_KEY, GROUP)}
        assert consumers == {"busy", "fresh"}
        await redis_client.client.flushdb()
        assert await asking.prune_idle_consumers(0) == {}  # no stream, no group


class TestStreamState:
    async def test_the_state_says_where_the_cursor_is_and_how_it_read(self, redis_client: RedisClient) -> None:
        stream = Followed(redis_client)
        cursor = stream.cursor
        assert cursor.name == "changes"
        assert cursor.state() == StreamState()

        await stream.start()
        await _add(redis_client, "1000-0")
        await _add(redis_client, "1500-0")
        await stream.read(block=0)
        state = cursor.state()

        assert (state.position, state.entries, state.full_reads) == ("1500-0", 2, {"start": 1})
        assert state.last_full_read_at is not None
        # The read came back to two entries half a second apart, by Redis's clock.
        assert state.lag_seconds == pytest.approx(0.5)
        await stream.read(block=0)
        assert cursor.state().lag_seconds == 0.0


def _slow_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A back-off far longer than the test, so only what the test does can send a pending entry."""
    monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 30.0)
    monkeypatch.setattr(streams, "RETRY_MAX_SECONDS", 30.0)


async def _changes(redis_client: RedisClient) -> list[tuple[str, str]]:
    """The changes stream's entries, (kind, ref), oldest first."""
    return [(entry["kind"], entry["ref"]) for _entry_id, entry in await redis_client.client.xrange(CHANGES_STREAM_KEY)]


class TestChangeLog:
    async def test_each_record_is_one_entry_naming_its_row_only_when_it_has_one(
        self, redis_client: RedisClient, change_log: ChangeLog
    ) -> None:
        await change_log.record(ChangeKind.PROXY, "p1")
        await change_log.record(ChangeKind.ALL)
        await change_log.record(ChangeKind.PROXY, "p1", "p2")

        assert await _changes(redis_client) == [("proxy", "p1"), ("all", ""), ("proxy", "")]
        assert change_log.pending == frozenset()

    async def test_the_stream_is_trimmed_as_it_is_written(
        self, redis_client: RedisClient, change_log: ChangeLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(streams, "CHANGES_STREAM_MAXLEN", 10)
        for _ in range(500):
            await change_log.record(ChangeKind.PROXY)

        # Approximately: Redis trims whole nodes of a hundred entries.
        assert await redis_client.client.xlen(CHANGES_STREAM_KEY) < 250

    async def test_an_entry_redis_does_not_take_never_holds_up_the_write_and_lands_once_it_answers(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """Pending entries of one kind go as one, with a ref only while they name one row."""
        client = RedisClient(test_redis_url, request_timeout=0.2)
        await client.connect()
        log = ChangeLog(client)
        try:
            await redis_client.client.client_pause(1500, all=False)  # writes wait
            started = time.monotonic()
            with patch("api.db.streams.logger") as logged:
                await log.record(ChangeKind.PROXY, "p1")
                await log.record(ChangeKind.PROXY, "p2")
                await log.record(ChangeKind.CONNECTOR, "c1")
            assert time.monotonic() - started < 1.2
            assert log.pending == {ChangeKind.PROXY, ChangeKind.CONNECTOR}
            assert [call.args for call in logged.warning.call_args_list] == [("Change event not published; retrying",)]

            await until(lambda: not log.pending)

            assert sorted(await _changes(redis_client)) == [("connector", "c1"), ("proxy", "")]
        finally:
            await log.close()
            await client.close()

    async def test_an_entry_that_lands_sends_the_pending_ones_without_waiting_out_the_back_off(
        self, redis_client: RedisClient, test_redis_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _slow_retries(monkeypatch)
        client = RedisClient(test_redis_url, request_timeout=0.2)
        await client.connect()
        log = ChangeLog(client)
        try:
            await redis_client.client.client_pause(400, all=False)
            await log.record(ChangeKind.GEO_SETTINGS)
            await asyncio.sleep(0.5)  # Redis answers again

            await log.record(ChangeKind.PROJECT, "p1")
            await until(lambda: not log.pending, timeout=2)

            assert sorted(await _changes(redis_client)) == [("geo_settings", ""), ("project", "p1")]
        finally:
            await log.close()
            await client.close()

    async def test_an_entry_kept_as_the_retry_ends_starts_the_next_retry(
        self, redis_client: RedisClient, change_log: ChangeLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The retry that sent the last pending entry has ended but is not dropped yet: it must not hold the next one off."""
        send = ChangeLog._send_pending
        kept: list[str] = []

        async def send_then_keep_one(log: ChangeLog) -> bool:
            landed = await send(log)
            if not kept:
                kept.append("p2")
                # Runs once the retry has ended, before its done callbacks.
                asyncio.get_running_loop().call_soon(log._keep, ChangeKind.PROXY, "p2", ConnectionError("paused"))
            return landed

        monkeypatch.setattr(ChangeLog, "_send_pending", send_then_keep_one)
        change_log._keep(ChangeKind.PROXY, "p1", ConnectionError("paused"))

        await until(lambda: bool(kept) and not change_log.pending, timeout=2)

        assert await _changes(redis_client) == [("proxy", "p1"), ("proxy", "p2")]

    async def test_close_sends_what_is_pending(
        self, redis_client: RedisClient, test_redis_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _slow_retries(monkeypatch)
        client = RedisClient(test_redis_url, request_timeout=0.2)
        await client.connect()
        log = ChangeLog(client)
        try:
            await redis_client.client.client_pause(400, all=False)
            await log.record(ChangeKind.DESCRIPTOR, "d1")
            assert log.pending == {ChangeKind.DESCRIPTOR}
            await asyncio.sleep(0.5)

            await log.close()

            assert await _changes(redis_client) == [("descriptor", "d1")]
            assert log.pending == frozenset()
        finally:
            await client.close()

    async def test_close_stops_the_retry(self, change_log: ChangeLog, monkeypatch: pytest.MonkeyPatch) -> None:
        """A retry left running after close would send through a client the process is closing."""
        _slow_retries(monkeypatch)
        change_log._keep(ChangeKind.PROXY, "p1", ConnectionError("paused"))
        await asyncio.sleep(0)  # the retry starts and waits out its back-off
        assert change_log._retrying.busy

        await change_log.close()

        assert not change_log._retrying.busy and change_log.pending == frozenset()

    async def test_a_record_never_raises_and_what_is_lost_at_close_is_logged(self) -> None:
        client = RedisClient("redis://127.0.0.1:1/0", request_timeout=0.2)
        await client.connect()
        log = ChangeLog(client)
        try:
            await log.record(ChangeKind.WIREGUARD)
            assert log.pending == {ChangeKind.WIREGUARD}

            with patch("api.db.streams.logger") as logged:
                await log.close()
                await log.record(ChangeKind.OPENVPN)

            assert [call.args for call in logged.warning.call_args_list] == [
                ("Change events not published at shutdown",),
                ("Change events not published at shutdown",),
            ]
            assert log.pending == {ChangeKind.WIREGUARD}
        finally:
            await client.close()
