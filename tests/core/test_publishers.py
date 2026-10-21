# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the batch publishers (api.core.publishers): a window a cycle, what Redis did not confirm first, one send at a time.

The item publisher stands for the family: its window is a buffer of notes appended to a test stream, on the session's
Redis; a test that pauses Redis waits the pause out.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from api.core.job_stats import job_stats
from api.core.publishers import ItemPublisher
from api.db.redis import BatchChannel, RedisClient
from tests.waits import running, until

NAME: Any = "toy_publisher"
KEY = "o6:stream:test-notes"


class Note(BaseModel):
    text: str


@pytest.fixture(autouse=True)
def _clean_job_stats() -> Any:
    job_stats.reset()
    yield
    job_stats.reset()


@pytest.fixture
async def connect(test_redis_url: str) -> AsyncIterator[Callable[..., Any]]:
    """Connect a client of its own; every one is closed at the end."""
    made: list[RedisClient] = []

    async def client(url: str = test_redis_url, **options: Any) -> RedisClient:
        made.append(RedisClient(url, **options))
        await made[-1].connect()
        return made[-1]

    yield client
    for made_client in made:
        await made_client.close()


def notes(redis_client: RedisClient | None, *, interval: float = 60, max_items: int = 0, max_pending: int = 1000) -> ItemPublisher[Note]:
    return ItemPublisher[Note](
        redis_client,
        name=NAME,
        interval=interval,
        channel=BatchChannel.SIGHTING_APPENDS,
        key=KEY,
        max_items=max_items,
        max_pending=max_pending,
    )


def push(publisher: ItemPublisher[Note], *texts: str) -> None:
    for text in texts:
        publisher.push(Note(text=text))


async def _stream(redis_client: RedisClient) -> list[str]:
    """The text of every note in the stream, oldest first."""
    return [json.loads(fields["item"])["text"] for _entry_id, fields in await redis_client.client.xrange(KEY)]


class TestBatchPublisher:
    async def test_each_interval_sends_the_window_and_one_with_nothing_to_send_is_idle(
        self, redis_client: RedisClient
    ) -> None:
        publisher = notes(redis_client, interval=0.1)
        push(publisher, "a", "b")
        async with running(publisher):
            await until(lambda: publisher.published == 2)
            push(publisher, "c")
            await until(lambda: publisher.published == 3)
            await until(lambda: (stats := job_stats.get(NAME)) is not None and stats.idle_runs >= 1)

        assert await _stream(redis_client) == ["a", "b", "c"]
        stats = job_stats.get(NAME)
        assert stats is not None and stats.working_runs == 2 and stats.interval_seconds == 0.1

    async def test_a_send_redis_did_not_answer_goes_first_next_time_and_its_items_land_once(
        self, redis_client: RedisClient, connect: Any
    ) -> None:
        """What was gathered meanwhile waits behind it, so the stream keeps the order things happened in."""
        publisher = notes(await connect(socket_timeout=0.3))
        push(publisher, "first")
        await redis_client.client.client_pause(600)
        with pytest.raises(Exception):  # noqa: B017 - the reply bound, whatever redis-py calls it
            await publisher.flush()
        assert publisher.owed == 1 and publisher.pending == 1
        push(publisher, "second")
        await asyncio.sleep(0.4)  # Redis answers again

        # The owed part as it was, then, once it landed, the window.
        assert await publisher.flush() == 2
        assert await _stream(redis_client) == ["first", "second"]
        assert (publisher.published, publisher.pending) == (2, 0)

    async def test_one_send_at_a_time_however_many_ask(self, redis_client: RedisClient) -> None:
        """A second flush waits for the first: sent at once, its newer part could run first and the older be skipped."""
        publisher = notes(redis_client)
        push(publisher, "first")
        await redis_client.client.client_pause(300)
        first = asyncio.create_task(publisher.flush())
        await asyncio.sleep(0.05)
        push(publisher, "second")
        second = asyncio.create_task(publisher.flush())

        assert await asyncio.gather(first, second) == [1, 1]
        assert await _stream(redis_client) == ["first", "second"]

    async def test_a_stop_ends_the_loop_and_sends_the_window_once_more(self, redis_client: RedisClient) -> None:
        publisher = notes(redis_client, interval=60)
        task = asyncio.create_task(publisher.run())
        push(publisher, "last words")
        await asyncio.sleep(0.05)

        await publisher.stop()
        await asyncio.wait_for(task, timeout=1)

        assert await _stream(redis_client) == ["last words"] and publisher.pending == 0

    async def test_a_stop_that_cannot_reach_redis_logs_what_is_lost_and_never_raises(self, connect: Any) -> None:
        publisher = notes(await connect("redis://127.0.0.1:1/0", request_timeout=0.2))
        push(publisher, "a", "b")

        with patch("api.core.publishers.logger") as log:
            await publisher.stop()

        assert [call.args for call in log.warning.call_args_list] == [
            ("Batch not sent at stop",),
            ("Batch items lost at stop",),
        ]
        assert log.warning.call_args.kwargs["pending"] == 2

    async def test_a_part_whose_script_failed_is_dropped_and_counted(self, redis_client: RedisClient) -> None:
        await redis_client.client.set(KEY, "not a stream")
        publisher = notes(redis_client)
        push(publisher, "a", "b")

        assert await publisher.flush() == 0
        assert (publisher.published, publisher.dropped, publisher.pending) == (0, 2, 0)

    async def test_without_redis_nothing_is_sent_and_the_window_is_kept(self) -> None:
        publisher = notes(None)
        push(publisher, "a")

        assert await publisher.flush() == 0
        await publisher.stop()
        assert publisher.pending == 1


class TestItemPublisher:
    async def test_a_full_buffer_lets_its_oldest_item_go_and_counts_it(self, redis_client: RedisClient) -> None:
        publisher = notes(redis_client, max_pending=3)
        push(publisher, "1", "2", "3", "4", "5")

        assert (publisher.pending, publisher.dropped) == (3, 2)
        await publisher.flush()
        assert await _stream(redis_client) == ["3", "4", "5"]

    async def test_what_the_stream_s_cap_trims_is_counted_dropped(self, redis_client: RedisClient) -> None:
        publisher = notes(redis_client, max_items=10)
        push(publisher, *[str(i) for i in range(500)])

        assert await publisher.flush() == 500
        length = await redis_client.client.xlen(KEY)
        # Approximately: Redis trims whole nodes of a hundred entries, oldest first.
        assert length < 200 and publisher.dropped == 500 - length
        assert (await _stream(redis_client))[-1] == "499"

    async def test_items_are_serialised_as_they_are_sent_an_entry_each(self, redis_client: RedisClient) -> None:
        publisher = notes(redis_client)
        note = Note(text="first")
        publisher.push(note)
        note.text = "changed before the send"  # serialised off the request path, at the cut

        await publisher.flush()

        assert await _stream(redis_client) == ["changed before the send"]
