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

There is one registry per process, exposed via the ``@lru_cache``-d
:func:`get_job_stats` factory and the ``job_stats`` alias - same pattern as
``get_event_bus()`` / ``event_bus`` and ``get_settings()`` / ``settings``::

    from api.core.job_stats import job_stats
    from api.core.workers import WorkerName

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
    runs: int = 0
    failures: int = 0
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
    def avg_duration_ms(self) -> float | None:
        """Mean cycle duration, or None before the first cycle completes."""
        return self.total_duration_ms / self.runs if self.runs else None


class JobRun:
    """Times one cycle and files it with the registry on the way out.

    Returned by :meth:`JobStatsRegistry.track`; not constructed directly.
    """

    def __init__(self, registry: JobStatsRegistry, name: str) -> None:
        self._registry = registry
        self._name = name
        self._started = 0.0

    def __enter__(self) -> JobRun:
        self._started = time.perf_counter()
        return self

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
        self._registry.record(self._name, duration_ms, error)
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

    def record(self, name: str, duration_ms: float, error: str | None = None) -> None:
        """Record one finished cycle of ``name``."""
        stats = self._stats.setdefault(name, JobStats(name=name))
        stats.runs += 1
        stats.last_run_at = utc_now()
        stats.last_duration_ms = duration_ms
        stats.total_duration_ms += duration_ms
        stats.max_duration_ms = max(stats.max_duration_ms or 0.0, duration_ms)
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
