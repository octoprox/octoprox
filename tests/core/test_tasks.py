# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for TaskSet and HeldTasks, the owners of a runtime's loops and of one-off tasks, and for ``quietly``, a step of a shutdown.

Real tasks on the test's event loop: a loop is a coroutine that waits until
it is cancelled, noting that its cleanup ran, or one that fails at once.
"""

from __future__ import annotations

import asyncio
import gc
import time
import weakref
from collections.abc import Awaitable
from typing import Any
from unittest.mock import patch

from api.core.tasks import HeldTasks, TaskSet, quietly, spawn_held
from api.core.workers import WorkerName


async def _loop(ended: list[str], name: str) -> None:
    try:
        await asyncio.Event().wait()
    finally:
        ended.append(name)


async def _unreachable_wait() -> None:
    """Await a future nothing but this task can reach, so nothing but its owner keeps the task."""
    await asyncio.get_running_loop().create_future()


async def _within(waiting: Awaitable[None], seconds: float = 2.0) -> None:
    """Await ``waiting``, failing the test rather than hanging the suite when it never ends."""
    await asyncio.wait_for(waiting, seconds)


class TestTaskSet:
    async def test_lists_each_loop_under_its_worker_name(self) -> None:
        tasks = TaskSet()
        ended: list[str] = []
        tasks.spawn(WorkerName.HEARTBEAT, _loop(ended, "heartbeat"))
        tasks.spawn(WorkerName.FEED_FOLLOWER, _loop(ended, "feed_follower"))
        try:
            assert [task.get_name() for task in tasks.tasks] == ["heartbeat", "feed_follower"]
            assert not any(task.done() for task in tasks.tasks)
        finally:
            await tasks.stop()

    async def test_stop_cancels_every_loop_and_waits_until_each_has_ended(self) -> None:
        tasks = TaskSet()
        ended: list[str] = []
        tasks.spawn(WorkerName.HEARTBEAT, _loop(ended, "heartbeat"))
        tasks.spawn(WorkerName.LOOP_LAG_MONITOR, _loop(ended, "loop_lag_monitor"))
        await asyncio.sleep(0)  # both start waiting
        handles = tasks.tasks

        with patch("api.core.tasks.logger") as log:
            await tasks.stop()

        # Their cleanup ran before stop returned, and a cancel is not a failure.
        assert sorted(ended) == ["heartbeat", "loop_lag_monitor"]
        assert all(task.cancelled() for task in handles)
        log.error.assert_not_called()
        assert tasks.tasks == []
        await tasks.stop()  # nothing left to stop

    async def test_a_loop_that_fails_is_logged_as_it_ends_and_stop_still_completes(self) -> None:
        """Without the log a loop that died would only show as failed on the System page."""

        async def broken() -> None:
            raise RuntimeError("redis down")

        tasks = TaskSet()
        with patch("api.core.tasks.logger") as log:
            tasks.spawn(WorkerName.FEED_FOLLOWER, broken())
            await asyncio.wait(tasks.tasks)

        log.error.assert_called_once()
        assert log.error.call_args.args == ("Background loop failed",)
        assert log.error.call_args.kwargs["task"] == "feed_follower"
        assert str(log.error.call_args.kwargs["exc_info"]) == "redis down"
        # Still listed, as failed, until the runtime stops.
        (failed,) = tasks.tasks
        assert failed.done() and not failed.cancelled()

        await tasks.stop()
        assert tasks.tasks == []


class TestHeldTasks:
    async def test_a_task_is_held_until_it_ends_then_dropped(self) -> None:
        """The event loop keeps a task only weakly: one awaiting what nothing else holds would be collected unfinished."""
        held = HeldTasks()
        task = weakref.ref(held.spawn(_unreachable_wait(), name="capture"))
        await asyncio.sleep(0)  # it starts and awaits its future
        gc.collect()
        assert task() is not None and held.busy

        await _within(held.cancel())
        # A loop turn, so the wake-up that resumed this test no longer holds the cancel's wait.
        await asyncio.sleep(0)
        gc.collect()
        assert task() is None and not held.busy

    async def test_a_failure_is_logged_with_its_context(self) -> None:
        async def failing() -> None:
            raise RuntimeError("redis down")

        held = HeldTasks()
        with patch("api.core.tasks.logger") as log:
            held.spawn(failing(), name="mitm_capture", project_id="p1")
            await _within(held.settle())

        log.warning.assert_called_once_with(
            "Background task failed", task="mitm_capture", error="redis down", project_id="p1"
        )

    async def test_a_failure_without_a_message_is_named_by_its_type(self) -> None:
        """A Redis call cut by the request deadline raises a bare TimeoutError."""

        async def timed_out() -> None:
            raise TimeoutError

        held = HeldTasks()
        with patch("api.core.tasks.logger") as log:
            held.spawn(timed_out(), name="sticky_binding_write", project_id="p1")
            await _within(held.settle())

        log.warning.assert_called_once_with(
            "Background task failed", task="sticky_binding_write", error="TimeoutError", project_id="p1"
        )

    async def test_settle_waits_for_a_task_spawned_while_it_waits(self) -> None:
        held = HeldTasks()
        ended: list[str] = []

        async def later() -> None:
            await asyncio.sleep(0.01)
            ended.append("later")

        async def first() -> None:
            await asyncio.sleep(0)
            held.spawn(later(), name="later")
            ended.append("first")

        held.spawn(first(), name="first")
        await _within(held.settle())

        assert ended == ["first", "later"] and not held.busy

    async def test_settle_with_a_timeout_waits_that_long_then_cancels_what_is_left_and_waits_for_it(self) -> None:
        held = HeldTasks()
        ended: list[str] = []

        async def quick() -> None:
            await asyncio.sleep(0.01)
            ended.append("quick")

        held.spawn(quick(), name="quick")
        stuck = held.spawn(_loop(ended, "stuck"), name="stuck")
        started = time.monotonic()

        await _within(held.settle(timeout=0.2))

        # The stuck one's cleanup ran before settle returned.
        assert ended == ["quick", "stuck"]
        assert stuck.cancelled() and not held.busy
        assert time.monotonic() - started >= 0.19

    async def test_cancel_cancels_every_task_and_waits_until_each_has_ended(self) -> None:
        held = HeldTasks()
        ended: list[str] = []
        first = held.spawn(_loop(ended, "first"), name="first")
        second = held.spawn(_loop(ended, "second"), name="second")
        await asyncio.sleep(0)  # both start waiting

        with patch("api.core.tasks.logger") as log:
            await _within(held.cancel())

        assert sorted(ended) == ["first", "second"]
        assert first.cancelled() and second.cancelled() and not held.busy
        log.warning.assert_not_called()  # a cancel is not a failure

    async def test_busy_is_whether_a_task_still_runs(self) -> None:
        """A task that has ended is not busy before its callbacks drop it: the change log starts its next retry then."""
        held = HeldTasks()
        release = asyncio.Event()
        seen: list[bool] = []

        async def write() -> None:
            await release.wait()
            # Runs once this task has ended, before its done callbacks.
            asyncio.get_running_loop().call_soon(lambda: seen.append(held.busy))

        assert not held.busy
        held.spawn(write(), name="write")
        assert held.busy
        release.set()
        await _within(held.settle())

        assert seen == [False] and not held.busy


class TestSpawnHeld:
    async def test_the_task_is_in_the_owner_s_set_until_it_ends_and_a_failure_is_logged(self) -> None:
        """The tunnel endpoints still keep their tasks in sets of their own."""
        held: set[asyncio.Task[Any]] = set()
        release = asyncio.Event()

        async def failing() -> None:
            await release.wait()
            raise RuntimeError("wg0 is down")

        with patch("api.core.tasks.logger") as log:
            task = spawn_held(held, failing(), name="wireguard_apply", endpoint="wg0")
            assert held == {task}
            release.set()
            await _within(asyncio.wait([task]))

        assert held == set()
        log.warning.assert_called_once_with(
            "Background task failed", task="wireguard_apply", error="wg0 is down", endpoint="wg0"
        )


class TestQuietly:
    async def test_a_failed_step_is_logged_and_the_next_one_still_runs(self) -> None:
        async def failing() -> None:
            raise RuntimeError("redis gone")

        ran: list[str] = []

        async def next_step() -> None:
            ran.append("next")

        with patch("api.core.tasks.logger") as log:
            await quietly("withdraw", failing())
            await quietly("Redis", next_step())

        assert ran == ["next"]
        log.warning.assert_called_once_with("Shutdown step failed", step="withdraw", error="redis gone")

    async def test_a_step_that_fails_without_a_message_is_named_by_its_type(self) -> None:
        async def timed_out() -> None:
            raise TimeoutError

        with patch("api.core.tasks.logger") as log:
            await quietly("controller lease", timed_out())

        log.warning.assert_called_once_with("Shutdown step failed", step="controller lease", error="TimeoutError")

    async def test_a_cancelled_shutdown_is_not_swallowed(self) -> None:
        """Only failures are quiet: the shutdown itself being cancelled still ends it."""

        async def waiting() -> None:
            await asyncio.Event().wait()

        stopping = asyncio.create_task(quietly("loops", waiting()))
        await asyncio.sleep(0)
        stopping.cancel()

        with patch("api.core.tasks.logger") as log:
            await asyncio.gather(stopping, return_exceptions=True)

        assert stopping.cancelled()
        log.warning.assert_not_called()
