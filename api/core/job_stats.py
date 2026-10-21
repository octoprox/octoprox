# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Per-cycle accounting for the named background loops: runs, idle runs, failures and overruns, since this process started.

A live task says nothing of whether its work happens; these counters, in memory, travel in the heartbeat and ``/metrics``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from types import TracebackType
from typing import Literal

from api.core import utc_now
from api.core.logging import error_label

# Kept short so one error string cannot bloat the stats response.
MAX_ERROR_CHARS = 300


@dataclass
class JobStats:
    """Run counters for one named worker, since this process started."""

    name: str
    # The cadence the loop was started with, declared by the loop itself so it
    # cannot drift from the value actually slept on. None for a loop that
    # declared none, which has no cadence to miss.
    interval_seconds: float | None = None
    runs: int = 0
    # Cycles that ran but had nothing to do: the buffer was empty, the head
    # was applied already. A subset of ``runs``, so ``runs`` keeps meaning
    # "the loop ticked" and ``working_runs`` answers "and something came of it".
    idle_runs: int = 0
    failures: int = 0
    # Cycles that took longer than the cadence. The loop cannot start the next
    # one until this one returns, so its effective period has stretched: its
    # work happens less often than configured.
    overruns: int = 0
    # Reset by the first cycle that fits inside the cadence again, so this is
    # "behind right now" while ``overruns`` keeps the lifetime record - one
    # slow cycle during a restart should not flag a worker for the rest of its
    # life, the same reason ``consecutive_failures`` exists.
    consecutive_overruns: int = 0
    last_overrun_at: datetime | None = None
    # Reset on the first success, so this is "is it broken right now" rather
    # than "has it ever been broken".
    consecutive_failures: int = 0
    last_run_at: datetime | None = None
    last_duration_ms: float | None = None
    max_duration_ms: float | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    # Sum, not a rolling average: avg_duration_ms is derived from it so the
    # counters stay additive and exact.
    total_duration_ms: float = 0.0

    @property
    def working_runs(self) -> int:
        """Cycles that had something to do."""
        return self.runs - self.idle_runs

    @property
    def avg_duration_ms(self) -> float | None:
        """Mean duration of a cycle that did work, or None if none has.

        Idle cycles are left out: the microsecond an early return takes is not
        a measurement of the work, and averaging it in would report a publisher
        that spends 20ms on every real flush as taking 0.1ms.
        """
        return (
            self.total_duration_ms / self.working_runs if self.working_runs else None
        )

    @property
    def interval_ms(self) -> float | None:
        return self.interval_seconds * 1000.0 if self.interval_seconds else None


class JobRun:
    """Times one cycle and files it with the registry on the way out.

    Returned by :meth:`JobStatsRegistry.track`; not constructed directly.
    """

    def __init__(self, registry: JobStatsRegistry, name: str) -> None:
        self._registry = registry
        self._name = name
        self._started = 0.0
        self._idle = False
        self._failure: str | None = None

    def __enter__(self) -> JobRun:
        self._started = time.perf_counter()
        return self

    def idle(self) -> None:
        """Mark this cycle as having found nothing to do, as the loop body learns it; a cycle that raises is never idle."""
        self._idle = True

    def fail(self, reason: str) -> None:
        """Count this cycle as failed although it returned normally, as one that gave up on purpose rather than raising.

        A heartbeat that found Postgres away, say: the work did not happen, and the System page should say so.
        """
        self._failure = reason

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        # Shutdown, not a failed cycle - and not a cycle at all.
        if exc_type is not None and issubclass(exc_type, asyncio.CancelledError):
            return False
        duration_ms = (time.perf_counter() - self._started) * 1000.0
        error = error_label(exc) if exc is not None else self._failure
        self._registry.record(
            self._name, duration_ms, error, idle=self._idle and error is None
        )
        # Never swallow: the caller's own ``except`` decides what happens to
        # the loop. Only the bookkeeping happens here.
        return False


class JobStatsRegistry:
    """The run counters of every background worker in this process."""

    def __init__(self) -> None:
        self._stats: dict[str, JobStats] = {}

    def track(self, name: str) -> JobRun:
        """Time one cycle of ``name`` and count it, failure or not."""
        return JobRun(self, name)

    def declare_interval(self, name: str, interval_seconds: float) -> None:
        """Record the cadence ``name`` was started with, declared by the loop itself so it is the one in force.

        The loop shows on the System page with its cadence before its first cycle ends, as an hourly loop or a standby
        normally does.
        """
        self._stats.setdefault(name, JobStats(name=name)).interval_seconds = interval_seconds

    def record(
        self,
        name: str,
        duration_ms: float,
        error: str | None = None,
        *,
        idle: bool = False,
    ) -> None:
        """Record one finished cycle of ``name``, failed when ``error`` says why.

        An ``idle`` cycle counts as a run but stays out of the timings, while the overrun check applies to it all the same.
        """
        stats = self._stats.setdefault(name, JobStats(name=name))
        stats.runs += 1
        stats.last_run_at = utc_now()
        if idle:
            stats.idle_runs += 1
        else:
            stats.last_duration_ms = duration_ms
            stats.total_duration_ms += duration_ms
            stats.max_duration_ms = max(stats.max_duration_ms or 0.0, duration_ms)
        interval_ms = stats.interval_ms
        if interval_ms is None:
            pass  # No cadence declared, so nothing to be late for.
        elif duration_ms > interval_ms:
            stats.overruns += 1
            stats.consecutive_overruns += 1
            stats.last_overrun_at = stats.last_run_at
        else:
            stats.consecutive_overruns = 0
        if error is None:
            stats.consecutive_failures = 0
        else:
            stats.failures += 1
            stats.consecutive_failures += 1
            stats.last_error = error[:MAX_ERROR_CHARS]
            stats.last_error_at = stats.last_run_at

    def standing_by(self, name: str) -> None:
        """``name`` is a peer's to run now, its lease having moved: a failure streak from before is history, not "failing now".

        The lifetime ``failures`` and ``last_error`` stay.
        """
        stats = self._stats.get(name)
        if stats is not None:
            stats.consecutive_failures = 0

    def get(self, name: str) -> JobStats | None:
        """Counters for one worker, or None if no cycle has completed yet."""
        return self._stats.get(name)

    def snapshot(self) -> dict[str, JobStats]:
        """All counters, keyed by worker name."""
        return dict(self._stats)

    def reset(self) -> None:
        """Drop every counter. For tests; nothing in the app calls this."""
        self._stats.clear()


@lru_cache
def get_job_stats() -> JobStatsRegistry:
    """The process-wide :class:`JobStatsRegistry`, cached so every loop files into the registry the snapshot reads.

    Tests reset it (:meth:`JobStatsRegistry.reset`) rather than rebuild it.
    """
    return JobStatsRegistry()


# Module-level convenience alias to the cached singleton - mirrors how
# ``api.core.config`` exposes both ``get_settings()`` and ``settings``.
job_stats = get_job_stats()
