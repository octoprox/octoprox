# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the loop bases (api.core.loops): the turn, its job stats and back-off, the lease gate, and the timer family.

Toy loops note when each cycle starts; leased ones lead by a real lease on the session's Redis, with its timings shrunk
(TTL 600 ms, refresh 200 ms, poll 100 ms) so a failover takes a fraction of a second.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest

import api.db.streams as streams
from api.core.job_stats import job_stats
from api.core.leadership import Lease
from api.core.loops import Loop, PeriodicLoop
from api.db.redis import LEASE_KEY, RedisClient
from tests.waits import running, until

NAME: Any = "toy_loop"
KEY = LEASE_KEY.format(name="test")
TTL_MS, REFRESH_MS, POLL_MS = 600, 200, 100


@pytest.fixture(autouse=True)
def _clean_job_stats() -> Any:
    """One registry per process: no test may see another's counters."""
    job_stats.reset()
    yield
    job_stats.reset()


class Ticker(PeriodicLoop):
    """A periodic loop that notes when each cycle starts and ends, and which terms it began."""

    def __init__(self, *, interval: float, lease: Lease | None = None, cycle_seconds: float = 0.0) -> None:
        super().__init__(name=NAME, interval=interval, lease=lease)
        self.cycle_seconds = cycle_seconds
        self.started: list[float] = []
        self.ended: list[float] = []
        self.terms: list[int] = []
        self.failing: Exception | None = None
        self.worked = True

    async def _cycle(self) -> bool:
        self.started.append(time.monotonic())
        if self.cycle_seconds:
            await asyncio.sleep(self.cycle_seconds)
        self.ended.append(time.monotonic())
        if self.failing is not None:
            raise self.failing
        return self.worked

    async def _on_term(self, term: int) -> None:
        await super()._on_term(term)
        self.terms.append(term)


class EagerTicker(Ticker):
    """A periodic loop whose first cycle, of the loop and of each lead, comes at once."""

    first_at_once = True


class Woken(Loop):
    """A loop with no timed duty: only a wake brings a cycle, or the back-off after one that failed."""

    def __init__(self) -> None:
        super().__init__(name=NAME)
        self.started: list[float] = []
        self.failures_left = 0

    async def _cycle(self) -> bool:
        self.started.append(time.monotonic())
        if self.failures_left:
            self.failures_left -= 1
            raise RuntimeError("Redis is down")
        return True


@pytest.fixture
async def leases(redis_client: RedisClient) -> AsyncIterator[list[Lease]]:
    """Leases the test creates; every one is released at the end."""
    created: list[Lease] = []
    yield created
    for lease in created:
        await lease.release()


def kept(redis_client: RedisClient, leases: list[Lease]) -> Lease:
    """A lease kept from now, with the timings shrunk."""
    lease = Lease(redis_client, name="test", owner_id="me", ttl_ms=TTL_MS, refresh_ms=REFRESH_MS, poll_ms=POLL_MS)
    leases.append(lease)
    lease.keep()
    return lease


class TestPeriodicLoop:
    async def test_the_first_cycle_comes_one_interval_after_the_start(self) -> None:
        loop = Ticker(interval=0.3)
        started = time.monotonic()
        async with running(loop):
            await until(lambda: len(loop.started) == 1)

        assert 0.27 <= loop.started[0] - started < 0.6

    async def test_a_loop_that_cycles_at_once_starts_with_a_cycle(self) -> None:
        loop = EagerTicker(interval=10)
        started = time.monotonic()
        async with running(loop):
            await until(lambda: len(loop.started) == 1)

        assert loop.started[0] - started < 0.2

    async def test_cycles_start_an_interval_apart_however_long_each_takes(self) -> None:
        """A cycle every interval, not an interval between cycles: the work does not stretch the period."""
        loop = Ticker(interval=0.15, cycle_seconds=0.1)
        async with running(loop):
            await until(lambda: len(loop.started) == 5)

        gaps = [later - earlier for earlier, later in zip(loop.started, loop.started[1:], strict=False)]
        assert 0.14 <= sum(gaps) / len(gaps) < 0.2

    async def test_a_cycle_longer_than_the_interval_is_followed_at_once_and_counted_an_overrun(self) -> None:
        loop = Ticker(interval=0.05, cycle_seconds=0.1)
        async with running(loop):
            await until(lambda: len(loop.ended) == 3)

        assert loop.started[2] - loop.ended[1] < 0.05
        stats = job_stats.get(NAME)
        assert stats is not None and stats.overruns >= 2 and stats.interval_seconds == 0.05

    async def test_a_cycle_that_failed_waits_for_its_next_interval(self) -> None:
        loop = Ticker(interval=0.2)
        loop.failing = RuntimeError("Postgres is down")
        async with running(loop):
            await until(lambda: len(loop.started) == 3)

        # The back-off of a second failure would be 2 s: it is held to the interval.
        assert 0.19 <= loop.started[2] - loop.started[1] < 0.5
        stats = job_stats.get(NAME)
        assert stats is not None
        assert (stats.failures, stats.consecutive_failures) == (stats.runs, stats.runs)
        assert stats.last_error == "RuntimeError: Postgres is down"

    async def test_a_streak_of_failures_is_logged_once_and_its_end_once(self) -> None:
        loop = EagerTicker(interval=0.05)
        loop.failing = RuntimeError("Postgres is down")
        with patch("api.core.loops.logger") as log:
            async with running(loop):
                await until(lambda: len(loop.started) == 3)
                loop.failing = RuntimeError("Postgres refused it")
                await until(lambda: len(loop.started) == 5)
                loop.failing = None
                await until(lambda: len(loop.started) == 7)

        # At error when the streak starts and when its error changes; at debug while it repeats.
        assert [call.kwargs["error"] for call in log.error.call_args_list] == ["Postgres is down", "Postgres refused it"]
        assert all(call.args == ("Loop cycle failed",) for call in log.debug.call_args_list)
        assert [call.args for call in log.info.call_args_list] == [("Loop recovered",)]

    async def test_a_cycle_that_did_nothing_is_idle_and_no_wait_is_timed(self) -> None:
        loop = EagerTicker(interval=0.2, cycle_seconds=0.02)
        async with running(loop):
            await until(lambda: len(loop.ended) == 2)
            loop.worked = False
            await until(lambda: len(loop.ended) == 4)

        stats = job_stats.get(NAME)
        assert stats is not None and stats.runs >= 4 and stats.idle_runs >= 1
        # The cycle's 20 ms, not the 200 ms it waited before it.
        assert stats.max_duration_ms is not None and 15 <= stats.max_duration_ms < 150

    async def test_the_cadence_is_declared_before_the_first_cycle(self) -> None:
        loop = Ticker(interval=60)
        async with running(loop):
            await until(lambda: job_stats.get(NAME) is not None)
            stats = job_stats.get(NAME)

        assert stats is not None and stats.interval_seconds == 60 and stats.runs == 0

    async def test_a_stop_ends_the_wait_at_once(self) -> None:
        loop = Ticker(interval=60)
        task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.1)

        stopped = time.monotonic()
        await loop.stop()
        await asyncio.wait_for(task, timeout=1)

        assert time.monotonic() - stopped < 0.2
        assert loop.started == [] and loop.stopped

    async def test_a_stop_ends_a_back_off_at_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wait the wake does not end: the stop does, as it ends a standby and a blocked read."""
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 30.0)
        loop = Woken()
        loop.failures_left = 1
        task = asyncio.create_task(loop.run())
        loop.wake()
        await until(lambda: len(loop.started) == 1)
        await asyncio.sleep(0.05)

        stopped = time.monotonic()
        await loop.stop()
        await asyncio.wait_for(task, timeout=1)

        assert time.monotonic() - stopped < 0.2

    async def test_a_stop_lets_the_cycle_under_way_finish(self) -> None:
        loop = EagerTicker(interval=60, cycle_seconds=0.3)
        task = asyncio.create_task(loop.run())
        await until(lambda: len(loop.started) == 1)

        await loop.stop()
        await asyncio.wait_for(task, timeout=1)

        assert len(loop.ended) == 1 and loop.ended[0] - loop.started[0] >= 0.29

    async def test_a_loop_stopped_before_it_runs_never_cycles(self) -> None:
        loop = EagerTicker(interval=60)
        await loop.stop()

        await asyncio.wait_for(loop.run(), timeout=0.5)

        assert loop.started == []

    def test_a_timer_loop_follows_no_stream(self) -> None:
        """Only a loop that reads a stream from a position of its own has one to report in its process's snapshot."""
        assert Ticker(interval=60).streams() == {}
        assert Woken().streams() == {}

    async def test_a_wake_brings_a_cycle_at_once_and_one_during_a_cycle_brings_another(self) -> None:
        loop = Ticker(interval=60, cycle_seconds=0.1)
        async with running(loop):
            await asyncio.sleep(0.05)
            woken = time.monotonic()
            loop.wake()
            await until(lambda: len(loop.started) == 1)
            assert loop.started[0] - woken < 0.1
            loop.wake()  # while the cycle runs
            await until(lambda: len(loop.started) == 2)
            await asyncio.sleep(0.3)

        assert loop.started[1] - loop.ended[0] < 0.1
        assert len(loop.started) == 2  # each wake served once, then the interval again


class TestBackOff:
    async def test_a_loop_with_no_timer_tries_again_after_a_back_off_that_doubles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.1)
        monkeypatch.setattr(streams, "RETRY_MAX_SECONDS", 0.2)
        loop = Woken()
        loop.failures_left = 3
        async with running(loop):
            loop.wake()
            await until(lambda: len(loop.started) == 4)

        gaps = [later - earlier for earlier, later in zip(loop.started, loop.started[1:], strict=False)]
        # 0.1 s after the first failure, then 0.2 s (the cap) after each further one.
        assert 0.09 <= gaps[0] < 0.18
        assert 0.19 <= gaps[1] < 0.3 and 0.19 <= gaps[2] < 0.3

    async def test_a_wait_that_fails_is_counted_with_no_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The job stats time the work, never a wait: a read that failed after waiting counts as a failure of 0 ms."""
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.05)
        loop = Woken()

        async def failing_wait() -> None:
            await asyncio.sleep(0.1)
            raise ConnectionError("Connection reset by peer")

        monkeypatch.setattr(loop, "_wait", failing_wait)
        async with running(loop):
            await until(lambda: (stats := job_stats.get(NAME)) is not None and stats.failures >= 2)

        stats = job_stats.get(NAME)
        assert stats is not None and stats.max_duration_ms == 0.0 and loop.started == []


class TestLeased:
    async def test_a_standby_runs_no_cycle_and_its_failure_streak_is_history(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        await redis_client.client.set(KEY, "peer", px=60_000)
        job_stats.record(NAME, 1.0, "RuntimeError: from the last lead")
        loop = EagerTicker(interval=0.05, lease=kept(redis_client, leases))
        async with running(loop):
            await asyncio.sleep(POLL_MS * 4 / 1000)

        stats = job_stats.get(NAME)
        assert loop.started == [] and loop.terms == []
        assert stats is not None and stats.consecutive_failures == 0

    async def test_a_stop_ends_a_standby_at_once(self, redis_client: RedisClient, leases: list[Lease]) -> None:
        await redis_client.client.set(KEY, "peer", px=60_000)
        loop = EagerTicker(interval=0.05, lease=kept(redis_client, leases))
        task = asyncio.create_task(loop.run())
        await asyncio.sleep(POLL_MS * 2 / 1000)

        stopped = time.monotonic()
        await loop.stop()
        await asyncio.wait_for(task, timeout=1)

        assert time.monotonic() - stopped < 0.2 and loop.started == []

    async def test_it_cycles_once_it_leads_and_each_new_lead_counts_its_first_cycle_afresh(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        await redis_client.client.set(KEY, "peer", px=60_000)
        lease = kept(redis_client, leases)
        loop = EagerTicker(interval=60, lease=lease)
        async with running(loop):
            await asyncio.sleep(POLL_MS * 2 / 1000)
            await redis_client.client.delete(KEY)
            await until(lambda: len(loop.started) == 1, timeout=1)
            assert loop.terms == [1]
            # A peer takes the lease over; when it lets go, this process leads a new term, at once.
            await redis_client.client.set(KEY, "peer", px=300)
            await until(lambda: lease.term == 2, timeout=2)
            await until(lambda: len(loop.started) == 2, timeout=1)

        assert loop.terms == [1, 2]

    async def test_a_wipe_ends_a_long_wait_within_a_refresh_and_the_new_term_cycles(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        """The defect of the feed loop that waited out its 20 s read on streams a FLUSHALL had deleted."""
        lease = kept(redis_client, leases)
        loop = EagerTicker(interval=30, lease=lease)
        async with running(loop):
            await until(lambda: len(loop.started) == 1, timeout=1)
            wiped = time.monotonic()
            await redis_client.client.flushdb()
            await until(lambda: len(loop.started) == 2, timeout=2)

        # The next refresh finds the key gone, the next take starts term 2, and term 2 cycles at once.
        assert loop.started[1] - wiped < (REFRESH_MS + POLL_MS) / 1000 + 0.3
        assert loop.terms == [1, 2]

    async def test_a_cycle_under_way_runs_to_its_end_when_the_lead_ends(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        lease = kept(redis_client, leases)
        loop = EagerTicker(interval=60, lease=lease, cycle_seconds=0.5)
        async with running(loop):
            await until(lambda: len(loop.started) == 1, timeout=1)
            await redis_client.client.set(KEY, "peer", px=60_000)
            await until(lambda: not lease.is_held, timeout=1)
            await until(lambda: len(loop.ended) == 1, timeout=1)
            await asyncio.sleep(0.2)

        assert loop.ended[0] - loop.started[0] >= 0.49
        assert len(loop.started) == 1  # standing by since

    async def test_a_term_that_failed_to_begin_is_begun_again_after_the_back_off(
        self, redis_client: RedisClient, leases: list[Lease], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not at once, which would spin while Redis is away; the second term too, whose first is long over."""
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.1)
        lease = kept(redis_client, leases)
        loop = EagerTicker(interval=60, lease=lease)
        attempts: dict[int, list[float]] = {1: [], 2: []}
        begin = EagerTicker._on_term

        async def flaky(self: EagerTicker, term: int) -> None:
            attempts[term].append(time.monotonic())
            if len(attempts[term]) < 3:
                raise ConnectionError("Redis is down")
            await begin(self, term)

        monkeypatch.setattr(EagerTicker, "_on_term", flaky)
        async with running(loop):
            await until(lambda: len(loop.started) == 1, timeout=2)
            await redis_client.client.set(KEY, "peer", px=300)
            await until(lambda: len(loop.started) == 2, timeout=3)

        assert loop.terms == [1, 2]
        for times in attempts.values():
            assert len(times) == 3
            assert times[1] - times[0] >= 0.09 and times[2] - times[1] >= 0.19
