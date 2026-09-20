# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Per-cycle accounting for the named background workers.

A *job* here is one cycle of a background worker: one health-check sweep, one
metrics flush, one peer message applied. The worker itself is a task that lives
for the whole process, so "is the task still alive" (which
:func:`api.core.system_stats.collect_tasks` reads off the asyncio task) says
nothing about whether the work inside it is actually happening - a loop that
raises on every cycle and swallows the error looks identical to a healthy one.
These counters are what tells them apart.

Process-local and in-memory by design: they describe the loops of *this*
instance, reset with it, and are reported alongside ``workers.tasks``, which
has the same scope. Cluster-wide counts would need a metrics backend, and the
question this answers - "is this instance's worker doing its job" - is a
per-instance question anyway.

Granularity is the cycle, not the item. A worker that handles each item under
its own ``try`` (the auto-scaler, per connector) records a successful cycle
even when individual items failed; its own logs carry those.

A cycle that found nothing to do is still a cycle - it proves the loop is
alive - but it is also counted separately as an *idle* one. Without that split
the two questions the counters answer collapse into one number: an install
taking no traffic shows the metric-delta publisher at 17k runs a day, every one
of them an early return over an empty buffer. Loops with a no-op path say so
themselves::

    with job_stats.track(WorkerName.METRIC_DELTA_PUBLISHER) as run:
        if not await self._flush_pending_metrics():
            run.idle()

There is one registry per process, exposed via the ``@lru_cache``-d
:func:`get_job_stats` factory and the ``job_stats`` alias - same pattern as
``get_event_bus()`` / ``event_bus`` and ``get_settings()`` / ``settings``::

    from api.core.job_stats import job_stats
    from api.core.workers import WorkerName

    job_stats.declare_interval(WorkerName.HEALTH_CHECKER, self._interval)
    ...
    with job_stats.track(WorkerName.HEALTH_CHECKER):
        await self._check_all_proxies()

:meth:`JobStatsRegistry.track` is a plain (synchronous) context manager: it
starts a clock on entry and files the result on exit, neither of which needs to
await anything, so it wraps an ``await`` inside a normal ``with``. Cancellation
is not a failure: a cancelled cycle is shutdown, so it is left out of the
counters entirely.
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

# Kept short so one error string cannot bloat the stats response.
MAX_ERROR_CHARS = 300


@dataclass
class JobStats:
    """Run counters for one named worker, since this process started."""

    name: str
    # The cadence the loop was started with, declared by the loop itself so it
    # cannot drift from the value actually slept on. None for an event-driven
    # loop (the pub/sub subscribers), which has no cadence to miss.
    interval_seconds: float | None = None
    runs: int = 0
    # Cycles that ran but had nothing to do - the buffer was empty, this
    # instance owned no proxies, the snapshot was not due yet. A subset of
    # ``runs``, so ``runs`` keeps meaning "the loop ticked" and
    # ``working_runs`` answers "and something came of it".
    idle_runs: int = 0
    failures: int = 0
    # Cycles that took longer than the cadence. The loop cannot start the next
    # one until this one returns, so its effective period has stretched: for
    # the snapshotter that shows up as gaps in the trend charts, for the others
    # as work happening less often than configured.
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

    def __enter__(self) -> JobRun:
        self._started = time.perf_counter()
        return self

    def idle(self) -> None:
        """Mark this cycle as having found nothing to do.

        Call it from the loop body once it knows - typically right where the
        work function reports an empty buffer. Cycles that raise are never
        idle, however early they gave up.
        """
        self._idle = True

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
        error = f"{type(exc).__name__}: {exc}" if exc is not None else None
        self._registry.record(
            self._name, duration_ms, error, idle=self._idle and exc is None
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
        """Record the cadence ``name`` was started with.

        Called by the loop itself, next to the sleep it describes, so the
        reported cadence is the one in force rather than a config value read
        somewhere else. Registering here also means a worker appears in the
        system view with its cadence before its first cycle finishes - which
        is the normal state of an hourly job, or of a singleton standing by.
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
        """Record one finished cycle of ``name``.

        ``idle`` marks a cycle that found nothing to do. It counts as a run
        (the loop ticked) but stays out of the timings, which describe the
        work rather than the early return that skipped it. The overrun check
        still applies: a no-op that somehow outlasted the cadence is worth
        knowing about, and a fast one clears the streak like any other.
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
    """Return the process-wide :class:`JobStatsRegistry`.

    Cached (same pattern as ``get_settings`` / ``get_event_bus``) so every
    worker files into the registry the system view reads. Reset between tests
    via :meth:`JobStatsRegistry.reset` rather than rebuilding.
    """
    return JobStatsRegistry()


# Module-level convenience alias to the cached singleton - mirrors how
# ``api.core.event_bus`` exposes both ``get_event_bus()`` and ``event_bus``.
job_stats = get_job_stats()
