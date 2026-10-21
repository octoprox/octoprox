# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the feed follower against the real Redis test container, every feed written by the real publisher.

A harness stands in for the gateway's applier, recording each update or refusing it on demand, and a head that moved
without an entry is made by writing back the keys of an earlier publish by hand.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import patch

import pytest

import api.db.streams as streams
from api.core.job_stats import job_stats
from api.core.workers import WorkerName
from api.db.redis import (
    FEED_HEAD_KEY,
    FEED_MANIFEST_KEY,
    FEED_SECTIONS_KEY,
    FEED_STREAM_KEY,
    RedisClient,
)
from api.feed.codec import FeedCodec, FeedDecodeError
from api.feed.follower import FeedFollower, FeedUpdate
from api.feed.models import FEED_SCHEMA, GLOBAL_SECTION, FeedHead, connector_section
from api.feed.publisher import FeedPublisher
from tests.feed.conftest import (
    COMPILED_AT,
    LEASE,
    OWNER,
    make_connector_section,
    make_feed,
    make_sections,
)
from tests.waits import until


class Harness:
    """A follower whose applier records every update, or refuses it on demand."""

    def __init__(self, redis_client: RedisClient, codec: FeedCodec, *, reconcile_interval: float = 3600.0) -> None:
        self.updates: list[FeedUpdate] = []
        self.refusing = False
        self.follower = FeedFollower(redis_client, codec, self._apply, reconcile_interval=reconcile_interval)
        self.task: asyncio.Task[None] | None = None

    async def _apply(self, update: FeedUpdate) -> None:
        if self.refusing:
            raise RuntimeError("snapshot build failed")
        self.updates.append(update)

    def start(self) -> None:
        self.task = asyncio.create_task(self.follower.run())

    async def close(self) -> None:
        await self.follower.stop()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    @property
    def full_reads(self) -> dict[str, int]:
        return self.follower.streams()["feed"].full_reads


@pytest.fixture
async def harness(redis_client: RedisClient, codec: FeedCodec) -> AsyncIterator[Harness]:
    built = Harness(redis_client, codec)
    yield built
    await built.close()


@pytest.fixture
async def stale_publisher(redis_client: RedisClient) -> AsyncIterator[FeedPublisher]:
    """A controller that holds the lease but another secret than the gateways."""
    await redis_client.client.set(LEASE, OWNER)
    yield FeedPublisher(redis_client, FeedCodec("another-secret"), lease_key=LEASE, owner_id=OWNER)


@pytest.fixture(autouse=True)
def _clean_job_stats() -> Iterator[None]:
    """One registry per process, so no test may inherit another's counters (an e2e gateway's, say)."""
    job_stats.reset()
    yield
    job_stats.reset()


@pytest.fixture(autouse=True)
def _quick_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries, of a failed apply and of a failed read, a twentieth of a second apart rather than a second, at most 0.2 s."""
    monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.05)
    monkeypatch.setattr(streams, "RETRY_MAX_SECONDS", 0.2)


def follower_failures() -> int:
    """Failed follower cycles this process has counted so far."""
    stats = job_stats.get(WorkerName.FEED_FOLLOWER)
    return stats.failures if stats is not None else 0


async def stored_feed(redis_client: RedisClient) -> tuple[str, dict[str, str], dict[str, str]]:
    """The head, manifest and sections Redis holds now, to write back later without an entry."""
    return (
        await redis_client.client.get(FEED_HEAD_KEY),
        await redis_client.client.hgetall(FEED_MANIFEST_KEY),
        await redis_client.client.hgetall(FEED_SECTIONS_KEY),
    )


async def write_back(redis_client: RedisClient, stored: tuple[str, dict[str, str], dict[str, str]]) -> None:
    """Make Redis hold an earlier publish again, as a publish no entry announced would leave it."""
    head, manifest, sections = stored
    pipe = redis_client.client.pipeline(transaction=True)
    pipe.delete(FEED_MANIFEST_KEY, FEED_SECTIONS_KEY)
    pipe.hset(FEED_MANIFEST_KEY, mapping=manifest)
    pipe.hset(FEED_SECTIONS_KEY, mapping=sections)
    pipe.set(FEED_HEAD_KEY, head)
    await pipe.execute()


class TestApply:
    async def test_the_first_poll_applies_the_whole_feed(self, harness: Harness, publisher: FeedPublisher) -> None:
        sections = make_feed("a")
        await publisher.publish(sections, changes="1700000000000-3", compiled_at=COMPILED_AT)
        assert harness.follower.readiness() == "feed not applied yet"

        assert await harness.follower.poll() is True

        [update] = harness.updates
        assert (update.changed, update.removed, dict(update.sections)) == (sections, set(), sections)
        assert update.head.changes == "1700000000000-3"
        assert harness.follower.applied_head == update.head
        assert harness.follower.applied_at is not None
        assert harness.follower.readiness() is None

    async def test_an_unchanged_feed_applies_nothing(self, harness: Harness, publisher: FeedPublisher) -> None:
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        await harness.follower.poll()
        # The controller compiles again and finds nothing changed.
        await publisher.publish(make_feed("a"), changes="2-0", compiled_at=COMPILED_AT)

        assert await harness.follower.poll() is False
        assert len(harness.updates) == 1

    async def test_only_changed_and_removed_sections_are_handed_over(
        self, harness: Harness, publisher: FeedPublisher
    ) -> None:
        await publisher.publish(make_feed("a", "b"), changes="1-0", compiled_at=COMPILED_AT)
        await harness.follower.poll()
        changed = make_connector_section("a", hosts=("10.0.0.9",))
        newer = make_sections(changed, make_connector_section("c"))
        await publisher.publish(newer, changes="2-0", compiled_at=COMPILED_AT)

        assert await harness.follower.poll() is True

        update = harness.updates[-1]
        assert sorted(update.changed) == ["connector:a", "connector:c"]
        assert update.changed["connector:a"] == changed
        assert update.removed == {"connector:b"}
        assert sorted(update.sections) == ["connector:a", "connector:c", GLOBAL_SECTION]
        assert update.head.changes == "2-0"

    async def test_an_update_the_applier_refused_is_offered_again(
        self, harness: Harness, publisher: FeedPublisher
    ) -> None:
        sections = make_feed("a")
        await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)
        harness.refusing = True

        with pytest.raises(RuntimeError, match="snapshot build failed"):
            await harness.follower.poll()

        assert harness.follower.applied_head is None
        harness.refusing = False
        assert await harness.follower.poll() is True
        assert harness.updates[0].changed == sections


class TestNeverPartial:
    async def test_a_missing_section_applies_nothing(
        self, harness: Harness, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        await publisher.publish(make_feed("a", "b"), changes="1-0", compiled_at=COMPILED_AT)
        await redis_client.client.hdel(FEED_SECTIONS_KEY, "connector:b")

        with pytest.raises(FeedDecodeError, match="connector:b is missing"):
            await harness.follower.poll()

        assert harness.updates == []
        assert harness.follower.readiness() == "feed not applied yet"

    async def test_a_section_that_does_not_match_its_digest_applies_nothing(
        self, harness: Harness, publisher: FeedPublisher, redis_client: RedisClient, codec: FeedCodec
    ) -> None:
        await publisher.publish(make_feed("a", "b"), changes="1-0", compiled_at=COMPILED_AT)
        # Sealed with the right secret, but not the content the manifest lists.
        _, payload = codec.encode(make_connector_section("b", hosts=("10.6.6.6",)))
        await redis_client.client.hset(FEED_SECTIONS_KEY, "connector:b", payload)

        with pytest.raises(FeedDecodeError, match="manifest entry"):
            await harness.follower.poll()

        assert harness.updates == []

    async def test_a_broken_update_keeps_the_feed_applied_so_far(
        self, harness: Harness, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        await harness.follower.poll()
        applied = harness.follower.applied_head
        await publisher.publish(make_feed("a", "b"), changes="2-0", compiled_at=COMPILED_AT)
        await redis_client.client.hdel(FEED_SECTIONS_KEY, "connector:b")

        with pytest.raises(FeedDecodeError):
            await harness.follower.poll()

        assert len(harness.updates) == 1
        assert harness.follower.applied_head == applied
        assert harness.follower.readiness() is None

    async def test_a_feed_sealed_with_another_secret_applies_nothing(
        self, harness: Harness, stale_publisher: FeedPublisher
    ) -> None:
        await stale_publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)

        with pytest.raises(FeedDecodeError, match="another internal secret"):
            await harness.follower.poll()

        assert harness.updates == []
        assert harness.follower.readiness() == "feed not applied yet"

    async def test_a_feed_without_its_global_section_is_not_applied(
        self, harness: Harness, publisher: FeedPublisher
    ) -> None:
        connectors_only = {connector_section("a"): make_connector_section("a")}
        await publisher.publish(connectors_only, changes="1-0", compiled_at=COMPILED_AT)

        with pytest.raises(FeedDecodeError, match="no global section"):
            await harness.follower.poll()

        assert harness.updates == []

    async def test_a_compile_that_lands_while_sections_are_read_is_applied_by_the_next_poll(
        self,
        harness: Harness,
        publisher: FeedPublisher,
        redis_client: RedisClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        newer = make_sections(make_connector_section("a", hosts=("10.0.0.9",)))
        pipeline = redis_client.client.pipeline
        landed = False

        def racing_pipeline(*args: Any, **kwargs: Any) -> Any:
            pipe = pipeline(*args, **kwargs)
            execute = pipe.execute

            async def execute_then_publish(*e_args: Any, **e_kwargs: Any) -> Any:
                nonlocal landed
                replies = await execute(*e_args, **e_kwargs)
                if not landed:
                    # The newer compile lands right after the follower read the manifest.
                    landed = True
                    await publisher.publish(newer, changes="2-0", compiled_at=COMPILED_AT)
                return replies

            pipe.execute = execute_then_publish
            return pipe

        monkeypatch.setattr(redis_client.client, "pipeline", racing_pipeline)

        assert await harness.follower.poll() is False
        assert harness.updates == []
        assert await harness.follower.poll() is True
        assert harness.updates[0].head.changes == "2-0"
        assert dict(harness.updates[0].sections) == newer


class TestHead:
    async def test_a_gateway_that_never_saw_a_feed_is_not_ready(self, harness: Harness) -> None:
        with patch("api.feed.follower.logger") as logger:
            assert await harness.follower.poll() is False
        assert harness.follower.readiness() == "feed not applied yet"
        # Expected before the controller's first publish: nothing is lost, so no warning.
        assert [call.args[0] for call in logger.info.call_args_list] == [
            "Routing feed head missing; keeping the feed applied so far"
        ]
        assert not logger.warning.called

    async def test_a_missing_head_keeps_the_feed_applied(
        self, harness: Harness, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        sections = make_feed("a")
        await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)
        await harness.follower.poll()
        await redis_client.client.flushdb()

        with patch("api.feed.follower.logger") as logger:
            assert await harness.follower.poll() is False
        assert harness.follower.readiness() is None
        assert [call.args[0] for call in logger.warning.call_args_list] == [
            "Routing feed head missing; keeping the feed applied so far"
        ]
        # The controller's next term writes the same compile again: nothing to apply.
        await redis_client.client.set(LEASE, OWNER)
        await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)
        assert await harness.follower.poll() is False
        assert len(harness.updates) == 1

    async def test_a_head_of_another_schema_is_ignored_and_the_gateway_keeps_routing_on_its_feed(
        self, harness: Harness, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        """A rolling upgrade that bumps the schema must not take every gateway out of the balancer at once."""
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        await harness.follower.poll()
        # A controller of a later release took over during an upgrade.
        await redis_client.client.set(FEED_HEAD_KEY, await foreign_head(redis_client))

        with patch("api.feed.follower.logger") as logger:
            assert await harness.follower.poll() is False
            assert await harness.follower.poll() is False
        assert harness.follower.readiness() is None
        assert len(harness.updates) == 1
        # Said once, not at every read.
        assert [call.args[0] for call in logger.error.call_args_list] == ["Routing feed of another schema ignored"]

        # A controller of this release publishes again.
        changed = make_sections(make_connector_section("a", hosts=("10.0.0.9",)))
        await publisher.publish(changed, changes="2-0", compiled_at=COMPILED_AT)
        assert await harness.follower.poll() is True
        assert harness.follower.readiness() is None
        assert sorted(harness.updates[-1].changed) == ["connector:a"]

    async def test_a_gateway_with_no_feed_says_the_schema_is_why(
        self, harness: Harness, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        await redis_client.client.set(FEED_HEAD_KEY, await foreign_head(redis_client))

        assert await harness.follower.poll() is False
        assert harness.updates == []
        assert harness.follower.readiness() == (
            f"feed schema {FEED_SCHEMA + 1} is not supported (this release reads {FEED_SCHEMA})"
        )


async def foreign_head(redis_client: RedisClient) -> str:
    """The stored head as a controller of a later release would write it."""
    head = FeedHead.model_validate_json(await redis_client.client.get(FEED_HEAD_KEY))
    return head.model_copy(update={"schema_version": FEED_SCHEMA + 1, "digest": "later"}).model_dump_json()


class TestRun:
    async def test_the_loop_applies_the_feed_at_once_and_stops_when_asked(
        self, harness: Harness, publisher: FeedPublisher
    ) -> None:
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        task = asyncio.create_task(harness.follower.run())
        try:
            await until(lambda: harness.follower.readiness() is None)
            # The stop cuts the read it waits on: no entry has to come to end it.
            await harness.follower.stop()
            await asyncio.wait_for(task, timeout=1)
        finally:
            task.cancel()
        stats = job_stats.get(WorkerName.FEED_FOLLOWER)
        assert stats is not None and (stats.runs, stats.idle_runs) == (1, 0)
        # Read in full once, at start; nothing else since.
        assert harness.full_reads == {"start": 1}

    async def test_each_publish_wakes_the_follower_with_no_poll(
        self, harness: Harness, publisher: FeedPublisher
    ) -> None:
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        harness.start()
        await until(lambda: len(harness.updates) == 1)

        for n, connectors in enumerate((("a", "b"), ("b",), ("b", "c")), start=2):
            await publisher.publish(make_feed(*connectors), changes=f"{n}-0", compiled_at=COMPILED_AT)
            # Woken by the publish's entry: well within a second, with an hour to the follower's own next read.
            await until(lambda n=n: len(harness.updates) == n, timeout=1.0)
        assert harness.updates[-1].head.changes == "4-0"
        state = harness.follower.streams()["feed"]
        assert state.entries == 3 and state.full_reads == {"start": 1}

    async def test_an_entry_for_the_head_applied_applies_nothing(
        self, harness: Harness, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        """A duplicate or a late entry costs one read of the head, nothing else."""
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        harness.start()
        await until(lambda: len(harness.updates) == 1)

        await redis_client.client.xadd(FEED_STREAM_KEY, {"digest": harness.updates[0].head.digest})

        await until(lambda: (stats := job_stats.get(WorkerName.FEED_FOLLOWER)) is not None and stats.runs == 2)
        stats = job_stats.get(WorkerName.FEED_FOLLOWER)
        assert stats is not None and stats.idle_runs == 1
        assert len(harness.updates) == 1

    async def test_an_update_that_failed_is_tried_again_with_no_new_entry(
        self, harness: Harness, publisher: FeedPublisher
    ) -> None:
        """A port that cannot be bound, a section that cannot be verified: no entry may come to retry it."""
        harness.refusing = True
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        with patch("api.core.loops.logger") as logger:
            harness.start()
            await until(lambda: follower_failures() >= 3)
            harness.refusing = False
            await until(lambda: harness.follower.readiness() is None)

        assert [update.head.changes for update in harness.updates] == ["1-0"]
        # One line when the updates start failing and one when they recover, not one a retry.
        assert [call.args[0] for call in logger.error.call_args_list] == ["Loop cycle failed"]
        assert logger.error.call_args.kwargs["loop"] == WorkerName.FEED_FOLLOWER
        assert [call.args[0] for call in logger.info.call_args_list] == ["Loop recovered"]
        # The read stayed owed through the streak and was made once more after it: the start's, retried.
        assert harness.full_reads == {"start": 1}
        stats = job_stats.get(WorkerName.FEED_FOLLOWER)
        assert stats is not None and stats.consecutive_failures == 0

    async def test_a_feed_sealed_with_another_secret_is_applied_once_the_controller_has_the_right_one(
        self, harness: Harness, publisher: FeedPublisher, stale_publisher: FeedPublisher
    ) -> None:
        await stale_publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        with patch("api.core.loops.logger") as logger:
            harness.start()
            await until(lambda: follower_failures() >= 2)
            # The controller is redeployed with the gateways' secret.
            await publisher.publish(make_feed("a"), changes="2-0", compiled_at=COMPILED_AT)
            await until(lambda: harness.follower.readiness() is None)

        assert harness.updates[0].head.changes == "2-0"
        assert [call.args[0] for call in logger.error.call_args_list] == ["Loop cycle failed"]

    async def test_a_read_that_failed_reads_the_head_in_full(
        self, publisher: FeedPublisher, redis_client: RedisClient, codec: FeedCodec, test_redis_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Redis may have restarted, empty or not: the entries after the follower's position mean nothing then."""
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.5)
        client = RedisClient(test_redis_url)
        await client.connect()
        harness = Harness(client, codec)
        try:
            await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
            harness.start()
            await until(lambda: len(harness.updates) == 1)
            # The follower's waiting read loses its connection; while it waits to read again, the
            # head moves and its entry is gone, as when Redis restarts from an older snapshot.
            await asyncio.sleep(0.1)
            blocked = [entry["id"] for entry in await redis_client.client.client_list() if entry["cmd"] == "xread"]
            assert len(blocked) == 1
            await redis_client.client.client_kill_filter(_id=blocked[0])
            await publisher.publish(make_feed("a", "b"), changes="2-0", compiled_at=COMPILED_AT)
            [(entry_id, _fields)] = await redis_client.client.xrevrange(FEED_STREAM_KEY, count=1)
            await redis_client.client.xdel(FEED_STREAM_KEY, entry_id)

            await until(lambda: len(harness.updates) == 2)
            assert harness.updates[1].head.changes == "2-0"
            assert harness.full_reads == {"start": 1, "reconnect": 1}
        finally:
            await harness.close()
            await client.close()

    async def test_the_reconcile_reads_a_head_no_entry_announced(
        self, publisher: FeedPublisher, redis_client: RedisClient, codec: FeedCodec
    ) -> None:
        harness = Harness(redis_client, codec, reconcile_interval=0.3)
        try:
            await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
            earlier = await stored_feed(redis_client)
            harness.start()
            await until(lambda: len(harness.updates) == 1)
            await publisher.publish(make_feed("a", "b"), changes="2-0", compiled_at=COMPILED_AT)
            await until(lambda: len(harness.updates) == 2)

            # The feed goes back to the first compile, and no entry says so.
            await write_back(redis_client, earlier)

            await until(lambda: len(harness.updates) == 3)
            assert harness.updates[2].head.changes == "1-0"
            assert harness.updates[2].removed == {"connector:b"}
            assert harness.full_reads["reconcile"] >= 1
        finally:
            await harness.close()

    async def test_a_stream_recreated_below_the_position_is_read_in_full(
        self, harness: Harness, publisher: FeedPublisher, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wipe, then a clock behind: the new entries sort before the position and no read would return them."""
        monkeypatch.setattr(streams, "STREAM_BLOCK_SECONDS", 0.3)
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        earlier = await stored_feed(redis_client)
        await publisher.publish(make_feed("a", "b"), changes="2-0", compiled_at=COMPILED_AT)
        harness.start()
        await until(lambda: len(harness.updates) == 1)

        await write_back(redis_client, earlier)
        await redis_client.client.delete(FEED_STREAM_KEY)
        await redis_client.client.xadd(FEED_STREAM_KEY, {"digest": "earlier"}, id="1-1")

        await until(lambda: len(harness.updates) == 2)
        assert harness.updates[1].head.changes == "1-0"
        assert harness.full_reads == {"start": 1, "gap": 1}
