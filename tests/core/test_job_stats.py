# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the per-cycle counters behind the admin worker list."""

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import pytest

from api.core.job_stats import MAX_ERROR_CHARS, JobStats, get_job_stats, job_stats
from api.core.proxy_manager import ProxyManager
from api.core.system_stats import collect_tasks
from api.core.workers import LEASE_KINDS, LEASE_WORKERS, WORKERS, WorkerName


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """One registry per process, so no test may inherit another's counters."""
    job_stats.reset()
    yield
    job_stats.reset()


class TestRegistrySingleton:
    def test_the_factory_hands_out_one_registry(self) -> None:
        """Workers file into the same registry the system view reads."""
        assert get_job_stats() is get_job_stats()
        assert get_job_stats() is job_stats


class TestTrack:
    async def test_counts_a_successful_cycle_and_times_it(self) -> None:
        # A plain `with` around an `await`: the clock needs no awaiting.
        with job_stats.track("worker"):
            await asyncio.sleep(0.01)

        stats = job_stats.get("worker")
        assert stats is not None
        assert (stats.runs, stats.failures, stats.consecutive_failures) == (1, 0, 0)
        assert stats.last_duration_ms is not None and stats.last_duration_ms >= 10
        assert stats.avg_duration_ms == stats.last_duration_ms
        assert stats.max_duration_ms == stats.last_duration_ms
        assert stats.last_run_at is not None
        assert stats.last_error is None

    async def test_counts_a_failed_cycle_and_re_raises(self) -> None:
        with pytest.raises(RuntimeError), job_stats.track("worker"):
            raise RuntimeError("redis down")

        stats = job_stats.get("worker")
        assert stats is not None
        # A failed cycle is still a cycle: it ran, it took time, it failed.
        assert (stats.runs, stats.failures, stats.consecutive_failures) == (1, 1, 1)
        assert stats.last_error == "RuntimeError: redis down"
        assert stats.last_error_at == stats.last_run_at

    async def test_a_success_clears_the_consecutive_failure_streak(self) -> None:
        for _ in range(3):
            with pytest.raises(ValueError), job_stats.track("worker"):
                raise ValueError("boom")
        with job_stats.track("worker"):
            pass

        stats = job_stats.get("worker")
        assert stats is not None
        assert (stats.runs, stats.failures) == (4, 3)
        # Cleared, so the UI says "recovered" rather than "still failing" - the
        # lifetime failure count stays for the record.
        assert stats.consecutive_failures == 0
        assert stats.last_error == "ValueError: boom"

    async def test_cancellation_is_shutdown_not_a_failure(self) -> None:
        with pytest.raises(asyncio.CancelledError), job_stats.track("worker"):
            raise asyncio.CancelledError()

        assert job_stats.get("worker") is None

    async def test_a_cycle_can_report_it_had_nothing_to_do(self) -> None:
        """An idle tick proves the loop is alive without claiming it worked."""
        with job_stats.track("worker") as run:
            run.idle()

        stats = job_stats.get("worker")
        assert stats is not None
        assert (stats.runs, stats.idle_runs, stats.working_runs) == (1, 1, 0)
        assert stats.failures == 0
        # The loop ticked, so it has a last run - it just has no timing, since
        # the early return is not a measurement of the work it skipped.
        assert stats.last_run_at is not None
        assert stats.last_duration_ms is None
        assert stats.avg_duration_ms is None
        assert stats.max_duration_ms is None

    async def test_idle_ticks_stay_out_of_the_timings(self) -> None:
        """Otherwise a mostly-idle loop reports work it does as instant."""
        job_stats.record("worker", 20.0)
        for _ in range(99):
            job_stats.record("worker", 0.01, idle=True)

        stats = job_stats.get("worker")
        assert stats is not None
        assert (stats.runs, stats.idle_runs, stats.working_runs) == (100, 99, 1)
        # 20ms, not the 0.2ms a mean over all 100 cycles would report.
        assert stats.avg_duration_ms == 20.0
        assert stats.max_duration_ms == 20.0

    async def test_a_cycle_that_raised_is_never_idle(self) -> None:
        """However early it gave up, it failed rather than found nothing."""
        with pytest.raises(RuntimeError), job_stats.track("worker") as run:
            run.idle()
            raise RuntimeError("redis down")

        stats = job_stats.get("worker")
        assert stats is not None
        assert (stats.runs, stats.idle_runs, stats.failures) == (1, 0, 1)

    async def test_workers_without_an_idle_path_report_none(self) -> None:
        """Loops that always have work keep runs and working_runs identical."""
        job_stats.record("worker", 5.0)
        job_stats.record("worker", 5.0)

        stats = job_stats.get("worker")
        assert stats is not None
        assert (stats.idle_runs, stats.working_runs) == (0, 2)

    async def test_averages_over_cycles(self) -> None:
        job_stats.record("worker", 10.0)
        job_stats.record("worker", 30.0)

        stats = job_stats.get("worker")
        assert stats is not None
        assert stats.avg_duration_ms == 20.0
        assert stats.max_duration_ms == 30.0
        assert stats.last_duration_ms == 10.0 or stats.last_duration_ms == 30.0

    async def test_counts_cycles_that_outlast_their_cadence(self) -> None:
        """Overruns are what "this job is quietly running late" looks like."""
        job_stats.declare_interval("worker", 0.05)
        job_stats.record("worker", 10.0)
        job_stats.record("worker", 80.0)

        stats = job_stats.get("worker")
        assert stats is not None
        assert stats.runs == 2
        # Only the 80ms cycle exceeded the 50ms cadence, and it is not a
        # failure - the work succeeded, it just delayed the next cycle.
        assert stats.overruns == 1
        assert stats.consecutive_overruns == 1
        assert stats.failures == 0

    async def test_a_cycle_back_within_cadence_clears_the_overrun_streak(self) -> None:
        """One slow cycle must not mark a worker as behind for the rest of its life."""
        job_stats.declare_interval("worker", 0.05)
        job_stats.record("worker", 80.0)
        assert (job_stats.get("worker") or JobStats("x")).consecutive_overruns == 1

        for _ in range(5):
            job_stats.record("worker", 10.0)

        stats = job_stats.get("worker")
        assert stats is not None
        # Back on cadence now, but the blip stays on the lifetime record.
        assert stats.consecutive_overruns == 0
        assert stats.overruns == 1
        assert stats.last_overrun_at is not None

    async def test_a_worker_without_a_cadence_never_overruns(self) -> None:
        """The pub/sub subscribers run on arrival, so there is nothing to miss."""
        job_stats.record("worker", 10_000.0)

        stats = job_stats.get("worker")
        assert stats is not None
        assert stats.interval_seconds is None
        assert stats.overruns == 0
        assert stats.consecutive_overruns == 0

    async def test_cadence_is_reported_before_the_first_cycle(self) -> None:
        """An hourly job should not look unknown for its first hour."""
        job_stats.declare_interval("worker", 3600)

        stats = job_stats.get("worker")
        assert stats is not None
        assert (stats.interval_seconds, stats.runs) == (3600, 0)

    async def test_long_errors_are_truncated(self) -> None:
        job_stats.record("worker", 1.0, "x" * 1000)

        stats = job_stats.get("worker")
        assert stats is not None and stats.last_error is not None
        assert len(stats.last_error) == MAX_ERROR_CHARS

    async def test_workers_are_counted_separately(self) -> None:
        job_stats.record("a", 1.0)
        job_stats.record("b", 1.0, "nope")

        assert set(job_stats.snapshot()) == {"a", "b"}
        assert job_stats.snapshot()["b"].failures == 1


class TestCollectTasks:
    """The worker list is the join of the asyncio task and its counters."""

    async def test_reports_description_scope_and_counters(self) -> None:
        async def loop() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(loop(), name=WorkerName.METRICS_FLUSHER)
        job_stats.record(WorkerName.METRICS_FLUSHER, 12.0)
        # Only ``background_tasks`` is read, so a real manager is overkill.
        manager: Any = SimpleNamespace(background_tasks=[task])
        try:
            (reported,) = collect_tasks(manager)
        finally:
            task.cancel()

        assert reported.state == "running"
        assert reported.description == WORKERS[WorkerName.METRICS_FLUSHER].description
        assert reported.scope == "singleton"
        assert reported.lease == "metrics_flusher"
        assert reported.runs == 1
        assert reported.idle_runs == 0
        assert reported.avg_duration_ms == 12.0

    async def test_a_worker_that_has_not_run_reports_zeroes_not_nulls(self) -> None:
        async def loop() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(loop(), name=WorkerName.HEALTH_CHECKER)
        # Only ``background_tasks`` is read, so a real manager is overkill.
        manager: Any = SimpleNamespace(background_tasks=[task])
        try:
            (reported,) = collect_tasks(manager)
        finally:
            task.cancel()

        assert (reported.runs, reported.idle_runs, reported.failures) == (0, 0, 0)
        assert reported.last_run_at is None
        assert reported.avg_duration_ms is None


class TestWorkerRegistry:
    def test_every_spawned_loop_is_described(self) -> None:
        """A loop added to ``start`` without a WORKERS entry is a blank row."""
        import inspect

        spawned = {
            WorkerName[member]
            for member in re.findall(
                r"_spawn\(\s*WorkerName\.([A-Z_]+)", inspect.getsource(ProxyManager.start)
            )
        }

        assert spawned, "no _spawn calls found - the regex needs updating"
        assert spawned <= set(WORKERS), f"undescribed workers: {spawned - set(WORKERS)}"

    def test_lease_names_resolve_back_to_their_worker(self) -> None:
        for lease, worker in LEASE_WORKERS.items():
            assert WORKERS[worker].lease == lease
            assert LEASE_KINDS[lease], f"{lease} has no human label"

    def test_singleton_workers_are_exactly_the_leased_ones(self) -> None:
        singletons = {name for name, info in WORKERS.items() if info.scope == "singleton"}
        assert singletons == set(LEASE_WORKERS.values())
