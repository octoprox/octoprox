# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Redis leases: the controller's leadership, kept by one task that takes the lease whenever it is free and refreshes it.

This is not fenced consensus: a write that must never come from two processes checks the lease in the same Redis step.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import structlog

from api.core.logging import error_text
from api.db.redis import LEASE_KEY, RedisClient

logger = structlog.get_logger()

# How often a standby asks for the lease, in milliseconds. A holder that
# died leaves the key to expire within its TTL, so a standby leads within the
# TTL plus one poll; a SET NX a second per standby costs Redis nothing.
STANDBY_POLL_MS = 1000

# How long a lease holds without a refresh, in milliseconds. It must outlast
# the longest stretch one synchronous step can hold the holder's event loop,
# when no refresh can run: a compile of 100k proxies holds it for seconds
# (over 3 s measured on a fast core), and a lease that expires under it is
# taken by a standby that then stalls the same way. A holder that died is
# replaced within this plus a poll.
LEASE_TTL_MS = 30_000

# How long a take or a refresh waits for its reply, in milliseconds, apart
# from how often the holder refreshes. A stall of the holder's event loop
# (that compile) must not cut a reply that arrived meanwhile, which costs a
# warning and a moment of "Redis unreachable"; and a refresh that hangs this
# long still leaves two more within the cover of the one before.
LEASE_REPLY_MS = 10_000

# Lua: SET NX PX, or PEXPIRE when the key already holds this holder's id (a
# take whose reply was lost). 1 when the caller holds the lease now.
_TAKE_SCRIPT = """
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'PX', ARGV[2]) then
    return 1
end
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
    return 1
end
return 0
"""

# Lua: PEXPIRE iff the current value equals the holder id.
_REFRESH_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
else
    return 0
end
"""

# Lua: DEL iff the current value equals the holder id.
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


class Lease:
    """A refreshing best-effort Redis lease, owner-checked: a late refresh or release never touches a peer's lease.

    A holder that dies stops refreshing, so its key expires after ``ttl_ms`` and a standby takes it at its next poll.

    Args:
        name: what the key is named after: ``LEASE_KEY`` for it, ``o6:lease:<name>``.
        owner_id: the lease's value while this process holds it, unique to the process: its ``instance_id``.
        ttl_ms: how long the lease holds without a refresh: longer than any synchronous step of the holder's loop.
        refresh_ms: how often the holder refreshes; under ``ttl_ms``.
        poll_ms: how often a standby asks for the lease.
        reply_ms: how long a take or a refresh waits for its reply before the keeper asks again.
    """

    def __init__(
        self,
        redis_client: RedisClient,
        name: str,
        owner_id: str,
        ttl_ms: int = LEASE_TTL_MS,
        refresh_ms: int = 2000,
        *,
        poll_ms: int = STANDBY_POLL_MS,
        reply_ms: int = LEASE_REPLY_MS,
    ) -> None:
        if refresh_ms >= ttl_ms:
            raise ValueError("refresh_ms must be less than ttl_ms")
        self._redis = redis_client
        self._name = name
        self._key = LEASE_KEY.format(name=name)
        self._owner_id = owner_id
        self._ttl_ms = ttl_ms
        self._refresh_ms = refresh_ms
        self._poll_ms = poll_ms
        self._reply_ms = reply_ms
        self._held = False
        # When the last take or refresh that worked stops covering the lead
        # (monotonic): when it was sent, plus the TTL less a refresh interval.
        self._valid_until = 0.0
        self._term = 0
        # What wait_leading() waits on: set by a take or a refresh that works,
        # and cleared by wait_leading() when it finds this process not
        # leading. A lead ends in several ways, and one of them, running out
        # (is_held is time-based), is marked by nothing, so the waiter checks.
        self._leading = asyncio.Event()
        # What wait_lost() waits on: set whenever a lead ends by an event
        # (_end_lead). A lead that runs out is marked by nothing, so the
        # waiter also wakes when the cover ends.
        self._ended = asyncio.Event()
        # Takes the lease when it is free and refreshes it while held, until released.
        self._keeper: asyncio.Task[None] | None = None
        # Why the last exchange with Redis failed; None once one works again.
        # The keeper talks to Redis every second or two, so this is how the
        # controller knows Redis is unreachable even while it stands by.
        self.last_error: str | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def key(self) -> str:
        """The Redis key holding the lease: what a fenced write checks."""
        return self._key

    @property
    def is_held(self) -> bool:
        """Whether this process leads now: it took the lease, and the last take or refresh that worked covers now.

        Refreshes that fail meanwhile do not end the lead before then; one
        that works afterwards extends the same term, since the key named this
        process throughout.
        """
        return self._held and time.monotonic() < self._valid_until

    @property
    def term(self) -> int:
        """How many times this process has taken the lease: a new value is a lead not yet acted on."""
        return self._term

    def keep(self) -> None:
        """Hold the lease whenever it is free, from now until :meth:`release`; calling it again does nothing.

        A standby asks for it every ``poll_ms``, and the holder refreshes it every ``refresh_ms`` and asks again as soon
        as it has lost it.
        """
        if self._keeper is None:
            self._keeper = asyncio.create_task(self._keep_loop(), name=f"lease:{self._name}")

    async def wait_leading(self) -> None:
        """Return once this process leads (:attr:`is_held`), at once while it does.

        The keeper's next take or refresh that works wakes it; a refresh resumes the same :attr:`term`, the key having
        named this process throughout, so compare terms to tell a new lead from one already acted on.
        """
        while not self.is_held:
            # Still set by a lead that has ended since: waiting on it would
            # return at once, and a caller's loop would spin.
            self._leading.clear()
            await self._leading.wait()

    async def wait_lost(self, term: int) -> None:
        """Return once this process no longer leads in ``term``: at once when it does not, else when the lead ends.

        A lead ends when a refresh or a fenced write finds the key gone or another holder's, at a release, and when
        the last take or refresh that worked stops covering it.
        """
        while self.is_held and self._term == term:
            # Cleared only while the lead goes on: every way it ends sets the
            # event after the lead is over, so no waiter misses it.
            self._ended.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._valid_until - time.monotonic()):
                    await self._ended.wait()

    def _end_lead(self) -> None:
        """Stop leading in the current term, and wake every :meth:`wait_lost`."""
        self._held = False
        self._ended.set()

    async def _keep_loop(self) -> None:
        while True:
            if self._held:
                await asyncio.sleep(self._refresh_ms / 1000.0)
                if self._held:
                    await self._refresh()
            elif not await self._take():
                await asyncio.sleep(self._poll_ms / 1000.0)

    async def _take(self) -> bool:
        """Take the lease when it is free or still names this process; True when it holds it now, as a new term."""
        sent = time.monotonic()
        try:
            taken = await self._ask(_TAKE_SCRIPT)
        except Exception as exc:
            self._failed("Lease acquire failed", exc)
            return False
        self.last_error = None
        if taken == 0:
            return False
        self._held = True
        self._term += 1
        self._covered(sent)
        logger.info("Lease acquired", name=self._name, owner=self._owner_id, term=self._term)
        return True

    async def _refresh(self) -> None:
        """Extend the lease while Redis still names this holder; a key gone or another holder's gives it up.

        A refresh that raises gives nothing up: the lead lasts while the last
        one that worked covers it (:attr:`is_held`), and the keeper tries again.
        """
        sent = time.monotonic()
        try:
            refreshed = await self._ask(_REFRESH_SCRIPT)
        except Exception as exc:
            self._failed("Lease refresh raised", exc)
            return
        self.last_error = None
        if refreshed == 0:
            logger.warning("Lease lost during refresh", name=self._name)
            self._end_lead()
            return
        self._covered(sent)

    async def _ask(self, script: str) -> int:
        """Run the take or the refresh script, waiting ``reply_ms`` at most for its reply.

        A reply cut off leaves the outcome unknown, which both scripts allow for: the next tick asks again, and finds
        the key still naming this process if the unanswered one landed.
        """
        try:
            async with asyncio.timeout(self._reply_ms / 1000.0):
                reply = await self._redis.client.eval(script, 1, self._key, self._owner_id, str(self._ttl_ms))
        except TimeoutError:
            # The deadline's own: redis-py raises redis.exceptions.TimeoutError, not this one.
            raise TimeoutError(f"no reply within {self._reply_ms} ms") from None
        return int(reply)

    def _covered(self, sent: float) -> None:
        """Record a take or refresh sent at ``sent`` (monotonic) that worked: the key names this process until then.

        It wakes :meth:`wait_leading`, which returns if that covers now.
        """
        self._valid_until = sent + (self._ttl_ms - self._refresh_ms) / 1000.0
        self._leading.set()

    def _failed(self, event: str, exc: Exception) -> None:
        """Note a failed exchange with Redis, logged as a warning when it starts and at debug while it repeats.

        The keeper retries every second through an outage; one warning says
        Redis went away, not one a second.
        """
        error = error_text(exc)
        log = logger.warning if error != self.last_error else logger.debug
        log(event, name=self._name, error=error, exc_info=error != self.last_error)
        self.last_error = error

    def mark_lost(self) -> None:
        """Stop leading now: a fenced write found the lease in another holder's hands, or gone.

        The keeper takes it again as soon as it is free, as a new term.
        """
        if not self._held:
            return
        logger.warning("Lease lost before a fenced write", name=self._name)
        self._end_lead()

    async def release(self) -> None:
        """Stop holding the lease and keeping it, and give it up in Redis if the key still names this instance.

        Safe to call more than once; a take the stop cut off is given up too, since it may have landed.
        """
        keeper, self._keeper = self._keeper, None
        if keeper is not None:
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper
        if not self._held and keeper is None:
            return
        was_held = self._held
        self._end_lead()
        try:
            await self._redis.client.eval(
                _RELEASE_SCRIPT, 1, self._key, self._owner_id
            )
        except Exception:
            logger.warning("Lease release failed", name=self._name, exc_info=True)
        if was_held:
            logger.info("Lease released", name=self._name, owner=self._owner_id)
