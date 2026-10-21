# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the per-proxy rate limiter: the shared window and the quarantine set it trips, on the real Redis test container.

Each limiter stands for one gateway, two on the same Redis for two gateways sharing a proxy, and a Redis that does not
answer is the container paused (``CLIENT PAUSE``), which the test waits out before it ends.
"""

import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from api.core.rate_limiter import RateLimiter
from api.db.redis import PROXY_REQUESTS_KEY, QUARANTINE_KEY, RUNTIME_STREAM_KEY, RedisClient
from api.db.streams import RuntimeSignal
from api.feed.state import RuntimeStateReader


class Gateway:
    """A rate limiter and the trips it handed on to its gateway's exclusions."""

    def __init__(self, redis_client: RedisClient) -> None:
        self.trips: list[tuple[str, datetime]] = []
        self.limiter = RateLimiter(redis_client, on_trip=lambda proxy_id, until: self.trips.append((proxy_id, until)))

    async def requests(
        self, count: int, *, proxy_id: str = "proxy-1", max_requests: int = 10, window_seconds: int = 60,
        quarantine: tuple[int, int] = (30, 60),
    ) -> None:
        for _ in range(count):
            await self.limiter.record_request(
                proxy_id=proxy_id, connector_id="conn-1", max_requests=max_requests,
                window_seconds=window_seconds, quarantine_seconds_min=quarantine[0],
                quarantine_seconds_max=quarantine[1],
            )

    @property
    def tripped(self) -> list[str]:
        return [proxy_id for proxy_id, _until in self.trips]


def epoch(naive_utc: datetime) -> float:
    return naive_utc.replace(tzinfo=UTC).timestamp()


@pytest.fixture
def gateway(redis_client: RedisClient) -> Iterator[Gateway]:
    yield Gateway(redis_client)


class TestWindow:
    async def test_under_the_limit_nothing_trips(self, gateway: Gateway, redis_client: RedisClient) -> None:
        await gateway.requests(9)
        assert gateway.tripped == []
        assert await redis_client.client.zcard(PROXY_REQUESTS_KEY.format(proxy_id="proxy-1")) == 9
        assert await redis_client.client.zcard(QUARANTINE_KEY) == 0

    async def test_the_request_that_fills_the_window_trips_the_proxy(
        self, gateway: Gateway, redis_client: RedisClient
    ) -> None:
        await gateway.requests(10)
        assert gateway.tripped == ["proxy-1"]
        until = await redis_client.client.zscore(QUARANTINE_KEY, "proxy-1")
        assert time.time() + 29 < until <= time.time() + 60
        # Counting starts over once the quarantine is over.
        assert await redis_client.client.exists(PROXY_REQUESTS_KEY.format(proxy_id="proxy-1")) == 0

    async def test_the_quarantine_lasts_between_its_bounds(self, gateway: Gateway, redis_client: RedisClient) -> None:
        await gateway.requests(5, max_requests=5, quarantine=(100, 200))
        remaining = await redis_client.client.zscore(QUARANTINE_KEY, "proxy-1") - time.time()
        assert 99 < remaining <= 200

    async def test_equal_bounds_give_a_fixed_quarantine(self, gateway: Gateway, redis_client: RedisClient) -> None:
        await gateway.requests(5, max_requests=5, quarantine=(60, 60))
        remaining = await redis_client.client.zscore(QUARANTINE_KEY, "proxy-1") - time.time()
        assert 59 < remaining <= 60

    async def test_requests_older_than_the_window_do_not_count(
        self, gateway: Gateway, redis_client: RedisClient
    ) -> None:
        key = PROXY_REQUESTS_KEY.format(proxy_id="proxy-1")
        old_ms = int(time.time() * 1000) - 120_000
        await redis_client.client.zadd(key, mapping={f"old-{i}": old_ms + i for i in range(8)})
        await gateway.requests(2)
        assert gateway.tripped == []
        assert await redis_client.client.zcard(key) == 2

    async def test_proxies_are_counted_apart(self, gateway: Gateway, redis_client: RedisClient) -> None:
        await gateway.requests(5, max_requests=5)
        await gateway.requests(4, proxy_id="proxy-2", max_requests=5)
        assert gateway.tripped == ["proxy-1"]
        assert await redis_client.client.zrange(QUARANTINE_KEY, 0, -1) == ["proxy-1"]

    async def test_gateways_sharing_a_proxy_count_in_one_window(self, redis_client: RedisClient) -> None:
        a, b = Gateway(redis_client), Gateway(redis_client)
        await a.requests(3, proxy_id="shared", max_requests=5)
        await b.requests(2, proxy_id="shared", max_requests=5)
        # The second gateway's request filled the window: it trips, for both.
        assert (a.tripped, b.tripped) == ([], ["shared"])
        assert await redis_client.client.zscore(QUARANTINE_KEY, "shared") is not None

    async def test_requests_in_flight_at_the_trip_count_in_the_new_window(
        self, gateway: Gateway, redis_client: RedisClient
    ) -> None:
        await gateway.requests(5, max_requests=5)
        # One that was already running when the proxy tripped: the vendor saw it.
        await gateway.requests(1, max_requests=5)
        assert gateway.tripped == ["proxy-1"]
        assert await redis_client.client.zcard(PROXY_REQUESTS_KEY.format(proxy_id="proxy-1")) == 1


class TestQuarantineSet:
    async def test_the_gateway_is_told_the_end_the_set_holds(self, gateway: Gateway, redis_client: RedisClient) -> None:
        await gateway.requests(5, max_requests=5)
        ((proxy_id, until),) = gateway.trips
        assert until.tzinfo is None
        assert epoch(until) == pytest.approx(await redis_client.client.zscore(QUARANTINE_KEY, proxy_id), abs=1e-3)
        # The reader the controller builds the feed from sees the same quarantine.
        quarantined = await RuntimeStateReader(redis_client).quarantined()
        assert quarantined.keys() == {"proxy-1"}
        assert epoch(quarantined["proxy-1"]) == pytest.approx(epoch(until), abs=1e-3)

    async def test_a_longer_quarantine_is_never_shortened(self, gateway: Gateway, redis_client: RedisClient) -> None:
        later = time.time() + 10_000
        await redis_client.client.zadd(QUARANTINE_KEY, {"proxy-1": later})
        await gateway.requests(5, max_requests=5, quarantine=(30, 30))
        assert gateway.tripped == ["proxy-1"]
        assert await redis_client.client.zscore(QUARANTINE_KEY, "proxy-1") == pytest.approx(later)

    async def test_a_trip_extends_a_shorter_quarantine(self, gateway: Gateway, redis_client: RedisClient) -> None:
        await redis_client.client.zadd(QUARANTINE_KEY, {"proxy-1": time.time() + 5})
        await gateway.requests(5, max_requests=5, quarantine=(300, 300))
        assert await redis_client.client.zscore(QUARANTINE_KEY, "proxy-1") > time.time() + 290


class TestRuntimeEntry:
    async def test_a_trip_announces_the_quarantine_on_the_runtime_stream(
        self, gateway: Gateway, redis_client: RedisClient
    ) -> None:
        """The entry wakes the controller, which rebuilds the feed's runtime section for every gateway."""
        await gateway.requests(5, max_requests=5)
        await gateway.requests(4, proxy_id="proxy-2", max_requests=5)

        entries = await redis_client.client.xrange(RUNTIME_STREAM_KEY)
        assert [fields for _id, fields in entries] == [{"kind": RuntimeSignal.QUARANTINE.value, "ref": "proxy-1"}]

    async def test_the_entry_goes_in_the_quarantines_own_transaction(
        self, gateway: Gateway, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So the controller never reads an entry whose quarantine is not in the set yet."""
        sent: list[tuple[bool, list[str]]] = []
        pipeline = redis_client.client.pipeline

        def recording(*args: Any, **kwargs: Any) -> Any:
            pipe = pipeline(*args, **kwargs)
            execute = pipe.execute

            async def recorded(*e_args: Any, **e_kwargs: Any) -> Any:
                sent.append((pipe.is_transaction, [str(args[0]).upper() for args, _options in pipe.command_stack]))
                return await execute(*e_args, **e_kwargs)

            pipe.execute = recorded
            return pipe

        monkeypatch.setattr(redis_client.client, "pipeline", recording)
        await gateway.requests(5, max_requests=5)

        assert sent == [(True, ["DEL", "ZADD", "XADD"])]


class TestFailOpen:
    async def test_a_window_redis_cannot_count_lets_the_request_through(self) -> None:
        # Nothing listens there: every command fails fast, as with Redis down.
        unreachable = RedisClient("redis://127.0.0.1:1/0", socket_timeout=0.5)
        await unreachable.connect()
        try:
            gateway = Gateway(unreachable)
            await gateway.requests(3, max_requests=1)
            assert gateway.limiter.window_errors == 3
            assert gateway.tripped == []
        finally:
            await unreachable.close()

    async def test_a_redis_that_does_not_answer_costs_a_count_its_deadline(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """A stuck Redis is not a down one: without the deadline the count would wait out the client's reply bound."""
        client = RedisClient(test_redis_url, request_timeout=0.2)
        await client.connect()
        try:
            gateway = Gateway(client)
            await client.client.ping()
            await redis_client.client.client_pause(1500, all=True)
            loop = asyncio.get_running_loop()
            started = loop.time()

            await gateway.requests(1, max_requests=1)

            assert loop.time() - started < 0.8
            assert gateway.limiter.window_errors == 1
            assert gateway.tripped == []
        finally:
            await client.close()
