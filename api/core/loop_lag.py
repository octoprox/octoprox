# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""How far behind this instance's event loop runs.

Every await waits for the loop to come back round to it. On a loop pinned at
a full core that wait grows from microseconds to a large fraction of a second,
and a job made of many sequential awaits (a Redis SCAN walk, a batch of
statements) stretches with it: a full reload that takes a second when idle
has taken six minutes at peak. A timer shows the wait directly: one asked to
fire in half a second fires late by what every other await is paying.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog

from api.core.job_stats import job_stats
from api.core.workers import WorkerName

logger = structlog.get_logger()

# How often the loop is asked to run a timer. Each sample is one callback.
SAMPLE_SECONDS = 0.5
# One reading per window: the worst and the average lateness in it.
WINDOW_SECONDS = 60.0
# A window this late at worst is logged as a warning. A lease is refreshed
# every 2 s and lapses after 5 s, so a loop a second behind is heading there.
WARN_MS = 1000.0


@dataclass(frozen=True)
class LoopLagReading:
    """How late the loop ran timers over the last full window, in milliseconds."""

    max_ms: float
    mean_ms: float


class LoopLagMonitor:
    """Samples how late a timer fires and keeps the last full window's reading."""

    def __init__(
        self,
        sample_seconds: float = SAMPLE_SECONDS,
        window_seconds: float = WINDOW_SECONDS,
        warn_ms: float = WARN_MS,
    ) -> None:
        self._sample = sample_seconds
        self._window = window_seconds
        self._warn_ms = warn_ms
        # None until the first window completes: no reading is not a zero one.
        self.last: LoopLagReading | None = None

    async def run(self) -> None:
        # One cycle per window in the system view, its duration the window's
        # worst lag: the row keeps the lifetime worst and average next to the
        # live reading, and says the monitor is alive.
        job_stats.declare_interval(WorkerName.LOOP_LAG_MONITOR, self._window)
        loop = asyncio.get_running_loop()
        window_started = loop.time()
        worst = total = 0.0
        samples = 0
        while True:
            asked = loop.time()
            await asyncio.sleep(self._sample)
            now = loop.time()
            late = max(0.0, now - asked - self._sample)
            worst = max(worst, late)
            total += late
            samples += 1
            if now - window_started < self._window:
                continue
            self.last = LoopLagReading(max_ms=round(worst * 1000, 1), mean_ms=round(total / samples * 1000, 1))
            job_stats.record(WorkerName.LOOP_LAG_MONITOR, self.last.max_ms)
            if self.last.max_ms >= self._warn_ms:
                logger.warning(
                    "Event loop running behind",
                    max_lag_ms=self.last.max_ms,
                    mean_lag_ms=self.last.mean_ms,
                    window_seconds=round(now - window_started),
                )
            window_started = now
            worst = total = 0.0
            samples = 0


# Process-wide, like ``job_stats``: one loop per process, and the system view
# reads the reading without being handed the manager that runs the monitor.
loop_lag = LoopLagMonitor()
