# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Redis lease against the real Redis test container, timings shrunk so a failover takes a test's time.

``CLIENT PAUSE`` holds Redis's replies back, standing for a take or a refresh that gets no reply.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest

from api.core.leadership import LEASE_REPLY_MS, LEASE_TTL_MS, Lease
from api.db.redis import LEASE_KEY, RedisClient
from tests.waits import until

KEY = LEASE_KEY.format(name="test")
TTL_MS, REFRESH_MS, POLL_MS = 600, 200, 100
# How long CLIENT PAUSE holds every reply back: several refresh intervals.
PAUSE_MS = 1500
# A reply deadline well inside the pause, for the leases whose asks it cuts.
REPLY_MS = 300


def kept(redis_client: RedisClient, owner: str) -> Lease:
    return Lease(redis_client, name="test", owner_id=owner, ttl_ms=TTL_MS, refresh_ms=REFRESH_MS, poll_ms=POLL_MS)


async def still_waiting(lease: Lease, seconds: float = POLL_MS * 3 / 1000) -> bool:
    """Whether ``wait_leading()`` still waits after ``seconds``, as a loop standing by would."""
    try:
        async with asyncio.timeout(seconds):
            await lease.wait_leading()
    except TimeoutError:
        return True
    return False


@contextlib.asynccontextmanager
async def waiting(lease: Lease) -> AsyncIterator[asyncio.Task[None]]:
    """``wait_leading()`` awaited in a task, as a loop standing by does; cancelled on the way out if it still waits."""
    waiter = asyncio.create_task(lease.wait_leading())
    try:
        yield waiter
    finally:
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter


@pytest.fixture
async def leases(redis_client: RedisClient) -> AsyncIterator[list[Lease]]:
    """Leases the test creates; every one is released at the end."""
    created: list[Lease] = []
    yield created
    for lease in created:
        await lease.release()


async def held(redis_client: RedisClient, leases: list[Lease]) -> Lease:
    """A kept lease, once this process holds it, that refreshes only every 30 s: no refresh lands mid-test."""
    lease = Lease(redis_client, name="test", owner_id="me", ttl_ms=60_000, refresh_ms=30_000, poll_ms=POLL_MS)
    leases.append(lease)
    lease.keep()
    await until(lambda: lease.is_held)
    return lease


class TestTransitionLogging:
    async def test_taking_and_releasing_the_lease_is_logged_at_info(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        """A controller taking over or stepping down is worth a line."""
        with patch("api.core.leadership.logger") as logger:
            lease = await held(redis_client, leases)
            await lease.release()

        assert [call.args[0] for call in logger.info.call_args_list] == ["Lease acquired", "Lease released"]
        assert not logger.debug.called


class TestKept:
    async def test_a_free_lease_is_taken_at_once_and_refreshed_while_held(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held, timeout=1)
        assert lease.term == 1
        # Held well past its TTL: the keeper refreshes it.
        await asyncio.sleep(TTL_MS * 3 / 1000)
        assert lease.is_held and await redis_client.client.get(KEY) == "leader"
        assert lease.term == 1

    async def test_a_standby_leads_within_the_ttl_and_a_poll_of_a_holder_that_stopped(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        leader, standby = kept(redis_client, "leader"), kept(redis_client, "standby")
        leases += [leader, standby]
        leader.keep()
        await until(lambda: leader.is_held)
        standby.keep()
        await asyncio.sleep(POLL_MS * 3 / 1000)
        assert not standby.is_held and standby.term == 0
        # The leader's process freezes: its keeper stops, the key is left to expire.
        assert leader._keeper is not None
        leader._keeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await leader._keeper
        took = await until(lambda: standby.is_held)
        assert took <= (TTL_MS + POLL_MS) / 1000 + 0.3
        assert standby.term == 1 and await redis_client.client.get(KEY) == "standby"

    async def test_a_graceful_release_hands_over_within_a_poll(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        leader, standby = kept(redis_client, "leader"), kept(redis_client, "standby")
        leases += [leader, standby]
        leader.keep()
        await until(lambda: leader.is_held)
        standby.keep()
        await leader.release()
        assert not leader.is_held and leader._keeper is None
        took = await until(lambda: standby.is_held)
        assert took <= POLL_MS / 1000 + 0.3
        # Released for good: the old leader does not stand again.
        await asyncio.sleep(POLL_MS * 3 / 1000)
        assert not leader.is_held and await redis_client.client.get(KEY) == "standby"

    async def test_release_never_deletes_another_holders_lease(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        standby = kept(redis_client, "standby")
        leases.append(standby)
        await redis_client.client.set(KEY, "leader", px=60_000)
        standby.keep()
        await asyncio.sleep(POLL_MS * 2 / 1000)
        await standby.release()
        assert await redis_client.client.get(KEY) == "leader"

    async def test_a_lost_lease_is_taken_again_as_a_new_term(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held)
        # A peer held it for a while (the key expired under a stall and was taken).
        await redis_client.client.set(KEY, "peer", px=300)
        await until(lambda: not lease.is_held)  # the next refresh finds the peer
        await until(lambda: lease.is_held)  # and the keeper takes it once the peer's lapses
        assert lease.term == 2

    async def test_a_fenced_write_that_found_another_holder_gives_the_lease_up(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held)
        await redis_client.client.set(KEY, "peer", px=60_000)
        lease.mark_lost()
        assert not lease.is_held
        await asyncio.sleep(POLL_MS * 3 / 1000)
        # The keeper keeps asking, and the peer keeps it.
        assert not lease.is_held and await redis_client.client.get(KEY) == "peer"

    async def test_an_unreachable_redis_is_reported_once_and_cleared(
        self, redis_client: RedisClient, leases: list[Lease], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lease = kept(redis_client, "leader")
        leases.append(lease)
        await redis_client.client.set(KEY, "peer", px=60_000)
        original = redis_client.client.eval
        calls = 0

        async def failing_eval(*args: object, **kwargs: object) -> object:
            nonlocal calls
            calls += 1
            raise ConnectionError("Redis is down")

        monkeypatch.setattr(redis_client.client, "eval", failing_eval)
        with patch("api.core.leadership.logger") as logger:
            lease.keep()
            await until(lambda: calls >= 3)
            assert lease.last_error == "Redis is down" and not lease.is_held
            # One warning for the outage, not one per poll.
            assert [call.args[0] for call in logger.warning.call_args_list] == ["Lease acquire failed"]
        monkeypatch.setattr(redis_client.client, "eval", original)
        await redis_client.client.delete(KEY)
        await until(lambda: lease.is_held)
        assert lease.last_error is None

    async def test_a_refresh_that_fails_ends_nothing_while_the_last_one_covers_the_lead(
        self, redis_client: RedisClient, leases: list[Lease], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reply that timed out after Redis extended the key must not cost the controller a new term."""
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held)
        original = redis_client.client.eval
        failed = 0

        async def timing_out(*args: object, **kwargs: object) -> object:
            nonlocal failed
            failed += 1
            reply = await original(*args, **kwargs)  # type: ignore[misc]
            if failed == 1:
                raise TimeoutError("Timeout reading from socket")
            return reply

        monkeypatch.setattr(redis_client.client, "eval", timing_out)
        await until(lambda: failed >= 3)
        assert lease.is_held and lease.term == 1
        assert lease.last_error is None

    async def test_a_leader_cut_off_from_redis_stops_leading_when_its_lease_runs_out_then_leads_a_new_term(
        self, redis_client: RedisClient, leases: list[Lease], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held)
        original = redis_client.client.eval

        async def unreachable(*args: object, **kwargs: object) -> object:
            raise ConnectionError("Redis is down")

        monkeypatch.setattr(redis_client.client, "eval", unreachable)
        # Once the last refresh that worked no longer covers it: the TTL less a refresh after it was sent.
        stopped = await until(lambda: not lease.is_held)
        assert stopped <= (TTL_MS - REFRESH_MS) / 1000 + 0.5
        assert lease.last_error == "Redis is down"
        # The key expires meanwhile; nobody takes it here, and this process leads again as a new term.
        await asyncio.sleep(TTL_MS / 1000)
        monkeypatch.setattr(redis_client.client, "eval", original)
        await until(lambda: lease.is_held)
        assert lease.term == 2 and lease.last_error is None

    async def test_a_key_that_still_names_this_process_is_taken_at_once(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        """A take whose reply was lost leaves the key naming this process: it leads at once, not after the TTL."""
        await redis_client.client.set(KEY, "leader", px=60_000)
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        took = await until(lambda: lease.is_held, timeout=1)
        assert took < 0.3 and lease.term == 1
        assert 0 < await redis_client.client.pttl(KEY) <= TTL_MS

    def test_the_default_lease_outlasts_a_large_compile(self, redis_client: RedisClient) -> None:
        """A compile of 100k proxies holds the event loop for seconds, during which nothing refreshes."""
        lease = Lease(redis_client, name="test", owner_id="me")
        assert lease._ttl_ms == LEASE_TTL_MS >= 30_000

    async def test_keeping_twice_runs_one_keeper(self, redis_client: RedisClient, leases: list[Lease]) -> None:
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        keeper = lease._keeper
        lease.keep()
        assert lease._keeper is keeper


class TestWaitLeading:
    async def test_a_standby_waits_until_it_takes_the_lease_and_a_leader_does_not_wait(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        await redis_client.client.set(KEY, "peer", px=60_000)
        lease = kept(redis_client, "standby")
        leases.append(lease)
        lease.keep()
        async with waiting(lease) as waiter:
            await asyncio.sleep(POLL_MS * 3 / 1000)
            assert not waiter.done()
            # The peer stepped down: the keeper's next ask takes the lease and wakes the loops.
            await redis_client.client.delete(KEY)
            assert await until(waiter.done, timeout=1) <= POLL_MS / 1000 + 0.3
        assert lease.is_held and lease.term == 1
        assert not await still_waiting(lease, seconds=0.01)

    async def test_the_loops_wait_for_a_new_term_once_a_refresh_finds_another_holder(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held)
        await redis_client.client.set(KEY, "peer", px=60_000)
        await until(lambda: not lease.is_held)
        async with waiting(lease) as waiter:
            await asyncio.sleep(POLL_MS * 3 / 1000)
            assert not waiter.done()
            await redis_client.client.delete(KEY)
            await until(waiter.done, timeout=1)
        assert lease.is_held and lease.term == 2

    async def test_the_loops_wait_once_a_fenced_write_found_another_holder(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        lease = await held(redis_client, leases)
        await redis_client.client.set(KEY, "peer", px=60_000)
        lease.mark_lost()
        assert await still_waiting(lease)

    async def test_the_loops_wait_once_the_lease_is_released(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        lease = await held(redis_client, leases)
        await lease.release()
        assert await still_waiting(lease)

    async def test_a_lead_that_ran_out_while_replies_were_lost_is_waited_for_and_resumes_as_the_same_term(
        self, redis_client: RedisClient, leases: list[Lease], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redis applied every refresh but no reply came back: the key named this process throughout."""
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held)
        original = redis_client.client.eval

        async def reply_lost(*args: object, **kwargs: object) -> object:
            await original(*args, **kwargs)  # type: ignore[misc]
            raise ConnectionError("Connection reset by peer")

        monkeypatch.setattr(redis_client.client, "eval", reply_lost)
        await until(lambda: not lease.is_held)
        async with waiting(lease) as waiter:
            await asyncio.sleep(POLL_MS * 3 / 1000)
            assert not waiter.done()
            monkeypatch.setattr(redis_client.client, "eval", original)
            await until(waiter.done, timeout=1)
        assert lease.is_held and lease.term == 1


class TestReplyDeadline:
    async def test_a_standby_asks_again_while_redis_holds_its_take_unanswered(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        """Unbounded, a take would wait out the pause, and on a dead connection the client's 30 s backstop."""
        lease = Lease(
            redis_client, name="test", owner_id="standby", ttl_ms=TTL_MS, refresh_ms=REFRESH_MS, poll_ms=POLL_MS,
            reply_ms=REPLY_MS,
        )
        leases.append(lease)
        await redis_client.client.client_pause(PAUSE_MS)
        lease.keep()
        await until(lambda: lease.last_error is not None, timeout=PAUSE_MS / 1000 * 0.7)
        assert lease.last_error == f"no reply within {REPLY_MS} ms" and not lease.is_held
        # Redis answers again: the keeper, still asking, takes the lease.
        async with asyncio.timeout(PAUSE_MS / 1000 + 1):
            await lease.wait_leading()
        assert lease.term == 1 and lease.last_error is None

    async def test_a_leader_refreshes_on_through_a_redis_that_holds_its_refresh_unanswered(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        """The last refresh that worked covers the pause: the lead never stops, and goes on as the same term."""
        lease = Lease(
            redis_client, name="test", owner_id="leader", ttl_ms=5000, refresh_ms=REFRESH_MS, poll_ms=POLL_MS,
            reply_ms=REPLY_MS,
        )
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held)
        await redis_client.client.client_pause(PAUSE_MS)
        await until(lambda: lease.last_error is not None, timeout=PAUSE_MS / 1000 * 0.7)
        assert lease.last_error == f"no reply within {REPLY_MS} ms" and lease.is_held
        await until(lambda: lease.last_error is None, timeout=PAUSE_MS / 1000 + 1)
        assert lease.is_held and lease.term == 1

    async def test_the_reply_deadline_is_its_own_not_the_refresh_cadence(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        """A loop stall longer than a refresh interval must not cut a reply that arrived: no warning, no unready flicker."""
        lease = Lease(
            redis_client, name="test", owner_id="leader", ttl_ms=5000, refresh_ms=REFRESH_MS, poll_ms=POLL_MS,
            reply_ms=PAUSE_MS * 2,
        )
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held)
        errors: list[str] = []
        # Replies held back for several refresh intervals, under the reply deadline.
        await redis_client.client.client_pause(PAUSE_MS // 2)
        paused_until = time.monotonic() + PAUSE_MS / 1000
        while time.monotonic() < paused_until:
            if lease.last_error is not None:
                errors.append(lease.last_error)
            await asyncio.sleep(0.02)
        assert errors == []
        assert lease.is_held and lease.term == 1

    def test_a_lease_waits_ten_seconds_for_a_reply_by_default(self, redis_client: RedisClient) -> None:
        """Long enough for a compile that holds the event loop for seconds, short of the 28 s cover."""
        lease = Lease(redis_client, name="test", owner_id="me")
        assert lease._reply_ms == LEASE_REPLY_MS == 10_000
        assert lease._refresh_ms == 2000


class TestWaitLost:
    """``wait_lost(term)`` returns as the lead in that term ends, however it ends: what cuts a leader loop's wait short."""

    @staticmethod
    def lost(lease: Lease, term: int) -> asyncio.Task[None]:
        """``wait_lost(term)`` awaited in a task, as a leader loop's wait races it."""
        return asyncio.create_task(lease.wait_lost(term))

    async def test_a_term_this_process_does_not_lead_in_is_lost_already(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        never_kept = Lease(redis_client, name="test", owner_id="idle")
        await asyncio.wait_for(never_kept.wait_lost(0), timeout=0.1)
        lease = await held(redis_client, leases)
        await asyncio.wait_for(lease.wait_lost(lease.term - 1), timeout=0.1)
        await asyncio.wait_for(lease.wait_lost(lease.term + 1), timeout=0.1)

    async def test_it_waits_while_the_refreshes_keep_the_lead(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held)
        waiter = self.lost(lease, lease.term)
        # Several covers long: each refresh that works moves the end on.
        await asyncio.sleep(TTL_MS * 3 / 1000)
        assert not waiter.done()
        await lease.release()
        await until(waiter.done, timeout=0.1)

    async def test_a_refresh_that_finds_another_holder_ends_it_within_a_refresh(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held)
        waiter = self.lost(lease, lease.term)
        await redis_client.client.set(KEY, "peer", px=60_000)
        assert await until(waiter.done, timeout=1) <= REFRESH_MS / 1000 + 0.15
        assert not lease.is_held

    async def test_a_flush_ends_it_within_a_refresh_and_the_next_take_is_a_new_term(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        """A wipe deletes the lease key: the leader's next refresh finds it gone, and a blocked loop must not wait on."""
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held)
        term = lease.term
        waiter = self.lost(lease, term)
        await redis_client.client.flushdb()
        assert await until(waiter.done, timeout=1) <= REFRESH_MS / 1000 + 0.15
        await until(lambda: lease.is_held)
        assert lease.term == term + 1

    async def test_a_fenced_write_that_found_another_holder_ends_it_at_once(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        lease = await held(redis_client, leases)
        fenced = self.lost(lease, lease.term)
        await asyncio.sleep(0.05)
        lease.mark_lost()
        await until(fenced.done, timeout=0.05)

    async def test_a_release_ends_it_at_once(self, redis_client: RedisClient, leases: list[Lease]) -> None:
        lease = await held(redis_client, leases)
        waiters = [self.lost(lease, lease.term) for _ in range(2)]
        await asyncio.sleep(0.05)
        assert not any(waiter.done() for waiter in waiters)
        await lease.release()
        # Every loop waiting on the term is woken, not only the first.
        await until(lambda: all(waiter.done() for waiter in waiters), timeout=0.05)

    async def test_a_lead_that_runs_out_ends_it_when_the_last_refresh_stops_covering_it(
        self, redis_client: RedisClient, leases: list[Lease], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redis applies every refresh and no reply comes back: nothing marks the end but the time."""
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held)
        original = redis_client.client.eval

        async def reply_lost(*args: object, **kwargs: object) -> object:
            await original(*args, **kwargs)  # type: ignore[misc]
            raise ConnectionError("Connection reset by peer")

        monkeypatch.setattr(redis_client.client, "eval", reply_lost)
        waiter = self.lost(lease, lease.term)
        # The cover of the last refresh that worked: the TTL less a refresh interval after it was sent.
        assert await until(waiter.done, timeout=2) <= (TTL_MS - REFRESH_MS) / 1000 + 0.15
        assert not lease.is_held and lease.term == 1

    async def test_the_end_of_a_term_leaves_a_waiter_of_the_next_waiting(
        self, redis_client: RedisClient, leases: list[Lease]
    ) -> None:
        lease = kept(redis_client, "leader")
        leases.append(lease)
        lease.keep()
        await until(lambda: lease.is_held)
        first = self.lost(lease, 1)
        await redis_client.client.set(KEY, "peer", px=300)
        await until(first.done, timeout=1)
        await until(lambda: lease.is_held, timeout=2)
        assert lease.term == 2
        second = self.lost(lease, 2)
        await asyncio.sleep(TTL_MS * 2 / 1000)
        assert not second.done()
        lease.mark_lost()
        await until(second.done, timeout=0.05)
