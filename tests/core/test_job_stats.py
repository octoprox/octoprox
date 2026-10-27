# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the per-cycle counters behind the admin worker list."""

import asyncio
from typing import Any

import pytest

from api.core.job_stats import MAX_ERROR_CHARS, JobStats, get_job_stats, job_stats
from api.core.roles import JobKind, Role
from api.core.workers import WORKERS, LeaseName, WorkerName, job_producer_name, job_runner_name


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

    async def test_a_cycle_that_gave_up_without_raising_can_count_as_failed(self) -> None:
        """A flush rolled back on purpose did not happen, though it returned normally."""
        with job_stats.track("worker") as run:
            run.idle()
            run.fail("Rolled back: the lease could not be confirmed before the commit")

        stats = job_stats.get("worker")
        assert stats is not None
        assert (stats.runs, stats.idle_runs, stats.failures, stats.consecutive_failures) == (1, 0, 1, 1)
        assert stats.last_error == "Rolled back: the lease could not be confirmed before the commit"

    async def test_standing_by_ends_the_failure_streak_but_keeps_the_record(self) -> None:
        """A standby records no cycles, so nothing else would clear the streak of the last one it ran."""
        with job_stats.track("worker") as run:
            run.fail("Skipped")
        job_stats.standing_by("worker")
        job_stats.standing_by("never-ran")  # nothing to clear, nothing created

        stats = job_stats.get("worker")
        assert stats is not None
        assert (stats.failures, stats.consecutive_failures, stats.last_error) == (1, 0, "Skipped")
        assert job_stats.get("never-ran") is None

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
        """A stream consumer waits on its stream, so it has no cadence to miss."""
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


class TestWorkerRegistry:
    """Checked against the catalogue itself: loops are spawned by ``WorkerName`` only, so every name needs an entry."""

    def test_every_loop_is_described_and_runs_somewhere(self) -> None:
        assert set(WORKERS) == set(WorkerName)
        for name, info in WORKERS.items():
            assert info.description, name
            assert info.roles, name

    def test_descriptions_read_as_ui_copy(self) -> None:
        """The System page renders them verbatim: a capitalised sentence, no closing period, plain ASCII."""
        for name, info in WORKERS.items():
            assert info.description[0].isupper(), name
            assert not info.description.endswith("."), name
            assert info.description.isascii(), name

    def test_each_role_runs_the_loops_of_the_split(self) -> None:
        # Spelled out rather than derived: these names are the wire contract
        # of the System page, so renaming one or moving it to another role
        # should fail here.
        expected = {
            Role.API: {
                "heartbeat",
                "loop_lag_monitor",
                "geo_observation_publisher",
                "definitions_refresher",
                "geo_database_retry",
            },
            Role.GATEWAY: {
                "heartbeat",
                "loop_lag_monitor",
                "feed_follower",
                "request_metering",
                "failure_log_publisher",
                "geo_observation_publisher",
                "sighting_publisher",
                "wireguard_status_publisher",
                "openvpn_status_publisher",
                "openvpn_supervisor",
                "tunnel_reconciler",
                "geo_database_sync",
                "geo_database_retry",
            },
            Role.CONTROLLER: {
                "heartbeat",
                "loop_lag_monitor",
                "feed_builder",
                "job_producer_health",
                "job_producer_sync",
                "job_producer_scale",
                "job_producer_metrics_flush",
                "job_producer_compaction",
                "job_producer_snapshot",
                "job_producer_geo_update",
                "job_producer_tunnel_last_seen",
                "job_producer_index_hygiene",
                "job_producer_update_check",
            },
            Role.WORKER: {
                "heartbeat",
                "loop_lag_monitor",
                "geo_observation_publisher",
                "definitions_refresher",
                "geo_database_retry",
                "job_runner_health",
                "job_runner_sync",
                "job_runner_scale",
                "job_runner_provision",
                "job_runner_reattribute",
                "job_runner_enrich",
                "job_runner_traffic",
                "job_runner_metrics_flush",
                "job_runner_compaction",
                "job_runner_snapshot",
                "job_runner_geo_update",
                "job_runner_tunnel_last_seen",
                "job_runner_index_hygiene",
                "job_runner_update_check",
                "sighting_ingest",
                "observation_ingest",
            },
            Role.ECHO: {"loop_lag_monitor"},
        }

        for role, names in expected.items():
            assert {name for name, info in WORKERS.items() if role in info.roles} == names, role

    def test_no_loop_names_role_all(self) -> None:
        """``all`` composes every role's runtime, so it runs every loop through the roles the catalogue names."""
        assert not any(Role.ALL in info.roles for info in WORKERS.values())

    def test_a_loop_runs_in_the_roles_it_lists_and_every_loop_in_role_all(self) -> None:
        for name, info in WORKERS.items():
            assert {role for role in Role if info.runs_in(role)} == {*info.roles, Role.ALL}, name

    def test_every_job_kind_has_a_runner_on_the_workers(self) -> None:
        for kind in JobKind:
            name = job_runner_name(kind)
            assert name == f"job_runner_{kind}"
            assert WORKERS[name].roles == {Role.WORKER}

    def test_every_periodic_kind_has_a_producer_on_the_controller_under_its_lease(self) -> None:
        periodic = {
            JobKind.HEALTH, JobKind.SYNC, JobKind.SCALE, JobKind.METRICS_FLUSH, JobKind.COMPACTION, JobKind.SNAPSHOT,
            JobKind.GEO_UPDATE, JobKind.TUNNEL_LAST_SEEN, JobKind.INDEX_HYGIENE, JobKind.UPDATE_CHECK,
        }
        for kind in periodic:
            name = job_producer_name(kind)
            assert name == f"job_producer_{kind}"
            assert (WORKERS[name].roles, WORKERS[name].lease) == ({Role.CONTROLLER}, LeaseName.CONTROLLER)
        # Asked for only, never on a timer.
        for kind in set(JobKind) - periodic:
            with pytest.raises(ValueError):
                job_producer_name(kind)

    def test_only_the_controllers_loops_are_singletons_under_its_lease(self) -> None:
        """The controller's loops run in the one process holding its lease, the rest in every process of their roles."""
        for name, info in WORKERS.items():
            controller = info.roles == {Role.CONTROLLER}
            assert (info.scope == "singleton") == controller, name
            assert (info.lease is LeaseName.CONTROLLER) == controller, name
