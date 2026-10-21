# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for a gateway's sighting publisher against the real Redis test container: preflight's sightings on their stream.

The batch publisher's own rules (what Redis did not confirm goes first, one send at a time) are tested with the base;
these hold the sighting publisher to its stream, its channel, its cap and its stop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from api.core import utc_now
from api.core.consumers import decode_items
from api.core.job_stats import job_stats
from api.core.workers import WorkerName
from api.db.redis import SIGHTING_STREAM_KEY, BatchChannel, BatchPart, BatchReply, RedisClient
from api.db.streams import StreamEntry
from api.gateway import sightings as sightings_module
from api.gateway.sightings import SightingPublisher
from api.geo.models import ObservationSource
from api.jobs.messages import SightingMessage
from tests.waits import running, until


def _sighting(proxy_id: str, ip: str = "81.2.69.160") -> SightingMessage:
    return SightingMessage(
        proxy_id=proxy_id, ip=ip, source=ObservationSource.PREFLIGHT, candidates=[], observed_at=utc_now(),
        instance_id="gateway-1",
    )


async def _taken(redis_client: RedisClient) -> list[SightingMessage]:
    """The sightings on the stream, oldest first, decoded as the workers' sighting ingest decodes them."""
    entries = [StreamEntry(entry_id, fields) for entry_id, fields in await redis_client.client.xrange(SIGHTING_STREAM_KEY)]
    parsed, malformed = decode_items(entries, SightingMessage.model_validate_json)
    assert malformed == []
    return parsed


@pytest.fixture(autouse=True)
def _clean_job_stats() -> Iterator[None]:
    job_stats.reset()
    yield
    job_stats.reset()


class TestSightingPublisher:
    async def test_each_interval_appends_the_sightings_to_their_stream_for_the_workers(
        self, redis_client: RedisClient
    ) -> None:
        publisher = SightingPublisher(redis_client, interval=0.05, max_pending=100)
        publisher.push(_sighting("slot-1"))
        publisher.push(_sighting("slot-2", ip="81.2.69.161"))
        async with running(publisher):
            await until(lambda: publisher.published == 2)

        assert [(item.proxy_id, item.ip) for item in await _taken(redis_client)] == [
            ("slot-1", "81.2.69.160"),
            ("slot-2", "81.2.69.161"),
        ]
        stats = job_stats.get(WorkerName.SIGHTING_PUBLISHER)
        assert stats is not None and stats.runs >= 1 and stats.interval_seconds == 0.05

    async def test_a_stop_sends_what_the_buffer_holds_once_more(self, redis_client: RedisClient) -> None:
        publisher = SightingPublisher(redis_client, interval=60, max_pending=100)
        task = asyncio.create_task(publisher.run())
        await asyncio.sleep(0.01)
        publisher.push(_sighting("slot-1"))

        await publisher.stop()

        await asyncio.wait_for(task, 1)
        assert [item.proxy_id for item in await _taken(redis_client)] == ["slot-1"]
        assert (publisher.pending, publisher.published) == (0, 1)

    async def test_its_parts_go_on_the_sighting_channel_of_their_own(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One writer per channel and process: sharing another's sequences, a part sent again could be skipped."""
        sent: list[BatchPart] = []
        real = redis_client.send_batch

        async def send(parts: list[BatchPart]) -> list[BatchReply]:
            sent.extend(parts)
            return await real(parts)

        monkeypatch.setattr(redis_client, "send_batch", send)
        publisher = SightingPublisher(redis_client, interval=60, max_pending=100)
        publisher.push(_sighting("slot-1"))

        assert await publisher.flush() == 1

        assert [part.id.channel for part in sent] == [BatchChannel.SIGHTING_APPENDS]
        assert sent[0].keys == (SIGHTING_STREAM_KEY,)

    async def test_the_stream_is_capped_and_what_the_cap_trims_is_counted(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stream no worker takes cannot fill Redis: each append trims it to about its cap, oldest first."""
        pipe = redis_client.client.pipeline(transaction=False)
        for n in range(1000):
            pipe.xadd(SIGHTING_STREAM_KEY, {"item": _sighting(f"old-{n}").model_dump_json()})
        await pipe.execute()
        monkeypatch.setattr(sightings_module, "SIGHTING_STREAM_MAXLEN", 10)
        publisher = SightingPublisher(redis_client, interval=60, max_pending=100)
        publisher.push(_sighting("slot-1"))

        await publisher.flush()

        # Approximate: Redis drops whole nodes of a hundred entries, the newest kept.
        assert await redis_client.client.xlen(SIGHTING_STREAM_KEY) <= 110
        assert (await _taken(redis_client))[-1].proxy_id == "slot-1"
        assert publisher.dropped >= 890

    async def test_a_full_buffer_lets_its_oldest_sighting_go(self) -> None:
        """Through a Redis outage the buffer holds ``max_pending`` sightings at most: memory stays bounded."""
        publisher = SightingPublisher(None, interval=60, max_pending=2)
        for n in range(3):
            publisher.push(_sighting(f"slot-{n}"))

        assert (publisher.pending, publisher.dropped) == (2, 1)
