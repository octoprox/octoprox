# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the event loop lag monitor: a blocked loop shows up as late timers."""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest

from api.core.config import Settings
from api.core.job_stats import job_stats
from api.core.loop_lag import LoopLagMonitor, LoopLagReading
from api.core.roles import Role
from api.core.workers import WorkerName
from api.roles import Node


@contextlib.asynccontextmanager
async def _running(monitor: LoopLagMonitor) -> AsyncIterator[None]:
    task = asyncio.create_task(monitor.run())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _next_reading(monitor: LoopLagMonitor) -> None:
    previous = monitor.last
    async with asyncio.timeout(5):
        while monitor.last is previous:
            await asyncio.sleep(0.005)


class TestLoopLagMonitor:
    async def test_no_reading_before_the_first_window_is_up(self) -> None:
        monitor = LoopLagMonitor(sample_seconds=0.01, window_seconds=60)
        async with _running(monitor):
            await asyncio.sleep(0.05)
        assert monitor.last is None

    async def test_a_blocked_loop_is_read_and_warned_about(self) -> None:
        monitor = LoopLagMonitor(sample_seconds=0.01, window_seconds=0.3, warn_ms=100)
        with patch("api.core.loop_lag.logger") as logger:
            async with _running(monitor):
                await asyncio.sleep(0.02)
                # Synchronous work holds the loop: every timer due meanwhile fires late.
                time.sleep(0.2)
                await _next_reading(monitor)

        assert monitor.last is not None
        assert monitor.last.max_ms >= 150
        assert monitor.last.mean_ms < monitor.last.max_ms
        logger.warning.assert_called_once()
        assert logger.warning.call_args.args == ("Event loop running behind",)
        assert logger.warning.call_args.kwargs["max_lag_ms"] == monitor.last.max_ms

    async def test_a_loop_keeping_up_is_not_warned_about(self) -> None:
        monitor = LoopLagMonitor(sample_seconds=0.01, window_seconds=0.1, warn_ms=1000)
        with patch("api.core.loop_lag.logger") as logger:
            async with _running(monitor):
                await _next_reading(monitor)

        assert monitor.last is not None and monitor.last.max_ms < 1000
        logger.warning.assert_not_called()

    async def test_each_window_is_a_cycle_in_the_system_view(self) -> None:
        """Without it the row reads as an event-driven loop that never ran."""
        job_stats.reset()
        try:
            monitor = LoopLagMonitor(sample_seconds=0.01, window_seconds=0.1)
            async with _running(monitor):
                await _next_reading(monitor)
            stats = job_stats.get(WorkerName.LOOP_LAG_MONITOR)
            assert stats is not None and monitor.last is not None
            assert (stats.interval_seconds, stats.runs, stats.failures) == (0.1, 1, 0)
            assert stats.last_duration_ms == monitor.last.max_ms
        finally:
            job_stats.reset()


@pytest.mark.parametrize("worst_ms", [0.0, 1234.5])
def test_the_reading_reaches_the_snapshot_and_the_metrics(worst_ms: float, test_settings: Settings) -> None:
    """The node's own monitor is what its heartbeat publishes and its /metrics renders; an echo node builds no I/O."""
    node = Node(test_settings.model_copy(update={"role": Role.ECHO}))

    node.loop_lag.last = LoopLagReading(max_ms=worst_ms, mean_ms=worst_ms / 10)
    runtime = node.snapshot().runtime
    assert (runtime.loop_lag_max_ms, runtime.loop_lag_mean_ms) == (worst_ms, worst_ms / 10)
    assert f'octoprox_event_loop_lag_max_seconds{{role="echo"}} {worst_ms / 1000}' in node.telemetry.render().decode()

    node.loop_lag.last = None
    assert node.snapshot().runtime.loop_lag_max_ms is None
    assert "octoprox_event_loop_lag" not in node.telemetry.render().decode()
