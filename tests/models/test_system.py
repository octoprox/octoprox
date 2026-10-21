# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for a heartbeat's loop rows, the streams a process follows and the work it did, the work streams, and the takeover.

Every process publishes its snapshot to Redis as JSON and the api reads it
back (``api.core.system_stats``), so the snapshot's cases go through that round trip.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Coroutine
from datetime import datetime
from typing import Any

from api.core.job_stats import JobStats
from api.core.leadership import LEASE_TTL_MS, STANDBY_POLL_MS
from api.core.roles import Role
from api.core.workers import WORKERS, WorkerName
from api.models.system import (
    ControllerSnapshot,
    ControllerStatus,
    DatabaseStats,
    FeedStatus,
    GatewayFeedStatus,
    InstanceRuntime,
    InstanceSnapshot,
    InventoryStats,
    RedisStats,
    StreamState,
    SystemStats,
    WorkerSnapshot,
    WorkerTask,
    WorkStream,
    WorkStreams,
)
from tests.routes.gateway_heartbeats import gateway_heartbeat

# A change stream entry ID: the milliseconds Redis gave it, and a sequence within them.
POSITION = "1760788800000-3"
# Three moments apart, so a time filed under another field shows.
OVERRUN_AT = datetime(2026, 10, 20, 12, 0, 1)
RAN_AT = datetime(2026, 10, 20, 12, 0, 2)
FAILED_AT = datetime(2026, 10, 20, 12, 0, 3)


def _published(**sections: Any) -> InstanceSnapshot:
    """A controller's heartbeat as the api reads it back from Redis."""
    runtime = InstanceRuntime(
        version="6.0.0", role=Role.CONTROLLER, instance_id="controller-1", pid=1
    )
    snapshot = InstanceSnapshot(runtime=runtime, **sections)
    return InstanceSnapshot.model_validate_json(snapshot.model_dump_json())


@contextlib.asynccontextmanager
async def _running(name: str) -> AsyncIterator[asyncio.Task[Any]]:
    """The task of a loop named ``name``, running until the block ends."""
    task = asyncio.create_task(asyncio.Event().wait(), name=name)
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _ended(name: str, loop: Coroutine[Any, Any, Any], *, cancel: bool = False) -> asyncio.Task[Any]:
    """The task of a loop named ``name`` once it ended: returned, raised, or cancelled first when ``cancel``."""
    task = asyncio.create_task(loop, name=name)
    if cancel:
        task.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await task
    return task


class TestWorkerTask:
    """A process's loop rows (its snapshot's ``tasks``) join the asyncio task, its counters and the catalogue."""

    async def test_a_running_loop_with_its_counters_and_its_catalogue_entry(self) -> None:
        name = WorkerName.FEED_BUILDER
        stats = JobStats(
            name=name,
            interval_seconds=0.5,
            runs=9,
            idle_runs=2,
            failures=3,
            overruns=4,
            consecutive_overruns=1,
            last_overrun_at=OVERRUN_AT,
            consecutive_failures=2,
            last_run_at=RAN_AT,
            last_duration_ms=10.0,
            max_duration_ms=80.0,
            last_error="boom",
            last_error_at=FAILED_AT,
            total_duration_ms=315.0,
        )

        async with _running(name) as task:
            row = WorkerTask.of(task, stats, WORKERS[name])

        assert row.model_dump() == {
            "name": "feed_builder",
            "description": WORKERS[name].description,
            "scope": "singleton",
            "lease": "controller",
            "state": "running",
            "error": None,
            "interval_seconds": 0.5,
            "runs": 9,
            "idle_runs": 2,
            "failures": 3,
            "overruns": 4,
            "consecutive_overruns": 1,
            "last_overrun_at": OVERRUN_AT,
            "consecutive_failures": 2,
            "last_run_at": RAN_AT,
            "last_duration_ms": 10.0,
            # Over the seven cycles that worked.
            "avg_duration_ms": 45.0,
            "max_duration_ms": 80.0,
            "last_error": "boom",
            "last_error_at": FAILED_AT,
        }

    async def test_a_loop_that_filed_no_cycle_reads_as_zeroes_not_nulls(self) -> None:
        async with _running(WorkerName.FEED_FOLLOWER) as task:
            row = WorkerTask.of(task, None, WORKERS[WorkerName.FEED_FOLLOWER])

        assert (row.runs, row.idle_runs, row.failures, row.overruns) == (0, 0, 0, 0)
        assert (row.consecutive_overruns, row.consecutive_failures) == (0, 0)
        assert (row.interval_seconds, row.last_run_at, row.avg_duration_ms, row.last_error) == (None, None, None, None)
        assert (row.scope, row.lease) == ("instance", None)

    async def test_a_loop_whose_task_returned_is_done(self) -> None:
        task = await _ended(WorkerName.HEARTBEAT, asyncio.sleep(0))

        row = WorkerTask.of(task, None, WORKERS[WorkerName.HEARTBEAT])

        assert (row.state, row.error) == ("done", None)

    async def test_a_cancelled_loop(self) -> None:
        task = await _ended(WorkerName.HEARTBEAT, asyncio.Event().wait(), cancel=True)

        row = WorkerTask.of(task, None, WORKERS[WorkerName.HEARTBEAT])

        assert (row.state, row.error) == ("cancelled", None)

    async def test_a_loop_that_died_says_how(self) -> None:
        async def fails() -> None:
            raise RuntimeError("redis down")

        task = await _ended(WorkerName.FEED_FOLLOWER, fails())

        row = WorkerTask.of(task, None, WORKERS[WorkerName.FEED_FOLLOWER])

        assert (row.state, row.error) == ("failed", "RuntimeError: redis down")

    async def test_a_loop_the_catalogue_lacks_still_gets_its_row_undescribed(self) -> None:
        async with _running("not_catalogued") as task:
            row = WorkerTask.of(task, JobStats(name="not_catalogued", runs=1), None)

        assert (row.name, row.description, row.scope, row.lease) == ("not_catalogued", "", "instance", None)
        assert row.runs == 1

    async def test_timings_are_rounded_to_the_microsecond(self) -> None:
        stats = JobStats(
            name=WorkerName.HEARTBEAT,
            runs=2,
            last_duration_ms=1.23456,
            max_duration_ms=12.34567,
            total_duration_ms=13.58023,
        )

        async with _running(WorkerName.HEARTBEAT) as task:
            row = WorkerTask.of(task, stats, WORKERS[WorkerName.HEARTBEAT])

        assert (row.last_duration_ms, row.avg_duration_ms, row.max_duration_ms) == (1.235, 6.79, 12.346)


class TestStreams:
    def test_a_follower_that_has_read_nothing_yet(self) -> None:
        assert StreamState().model_dump() == {
            "position": None,
            "lag_seconds": None,
            "entries": 0,
            "full_reads": {},
            "last_full_read_at": None,
        }

    def test_each_stream_a_process_follows_reaches_the_api(self) -> None:
        changes = StreamState(
            position=POSITION,
            lag_seconds=0.25,
            entries=12,
            full_reads={"start": 1, "gap": 2, "reconcile": 3},
            last_full_read_at=datetime(2026, 10, 18, 12, 0),
        )
        # A stream that did not exist at the last full read.
        runtime = StreamState(position="0-0", full_reads={"start": 1})

        read = _published(streams={"changes": changes, "runtime": runtime})

        assert read.streams == {"changes": changes, "runtime": runtime}

    def test_a_heartbeat_without_them_reads_as_following_none(self) -> None:
        runtime = {"version": "6.0.0", "role": "api", "instance_id": "api-1", "pid": 1}
        heartbeat = json.dumps({"runtime": runtime})
        assert InstanceSnapshot.model_validate_json(heartbeat).streams == {}


class TestChangePositions:
    def test_the_controller_reports_how_far_its_compile_read_and_when_it_was_made(self) -> None:
        compiled = datetime(2026, 10, 18, 12, 0)
        controller = ControllerSnapshot(
            is_leader=True, lease_terms=1, changes=POSITION, compiled_at=compiled
        )

        read = _published(controller=controller).controller

        assert read is not None
        assert (read.changes, read.compiled_at) == (POSITION, compiled)

    def test_a_gateway_reports_how_far_the_compile_of_its_feed_read(self) -> None:
        applied = InstanceSnapshot.model_validate_json(
            gateway_heartbeat("gateway-1", feed_changes=POSITION)
        ).gateway
        fresh = InstanceSnapshot.model_validate_json(gateway_heartbeat("gateway-2")).gateway

        assert applied is not None and fresh is not None
        assert (applied.feed_changes, fresh.feed_changes) == (POSITION, None)

    def test_the_system_view_names_them(self) -> None:
        """The names the System page and the API docs read; None until there is something to name."""
        head = "1760788801000-0"
        controller = ControllerStatus(changes=POSITION, changes_head=head).model_dump(mode="json")
        gateway = GatewayFeedStatus(instance_id="gateway-1", changes=POSITION)
        feed = FeedStatus(changes=POSITION, gateways=[gateway]).model_dump(mode="json")

        assert (controller["changes"], controller["changes_head"]) == (POSITION, head)
        assert (feed["changes"], feed["gateways"][0]["changes"]) == (POSITION, POSITION)
        empty = ControllerStatus().model_dump(mode="json")
        assert (empty["changes"], empty["changes_head"]) == (None, None)
        assert FeedStatus().changes is None
        assert GatewayFeedStatus(instance_id="gateway-1").changes is None


class TestTakeover:
    def test_the_system_view_carries_the_standby_poll_and_the_lease_ttl_the_controllers_run_with(self) -> None:
        """The System page words how soon a standby takes the lease over from these, with or without a leader."""
        led = ControllerStatus(leader="controller-1").model_dump(mode="json")
        unreadable = ControllerStatus(error="redis down").model_dump(mode="json")

        for status in (led, unreadable):
            assert (status["standby_poll_ms"], status["lease_ttl_ms"]) == (STANDBY_POLL_MS, LEASE_TTL_MS)


class TestWorkStreams:
    def test_queued_is_what_no_worker_has_taken_yet(self) -> None:
        stream = WorkStream(length=12, pending=4, consumers=2, oldest_seconds=3.5)

        assert stream.queued == 8
        assert stream.model_dump(mode="json") == {
            "length": 12,
            "pending": 4,
            "consumers": 2,
            "oldest_seconds": 3.5,
            "queued": 8,
        }

    def test_queued_is_never_below_zero(self) -> None:
        """The cap can trim entries still pending, so a stream may hold fewer entries than are pending."""
        assert WorkStream(length=3, pending=5).queued == 0

    def test_an_empty_stream_has_no_oldest_entry(self) -> None:
        assert WorkStream().model_dump(mode="json") == {
            "length": 0, "pending": 0, "consumers": 0, "oldest_seconds": None, "queued": 0,
        }

    def test_the_system_view_carries_them_by_name_and_says_why_when_they_could_not_be_read(self) -> None:
        stats = SystemStats(
            generated_at=datetime(2026, 10, 19, 12, 0),
            controller=ControllerStatus(),
            feed=FeedStatus(),
            inventory=InventoryStats(),
            database=DatabaseStats(),
            redis=RedisStats(),
        )
        assert stats.streams == WorkStreams()

        streams = WorkStreams(by_name={"jobs.health": WorkStream(length=2, pending=1)}, error=None)
        read = SystemStats.model_validate_json(stats.model_copy(update={"streams": streams}).model_dump_json())
        assert read.streams == streams
        assert read.model_dump(mode="json")["streams"]["by_name"]["jobs.health"]["queued"] == 1
        assert WorkStreams(error="redis down").model_dump(mode="json") == {"by_name": {}, "error": "redis down"}


class TestWorkerCounts:
    def test_a_worker_reports_the_observations_it_ingested_and_lost(self) -> None:
        worker = WorkerSnapshot(jobs={}, observations_consumed=5000, observations_dropped=12)

        read = _published(worker=worker).worker

        assert read is not None
        assert (read.observations_consumed, read.observations_dropped) == (5000, 12)

    def test_a_heartbeat_without_them_reads_as_zero(self) -> None:
        """As a worker of the release before writes it."""
        read = WorkerSnapshot.model_validate_json(json.dumps({"jobs": {}, "sightings_consumed": 3}))

        assert (read.observations_consumed, read.observations_dropped) == (0, 0)
