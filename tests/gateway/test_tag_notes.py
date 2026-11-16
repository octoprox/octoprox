# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A gateway's notes of tagged requests: what each tag's request went through, due in Redis when it changes and once
half the TTL has passed, never once per request; the notes due go in one pipelined write every 100 ms, and Redis failing
never fails a request.

Redis is a recording double here, so a write can be held on the wire while another is due.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

from api.core.workers import WorkerName
from api.db.redis import RedisClient
from api.gateway.tag_notes import TAG_NOTE_WRITE_INTERVAL_SECONDS, TagNotes
from api.geo.verifier import ExitDecision
from api.models.project import ExitReputationMode, Project
from api.models.proxy import Proxy
from api.reputation.tag_note import TagNote
from tests.waits import running, until

PROJECT = Project(id="p1", name="P", username="u", password="pw", exit_reputation=ExitReputationMode.RECORD)
STATIC = Proxy(id="row-1", host="h", port=1, connector_id="pool", metadata={"discovered_ip": "::ffff:203.0.113.7"})
OTHER = Proxy(id="row-2", host="h", port=2, connector_id="pool", metadata={"discovered_ip": "203.0.113.8"})
GATEWAY = Proxy(
    id="gw", host="g", port=1, connector_id="dynamic", metadata={"dynamic_sessions": "true", "discovered_ip": "1.2.3.4"}
)


class FakeRedis:
    """What tag notes ask of Redis: a pipeline of SETs under the request deadline, sent as one write; a write may fail, or
    be held until the test lets it land."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        # Each write that landed: its notes as (key, note, ttl), in pipeline order.
        self.writes: list[list[tuple[str, dict[str, Any], int]]] = []
        self.failing = False
        self.hold: asyncio.Event | None = None
        self.client = self

    @asynccontextmanager
    async def request_deadline(self) -> AsyncIterator[None]:
        yield

    def pipeline(self, transaction: bool = True) -> "FakePipeline":
        assert not transaction
        return FakePipeline(self)

    @property
    def sets(self) -> list[tuple[str, dict[str, Any], int]]:
        """Every note written, in the order they landed."""
        return [written for write in self.writes for written in write]


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._sets: list[tuple[str, str, int]] = []

    def set(self, key: str, value: str, ex: int) -> None:
        self._sets.append((key, value, ex))

    async def execute(self) -> list[bool]:
        redis = self._redis
        if redis.failing:
            raise ConnectionError("Redis went away")
        hold, redis.hold = redis.hold, None
        if hold is not None:
            await hold.wait()
        for key, value, _ttl in self._sets:
            redis.values[key] = value
        redis.writes.append([(key, json.loads(value), ttl) for key, value, ttl in self._sets])
        return [True] * len(self._sets)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def notes(redis: FakeRedis, clock: Clock) -> TagNotes:
    return TagNotes(cast(RedisClient, redis), ttl_seconds=120, clock=clock)


KEY = TagNote.key("p1", "t1")


class TestNoting:
    async def test_a_tagged_request_notes_its_row_and_exit(self, notes: TagNotes, redis: FakeRedis) -> None:
        notes.note(PROJECT, "t1", ExitDecision(STATIC))
        notes.note(PROJECT, "t2", ExitDecision(GATEWAY))
        assert redis.writes == [] and notes.pending == 2

        assert await notes.flush() == 2

        # Both in one write.
        (write,) = redis.writes
        assert sorted(write) == [
            (KEY, {"connector_id": "pool", "proxy_id": "row-1", "ip": "203.0.113.7"}, 120),
            (TagNote.key("p1", "t2"), {"connector_id": "dynamic", "proxy_id": "gw", "ip": None}, 120),
        ]
        # A dynamic gateway's exit is the request's own: its row names none.
        assert TagNote.model_validate_json(redis.values[KEY]).ip == "203.0.113.7"
        assert notes.pending == 0 and await notes.flush() == 0 and len(redis.writes) == 1

    async def test_nothing_without_a_tag_or_for_a_project_that_records_nothing(
        self, notes: TagNotes, redis: FakeRedis
    ) -> None:
        notes.note(PROJECT, None, ExitDecision(STATIC))
        notes.note(PROJECT, "", ExitDecision(STATIC))
        notes.note(PROJECT.model_copy(update={"exit_reputation": ExitReputationMode.OFF}), "t1", ExitDecision(STATIC))
        await notes.flush()

        assert redis.writes == []

    async def test_a_reused_tag_is_written_again_when_its_exit_moves_or_half_the_ttl_passed(
        self, notes: TagNotes, redis: FakeRedis, clock: Clock
    ) -> None:
        """A busy session reusing its tag costs a write per half TTL, not one per request."""
        for _ in range(5):
            notes.note(PROJECT, "t1", ExitDecision(STATIC))
            clock.now += 10
            await notes.flush()
        assert len(redis.sets) == 1

        notes.note(PROJECT, "t1", ExitDecision(OTHER))
        await notes.flush()
        assert [note["proxy_id"] for _key, note, _ttl in redis.sets] == ["row-1", "row-2"]

        clock.now += 61
        notes.note(PROJECT, "t1", ExitDecision(OTHER))
        await notes.flush()
        assert len(redis.sets) == 3

    async def test_a_tag_that_moves_twice_between_writes_is_written_once_where_it_went(
        self, notes: TagNotes, redis: FakeRedis
    ) -> None:
        notes.note(PROJECT, "t1", ExitDecision(STATIC))
        notes.note(PROJECT, "t1", ExitDecision(OTHER))
        await notes.flush()

        assert [(key, note["proxy_id"]) for key, note, _ttl in redis.sets] == [(KEY, "row-2")]

    async def test_a_redis_that_fails_never_fails_the_request(self, notes: TagNotes, redis: FakeRedis, clock: Clock) -> None:
        redis.failing = True
        notes.note(PROJECT, "t1", ExitDecision(STATIC))
        with pytest.raises(ConnectionError):
            await notes.flush()
        assert redis.sets == []

        # Not at the next write: at the tag's next due request.
        redis.failing = False
        notes.note(PROJECT, "t1", ExitDecision(STATIC))
        assert await notes.flush() == 0
        clock.now += 61
        notes.note(PROJECT, "t1", ExitDecision(STATIC))
        await notes.flush()
        assert [key for key, _note, _ttl in redis.sets] == [KEY]

    async def test_a_move_during_a_write_is_written_after_it(self, notes: TagNotes, redis: FakeRedis) -> None:
        """One write at a time: the last to land is where the tag went."""
        redis.hold = held = asyncio.Event()
        notes.note(PROJECT, "t1", ExitDecision(STATIC))
        first = asyncio.create_task(notes.flush())
        await asyncio.sleep(0)
        notes.note(PROJECT, "t1", ExitDecision(OTHER))
        second = asyncio.create_task(notes.flush())
        await asyncio.sleep(0.01)
        assert redis.writes == []
        held.set()
        await asyncio.gather(first, second)

        assert [note["proxy_id"] for _key, note, _ttl in redis.sets] == ["row-1", "row-2"]
        assert TagNote.model_validate_json(redis.values[KEY]).proxy_id == "row-2"


class TestLoop:
    async def test_the_notes_due_are_written_every_interval_in_one_write(self, redis: FakeRedis) -> None:
        notes = TagNotes(cast(RedisClient, redis), interval=0.01)
        assert notes.name == WorkerName.TAG_NOTE_PUBLISHER and TAG_NOTE_WRITE_INTERVAL_SECONDS == 0.1
        async with running(notes):
            notes.note(PROJECT, "t1", ExitDecision(STATIC))
            notes.note(PROJECT, "t2", ExitDecision(OTHER))
            await until(lambda: len(redis.sets) == 2)
            assert len(redis.writes) == 1
            notes.note(PROJECT, "t3", ExitDecision(GATEWAY))
            await until(lambda: len(redis.writes) == 2)

    async def test_stopping_writes_what_is_due(self, redis: FakeRedis) -> None:
        notes = TagNotes(cast(RedisClient, redis), interval=3600)
        async with running(notes):
            await asyncio.sleep(0)
            notes.note(PROJECT, "t1", ExitDecision(STATIC))

        assert [key for key, _note, _ttl in redis.sets] == [KEY] and notes.pending == 0

    async def test_a_stop_redis_refuses_is_logged_and_never_raises(self, redis: FakeRedis) -> None:
        notes = TagNotes(cast(RedisClient, redis), interval=3600)
        notes.note(PROJECT, "t1", ExitDecision(STATIC))
        redis.failing = True

        await notes.stop()

        assert redis.sets == []
