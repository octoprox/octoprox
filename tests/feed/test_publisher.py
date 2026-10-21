# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the feed publisher against the real Redis test container.

What a publish leaves is read back from the three feed keys and the feed
stream, so the tests pin the layout followers rely on: one head, one
manifest and the sealed sections of the same compile, written only by the
lease holder, and one stream entry for each head that replaces another.
"""

from typing import Any

import pytest

from api.db.redis import (
    FEED_HEAD_KEY,
    FEED_MANIFEST_KEY,
    FEED_SECTIONS_KEY,
    FEED_STREAM_KEY,
    RedisClient,
)
from api.feed.codec import FeedCodec
from api.feed.models import FEED_SCHEMA, GLOBAL_SECTION, ConnectorSection, FeedHead, GlobalSection
from api.feed.publisher import FeedPublisher
from tests.feed.conftest import (
    COMPILED_AT,
    LEASE,
    OWNER,
    make_connector_section,
    make_feed,
    make_sections,
)


async def stored_feed(redis_client: RedisClient) -> tuple[Any, ...]:
    """Everything the feed keeps in Redis, to compare before and after."""
    return (
        await redis_client.client.get(FEED_HEAD_KEY),
        await redis_client.client.hgetall(FEED_MANIFEST_KEY),
        await redis_client.client.hgetall(FEED_SECTIONS_KEY),
    )


async def stored_head(redis_client: RedisClient) -> FeedHead:
    return FeedHead.model_validate_json(await redis_client.client.get(FEED_HEAD_KEY))


class TestPublish:
    async def test_a_first_publish_writes_every_section(
        self, publisher: FeedPublisher, redis_client: RedisClient, codec: FeedCodec
    ) -> None:
        sections = make_feed("a", "b")

        result = await publisher.publish(sections, changes="7-0", compiled_at=COMPILED_AT)

        assert (result.written, result.removed, result.head_missing, result.lease_lost) == (3, 0, True, False)
        manifest = await redis_client.client.hgetall(FEED_MANIFEST_KEY)
        assert sorted(manifest) == sorted(sections)
        head = await stored_head(redis_client)
        assert (head.schema_version, head.changes, head.compiled_at, head.sections) == (
            FEED_SCHEMA, "7-0", COMPILED_AT, 3
        )
        assert head.digest == codec.manifest_digest(manifest)
        payload = await redis_client.client.hget(FEED_SECTIONS_KEY, "connector:a")
        assert codec.decode(payload, ConnectorSection, manifest["connector:a"]) == sections["connector:a"]

    async def test_only_changed_sections_are_written(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        await publisher.publish(make_feed("a", "b"), changes="1-0", compiled_at=COMPILED_AT)
        before = await redis_client.client.hgetall(FEED_SECTIONS_KEY)
        changed = make_sections(make_connector_section("a"), make_connector_section("b", hosts=("10.0.0.9",)))

        result = await publisher.publish(changed, changes="2-0", compiled_at=COMPILED_AT)

        assert (result.written, result.removed, result.head_missing) == (1, 0, False)
        after = await redis_client.client.hgetall(FEED_SECTIONS_KEY)
        # An untouched section keeps the very payload it was sealed with, nonce and all.
        assert after[GLOBAL_SECTION] == before[GLOBAL_SECTION]
        assert after["connector:a"] == before["connector:a"]
        assert after["connector:b"] != before["connector:b"]
        assert (await stored_head(redis_client)).changes == "2-0"

    async def test_a_compile_that_changed_nothing_writes_nothing(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        before = await stored_feed(redis_client)

        result = await publisher.publish(make_feed("a"), changes="2-0", compiled_at=COMPILED_AT)

        assert (result.written, result.removed, result.head_missing, result.lease_lost) == (0, 0, False, False)
        # The head still names the compile that last changed something.
        assert await stored_feed(redis_client) == before

    async def test_dropped_sections_leave_the_manifest_and_the_sections(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        await publisher.publish(make_feed("a", "b"), changes="1-0", compiled_at=COMPILED_AT)

        result = await publisher.publish(make_feed("a"), changes="2-0", compiled_at=COMPILED_AT)

        assert (result.written, result.removed) == (0, 1)
        assert sorted(await redis_client.client.hkeys(FEED_MANIFEST_KEY)) == ["connector:a", GLOBAL_SECTION]
        assert sorted(await redis_client.client.hkeys(FEED_SECTIONS_KEY)) == ["connector:a", GLOBAL_SECTION]
        assert (await stored_head(redis_client)).sections == 2


def count_serialised(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The sections serialised from now on, by name, as the publisher sees them."""
    serialised: list[str] = []
    connector_json = ConnectorSection.model_dump_json
    global_json = GlobalSection.model_dump_json

    def connector(self: ConnectorSection, **kwargs: Any) -> str:
        serialised.append(f"connector:{self.connector.id}")
        return connector_json(self, **kwargs)

    def global_(self: GlobalSection, **kwargs: Any) -> str:
        serialised.append(GLOBAL_SECTION)
        return global_json(self, **kwargs)

    monkeypatch.setattr(ConnectorSection, "model_dump_json", connector)
    monkeypatch.setattr(GlobalSection, "model_dump_json", global_)
    return serialised


class TestDigests:
    async def test_only_a_section_handed_over_anew_is_serialised(
        self, publisher: FeedPublisher, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cycle that rebuilt one connector's section costs that section, however large the feed."""
        sections = make_feed("a", "b", "c")
        await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)
        serialised = count_serialised(monkeypatch)

        rebuilt = {**sections, "connector:b": make_connector_section("b", hosts=("10.0.0.9",))}
        result = await publisher.publish(rebuilt, changes="2-0", compiled_at=COMPILED_AT)

        assert result.written == 1
        # Once for its digest, and the encode reuses that JSON.
        assert serialised == ["connector:b"]
        # The same objects again: nothing serialised, nothing written.
        assert (await publisher.publish(rebuilt, changes="2-0", compiled_at=COMPILED_AT)).written == 0
        assert serialised == ["connector:b"]

    async def test_a_wiped_feed_is_written_again_from_remembered_digests(
        self, publisher: FeedPublisher, redis_client: RedisClient, codec: FeedCodec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sections = make_feed("a", "b")
        await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)
        await redis_client.client.delete(FEED_HEAD_KEY, FEED_MANIFEST_KEY, FEED_SECTIONS_KEY)
        serialised = count_serialised(monkeypatch)

        result = await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)

        # Every digest came from the memo, and every section is sealed again.
        assert (result.written, result.head_missing) == (3, True)
        assert sorted(serialised) == sorted(sections)
        manifest = await redis_client.client.hgetall(FEED_MANIFEST_KEY)
        assert (await stored_head(redis_client)).digest == codec.manifest_digest(manifest)
        payload = await redis_client.client.hget(FEED_SECTIONS_KEY, "connector:b")
        assert codec.decode(payload, ConnectorSection, manifest["connector:b"]) == sections["connector:b"]


class TestFencing:
    async def test_a_publisher_whose_lease_was_taken_over_writes_nothing(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        before = await stored_feed(redis_client)
        # The lease expired while this process stalled, and another controller took it.
        await redis_client.client.set(LEASE, "controller-2")
        changed = make_sections(make_connector_section("a", hosts=("10.0.0.9",)))

        result = await publisher.publish(changed, changes="2-0", compiled_at=COMPILED_AT)

        assert result.lease_lost is True
        assert (result.written, result.removed) == (0, 0)
        assert await stored_feed(redis_client) == before

    async def test_nothing_is_written_without_a_lease(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        await redis_client.client.delete(LEASE)

        result = await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)

        assert (result.lease_lost, result.head_missing) == (True, True)
        assert await redis_client.client.exists(FEED_HEAD_KEY, FEED_MANIFEST_KEY, FEED_SECTIONS_KEY) == 0


async def announced(redis_client: RedisClient) -> list[str]:
    """The digests the feed stream announces, oldest first."""
    return [fields["digest"] for _id, fields in await redis_client.client.xrange(FEED_STREAM_KEY)]


class TestFeedStream:
    async def test_each_head_that_replaces_another_is_announced_with_its_digest(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        first = (await stored_head(redis_client)).digest
        await publisher.publish(make_feed("a", "b"), changes="2-0", compiled_at=COMPILED_AT)

        assert await announced(redis_client) == [first, (await stored_head(redis_client)).digest]

    async def test_a_compile_that_changed_nothing_announces_nothing(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)
        await publisher.publish(make_feed("a"), changes="2-0", compiled_at=COMPILED_AT)
        assert len(await announced(redis_client)) == 1

    async def test_the_same_head_written_again_announces_nothing(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        """A feed rewritten in full under the head it had (a lost sections hash) wakes no gateway."""
        sections = make_feed("a")
        await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)
        await redis_client.client.delete(FEED_SECTIONS_KEY)

        result = await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)
        assert result.written == 2
        assert len(await announced(redis_client)) == 1

    async def test_a_publisher_without_the_lease_announces_nothing(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        await redis_client.client.set(LEASE, "controller-2")
        assert (await publisher.publish(make_feed("a"), changes="1-0", compiled_at=COMPILED_AT)).lease_lost
        assert await announced(redis_client) == []

    async def test_the_stream_keeps_about_its_last_hundred_heads(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        """A gateway needs only "something is new"; the stream is trimmed as it is written."""
        for n in range(250):
            sections = make_sections(make_connector_section("a", hosts=(f"10.0.{n // 200}.{n % 200}",)))
            await publisher.publish(sections, changes=f"{n}-0", compiled_at=COMPILED_AT)
        length = await redis_client.client.xlen(FEED_STREAM_KEY)
        assert 100 <= length < 250


class TestRepair:
    async def test_a_wiped_redis_is_written_again_in_full(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        sections = make_feed("a", "b")
        await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)
        await redis_client.client.flushdb()
        # The controller holds its lease again before its next cycle.
        await redis_client.client.set(LEASE, OWNER)

        result = await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)

        assert (result.written, result.removed, result.head_missing) == (3, 0, True)
        assert sorted(await redis_client.client.hkeys(FEED_MANIFEST_KEY)) == sorted(sections)
        assert await redis_client.client.hlen(FEED_SECTIONS_KEY) == 3

    async def test_a_lost_sections_hash_is_written_again(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        sections = make_feed("a")
        await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)
        await redis_client.client.delete(FEED_SECTIONS_KEY)

        result = await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)

        assert (result.written, result.head_missing) == (2, False)
        assert await redis_client.client.hlen(FEED_SECTIONS_KEY) == 2

    async def test_sections_dropped_while_the_manifest_was_lost_do_not_linger(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        await publisher.publish(make_feed("a", "b"), changes="1-0", compiled_at=COMPILED_AT)
        await redis_client.client.delete(FEED_MANIFEST_KEY)

        await publisher.publish(make_feed("a"), changes="2-0", compiled_at=COMPILED_AT)

        assert sorted(await redis_client.client.hkeys(FEED_SECTIONS_KEY)) == ["connector:a", GLOBAL_SECTION]

    async def test_a_head_of_another_schema_is_replaced(
        self, publisher: FeedPublisher, redis_client: RedisClient
    ) -> None:
        sections = make_feed("a")
        await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)
        head = await stored_head(redis_client)
        later = head.model_copy(update={"schema_version": FEED_SCHEMA + 1})
        await redis_client.client.set(FEED_HEAD_KEY, later.model_dump_json())

        result = await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)

        assert result.written == 2
        assert (await stored_head(redis_client)).schema_version == FEED_SCHEMA

    async def test_a_feed_sealed_with_another_secret_is_written_again(
        self, publisher: FeedPublisher, redis_client: RedisClient, codec: FeedCodec
    ) -> None:
        sections = make_feed("a")
        previous = FeedPublisher(redis_client, FeedCodec("previous-secret"), lease_key=LEASE, owner_id=OWNER)
        await previous.publish(sections, changes="1-0", compiled_at=COMPILED_AT)

        result = await publisher.publish(sections, changes="1-0", compiled_at=COMPILED_AT)

        assert result.written == 2
        manifest = await redis_client.client.hgetall(FEED_MANIFEST_KEY)
        assert (await stored_head(redis_client)).digest == codec.manifest_digest(manifest)
        payload = await redis_client.client.hget(FEED_SECTIONS_KEY, "connector:a")
        assert codec.decode(payload, ConnectorSection, manifest["connector:a"]) == sections["connector:a"]
