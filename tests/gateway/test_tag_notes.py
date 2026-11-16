# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A gateway's notes of tagged requests: what each tag's request went through, written to Redis when it changes and
once half the TTL has passed, never once per request, and never failing a request.

Redis is a recording double here, so a write can be held on the wire while another lands.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

from api.db.redis import RedisClient
from api.gateway.tag_notes import TagNotes
from api.geo.verifier import ExitDecision
from api.models.project import ExitReputationMode, Project
from api.models.proxy import Proxy
from api.reputation.tag_note import TagNote

PROJECT = Project(id="p1", name="P", username="u", password="pw", exit_reputation=ExitReputationMode.RECORD)
STATIC = Proxy(id="row-1", host="h", port=1, connector_id="pool", metadata={"discovered_ip": "::ffff:203.0.113.7"})
OTHER = Proxy(id="row-2", host="h", port=2, connector_id="pool", metadata={"discovered_ip": "203.0.113.8"})
GATEWAY = Proxy(
    id="gw", host="g", port=1, connector_id="dynamic", metadata={"dynamic_sessions": "true", "discovered_ip": "1.2.3.4"}
)


class FakeRedis:
    """What tag notes ask of Redis: SET under the request deadline; a write may be held until the test lets it land."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: list[tuple[str, dict[str, Any], int]] = []
        self.failing = False
        self.hold: asyncio.Event | None = None
        self.client = self

    @asynccontextmanager
    async def request_deadline(self) -> AsyncIterator[None]:
        yield

    async def set(self, key: str, value: str, ex: int) -> None:
        if self.failing:
            raise ConnectionError("Redis went away")
        note = json.loads(value)
        hold, self.hold = self.hold, None
        if hold is not None:
            await hold.wait()
        self.values[key] = value
        self.sets.append((key, note, ex))


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
        await notes.settle()

        assert redis.sets == [
            (KEY, {"connector_id": "pool", "proxy_id": "row-1", "ip": "203.0.113.7"}, 120),
            (TagNote.key("p1", "t2"), {"connector_id": "dynamic", "proxy_id": "gw", "ip": None}, 120),
        ]
        # A dynamic gateway's exit is the request's own: its row names none.
        assert TagNote.model_validate_json(redis.values[KEY]).ip == "203.0.113.7"

    async def test_nothing_without_a_tag_or_for_a_project_that_records_nothing(
        self, notes: TagNotes, redis: FakeRedis
    ) -> None:
        notes.note(PROJECT, None, ExitDecision(STATIC))
        notes.note(PROJECT, "", ExitDecision(STATIC))
        notes.note(PROJECT.model_copy(update={"exit_reputation": ExitReputationMode.OFF}), "t1", ExitDecision(STATIC))
        await notes.settle()

        assert redis.sets == []

    async def test_a_reused_tag_is_written_again_when_its_exit_moves_or_half_the_ttl_passed(
        self, notes: TagNotes, redis: FakeRedis, clock: Clock
    ) -> None:
        """A busy session reusing its tag costs a write per half TTL, not one per request."""
        for _ in range(5):
            notes.note(PROJECT, "t1", ExitDecision(STATIC))
            clock.now += 10
        await notes.settle()
        assert len(redis.sets) == 1

        notes.note(PROJECT, "t1", ExitDecision(OTHER))
        await notes.settle()
        assert [note["proxy_id"] for _key, note, _ttl in redis.sets] == ["row-1", "row-2"]

        clock.now += 61
        notes.note(PROJECT, "t1", ExitDecision(OTHER))
        await notes.settle()
        assert len(redis.sets) == 3

    async def test_a_redis_that_fails_never_fails_the_request(self, notes: TagNotes, redis: FakeRedis, clock: Clock) -> None:
        redis.failing = True
        notes.note(PROJECT, "t1", ExitDecision(STATIC))
        await notes.settle()
        assert redis.sets == []

        # Written again at the tag's next due request.
        redis.failing = False
        clock.now += 61
        notes.note(PROJECT, "t1", ExitDecision(STATIC))
        await notes.settle()
        assert [key for key, _note, _ttl in redis.sets] == [KEY]

    async def test_a_write_that_lands_late_is_followed_by_where_the_tag_went(
        self, notes: TagNotes, redis: FakeRedis
    ) -> None:
        """Writes go out on several connections and may land out of order: the last to land writes the newest exit."""
        redis.hold = held = asyncio.Event()
        notes.note(PROJECT, "t1", ExitDecision(STATIC))
        await asyncio.sleep(0)
        notes.note(PROJECT, "t1", ExitDecision(OTHER))
        await asyncio.sleep(0.01)
        held.set()
        await notes.settle()

        assert TagNote.model_validate_json(redis.values[KEY]).proxy_id == "row-2"
