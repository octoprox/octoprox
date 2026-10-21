# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Background loops: the base every loop of a role builds on, and the family that runs on a timer.

A loop waits for its next duty or a wake, then runs one cycle; given a lease it runs only while this process leads,
and a lead that ends cuts its wait short, never its cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, ClassVar

import structlog

from api.core.job_stats import job_stats
from api.core.logging import error_label, error_text
from api.db.streams import retry_delay

if TYPE_CHECKING:
    from api.core.leadership import Lease
    from api.core.workers import WorkerName
    from api.models.system import StreamState

logger = structlog.get_logger()


class Loop(ABC):
    """A background loop: it waits until its next duty or a wake, then runs one cycle, until it is stopped.

    Args:
        name: the loop's catalogue name: its task, its job stats row and its log lines.
        lease: the lease it leads by, standing by while another process holds it; None runs it in every process.
    """

    def __init__(self, *, name: WorkerName, lease: Lease | None = None) -> None:
        self.name = name
        self._lease = lease
        # Set by wake() and stop(): the default wait ends early.
        self._woken = asyncio.Event()
        # Set by stop(), for good: every wait ends, and no cycle starts after it.
        self._halt = asyncio.Event()
        # The lease term this loop last began (_on_term); None before the first, and without a lease.
        self._term: int | None = None
        # Turns failed in a row, and why the last one did: one log line per streak.
        self._failures = 0
        self._last_error: str | None = None
        # When the cycle under way began (perf_counter), to time it should it fail; None between cycles.
        self._cycle_started: float | None = None
        # The task running run(), and the rivals every wait races: the stop, and the end of the current lead.
        self._task: asyncio.Task[Any] | None = None
        self._halted: asyncio.Future[Any] | None = None
        self._lead_end: asyncio.Future[None] | None = None
        self._lead_end_term: int | None = None

    @property
    def stopped(self) -> bool:
        """Whether :meth:`stop` was called: no cycle starts after it."""
        return self._halt.is_set()

    async def run(self) -> None:
        """Turn until :meth:`stop`: wait for the next duty or a wake, then run one cycle; a turn that fails backs off."""
        if self._halt.is_set():
            return
        self._task = asyncio.current_task()
        self._halted = asyncio.ensure_future(self._halt.wait())
        if (cadence := self._cadence()) is not None:
            job_stats.declare_interval(self.name, cadence)
        try:
            while not self._halt.is_set():
                try:
                    await self._turn()
                except Exception as exc:
                    await self._failed(exc)
        finally:
            rivals = [rival for rival in (self._halted, self._lead_end) if rival is not None]
            for rival in rivals:
                rival.cancel()
            await asyncio.gather(*rivals, return_exceptions=True)

    async def stop(self) -> None:
        """Stop for good: the wait under way ends and no cycle starts after it; a cycle under way runs to its end."""
        self._halt.set()
        self._woken.set()

    def wake(self) -> None:
        """Run a cycle now rather than at the next duty: the default wait ends early, a blocked stream read does not."""
        self._woken.set()

    def streams(self) -> dict[str, StreamState]:
        """How far this loop has read each stream it follows from a position of its own, by stream name; none by default."""
        return {}

    # --- What a subclass fills in -------------------------------------------------------------------

    @abstractmethod
    async def _cycle(self) -> bool:
        """Do what is due; whether anything was, since a cycle that did nothing counts as idle."""

    def _next_duty(self) -> float | None:
        """Seconds until a cycle is due, 0 when one is due now; None when only a wake brings one.

        By default only a wake does, or the back-off after a failure, which tries the cycle again.
        """
        return 0.0 if self._failures else None

    async def _on_term(self, term: int) -> None:
        """Begin a lease term this loop has not acted on: what every new lead starts with; an override calls this one.

        A term that ran out and resumed is the same term, and begins nothing again; raising begins it again next turn.
        """
        return None

    def _cadence(self) -> float | None:
        """The cadence the job stats hold this loop's cycles to, declared as it starts; None for none."""
        return None

    def _retry_delay(self, failures: int) -> float:
        """Seconds the loop waits after its ``failures``-th failed turn in a row: 1 s, doubling, at most 30 s."""
        return retry_delay(failures)

    async def _wait(self) -> None:
        """Wait until the next duty or a wake: what a turn does before its cycle."""
        duty = self._next_duty()
        if duty is not None and duty <= 0:
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(duty):
                await self._woken.wait()

    def _count_cycle(self, duration_ms: float, worked: bool) -> None:
        """File a cycle that returned in the job stats: a run of the loop, idle when it did nothing."""
        job_stats.record(self.name, duration_ms, idle=not worked)

    # --- The turn -----------------------------------------------------------------------------------

    async def _turn(self) -> None:
        """One turn: lead, wait, then a cycle, unless the loop stopped or the lead ended meanwhile."""
        term = await self._lead()
        if self._halt.is_set():
            return
        if not await self._raced(self._wait(), term) or self._halt.is_set() or not self._leads(term):
            return
        # A wake that came before the cycle starts is served by it.
        self._woken.clear()
        await self._counted_cycle()
        self._recovered()

    async def _lead(self) -> int | None:
        """Stand by until this process leads, then begin a term not begun yet; the term, None without a lease."""
        lease = self._lease
        if lease is None:
            return None
        if not lease.is_held:
            job_stats.standing_by(self.name)
            if not await self._raced(lease.wait_leading(), None):
                return None
        term = lease.term
        if term != self._term:
            await self._on_term(term)
            self._term = term
        return term

    def _leads(self, term: int | None) -> bool:
        """Whether this process still leads in ``term``; always without a lease."""
        lease = self._lease
        return lease is None or (lease.is_held and lease.term == term)

    async def _raced(self, wait: Awaitable[Any], term: int | None) -> bool:
        """Await ``wait`` unless the loop stops, or the lead in ``term`` ends, first; whether it ran to its end.

        A wait cut short is cancelled and awaited, so a cut read has closed its connection before the loop goes on.
        """
        task = asyncio.ensure_future(wait)
        rivals = [rival for rival in (self._halted, self._ending(term)) if rival is not None]
        try:
            await asyncio.wait([task, *rivals], return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.wait([task])
        if task.cancelled():
            return False
        task.result()
        return True

    def _ending(self, term: int | None) -> asyncio.Future[None] | None:
        """What ends with the lead in ``term``, kept across waits; None without a lease or a term."""
        if self._lease is None or term is None:
            return None
        if self._lead_end is None or self._lead_end.done() or self._lead_end_term != term:
            if self._lead_end is not None:
                self._lead_end.cancel()
            self._lead_end = asyncio.ensure_future(self._lease.wait_lost(term))
            self._lead_end_term = term
        return self._lead_end

    async def _counted_cycle(self) -> None:
        """Run one cycle and file it (:meth:`_count_cycle`); one that raises is filed as it fails."""
        started = self._cycle_started = time.perf_counter()
        worked = await self._cycle()
        self._cycle_started = None
        self._count_cycle((time.perf_counter() - started) * 1000.0, worked)

    async def _failed(self, error: Exception) -> None:
        """File, count and log a turn that raised, then back off; only a cycle is timed, never a wait."""
        started, self._cycle_started = self._cycle_started, None
        duration_ms = 0.0 if started is None else (time.perf_counter() - started) * 1000.0
        job_stats.record(self.name, duration_ms, error_label(error))
        self._failures += 1
        reason = error_text(error)
        # At error when a streak starts or its error changes, at debug while it repeats.
        if reason != self._last_error:
            logger.error("Loop cycle failed", loop=self.name, error=reason, failures=self._failures, exc_info=error)
        else:
            logger.debug("Loop cycle failed", loop=self.name, error=reason, failures=self._failures)
        self._last_error = reason
        # Cut short when the lead it runs in ends; a term that failed to begin is the lease's, not the loop's.
        lease = self._lease
        term = lease.term if lease is not None and lease.is_held else None
        await self._raced(asyncio.sleep(self._retry_delay(self._failures)), term)

    def _recovered(self) -> None:
        """A cycle ran: a streak of failures before it is over, which is logged once."""
        if self._failures:
            logger.info("Loop recovered", loop=self.name, failures=self._failures)
        self._failures = 0
        self._last_error = None

    async def _run_ended(self) -> None:
        """Wait until :meth:`run` has returned, unless called from it: once stopped, it ends after its cycle under way."""
        task = self._task
        if task is not None and task is not asyncio.current_task() and not task.done():
            await asyncio.wait([task])


class PeriodicLoop(Loop):
    """A loop with a cycle every ``interval`` seconds from the start of the one before; a cycle that fails waits for the next.

    A cycle that outlasts the interval is followed at once, and counted in the job stats as an overrun.
    """

    # Whether the first cycle, of the loop and of each new lead, comes at once rather than one interval later.
    first_at_once: ClassVar[bool] = False

    def __init__(self, *, name: WorkerName, interval: float, lease: Lease | None = None) -> None:
        super().__init__(name=name, lease=lease)
        self.interval = interval
        # When the next cycle is due (monotonic): set at the start, at each new lead, and as each cycle starts.
        self._due_at = 0.0

    async def run(self) -> None:
        """Turn until :meth:`stop`, the first cycle at once or one interval after the start (``first_at_once``)."""
        self._schedule_first()
        await super().run()

    async def _on_term(self, term: int) -> None:
        """A new lead counts its first cycle afresh, at once or one interval later."""
        await super()._on_term(term)
        self._schedule_first()

    def _next_duty(self) -> float:
        """Seconds until the next cycle is due."""
        return max(0.0, self._due_at - time.monotonic())

    def _cadence(self) -> float:
        """The interval: a cycle longer than it stretched the period."""
        return self.interval

    def _retry_delay(self, failures: int) -> float:
        """The back-off, at most an interval: a cycle that failed waits for its next interval, as any cycle does."""
        return min(super()._retry_delay(failures), self.interval)

    async def _counted_cycle(self) -> None:
        """Run one cycle, the next one due an interval after this one starts."""
        self._due_at = time.monotonic() + self.interval
        await super()._counted_cycle()

    def _schedule_first(self) -> None:
        """Set when the first cycle of the loop, or of a lead, is due."""
        self._due_at = time.monotonic() + (0.0 if self.first_at_once else self.interval)
