# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the role runtime base (api.roles.runtime): its loops started under their names, stopped together, their streams merged.

Toy loops with no timed duty stand in for a role's loops; nothing here needs Redis or Postgres.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

import pytest
import structlog

from api.core.loops import Loop
from api.models.system import StreamState
from api.roles.runtime import RoleRuntime
from tests.waits import until


class Toy(Loop):
    """A loop that cycles on a wake; its stop takes ``stop_seconds``, then raises ``stop_error`` when set."""

    def __init__(self, name: Any, streams: dict[str, StreamState] | None = None) -> None:
        super().__init__(name=name)
        self.cycles = 0
        self.stop_seconds = 0.0
        self.stop_error: Exception | None = None
        self.stops_started: list[float] = []
        self._streams = streams or {}

    async def _cycle(self) -> bool:
        self.cycles += 1
        return True

    async def stop(self) -> None:
        self.stops_started.append(time.monotonic())
        await asyncio.sleep(self.stop_seconds)
        if self.stop_error is not None:
            raise self.stop_error
        await super().stop()

    def streams(self) -> dict[str, StreamState]:
        return dict(self._streams)


class Toys(RoleRuntime):
    """A role whose loops are the toys it is given."""

    def __init__(self, *loops: Loop) -> None:
        super().__init__()
        self._loops = loops

    def loops(self) -> Sequence[Loop]:
        return self._loops

    def readiness(self) -> str | None:
        return None


class TestRoleRuntime:
    async def test_each_loop_runs_as_a_task_named_after_it(self) -> None:
        """The task's name is what the System page lists the loop by."""
        one, other = Toy("one"), Toy("other")
        runtime = Toys(one, other)

        await runtime.start()
        try:
            assert [task.get_name() for task in runtime.tasks.tasks] == ["one", "other"]
            one.wake()
            other.wake()
            await until(lambda: one.cycles == other.cycles == 1)
        finally:
            await runtime.stop()

    async def test_a_stop_stops_every_loop_together_then_awaits_their_tasks(self) -> None:
        """Together, so a worker's runners spend their stop graces at once rather than one after another."""
        one, other = Toy("one"), Toy("other")
        one.stop_seconds = other.stop_seconds = 0.2
        runtime = Toys(one, other)
        await runtime.start()
        tasks = runtime.tasks.tasks

        started = time.monotonic()
        await runtime.stop()

        assert time.monotonic() - started < 0.35
        assert one.stopped and other.stopped
        assert all(task.done() for task in tasks) and runtime.tasks.tasks == []

    async def test_a_loop_whose_stop_raises_is_logged_and_neither_the_others_nor_the_runtime_s_stop_fail(self) -> None:
        """Its task is ended all the same: a loop left running would hold the process up at its exit."""
        failing, other = Toy("failing"), Toy("other")
        failing.stop_error = RuntimeError("Redis went away")
        runtime = Toys(failing, other)
        await runtime.start()
        tasks = runtime.tasks.tasks

        with structlog.testing.capture_logs() as logs:
            await runtime.stop()

        assert other.stopped and not failing.stopped
        assert all(task.done() for task in tasks) and runtime.tasks.tasks == []
        assert [(log["event"], log["step"], log["error"]) for log in logs if log["log_level"] == "warning"] == [
            ("Shutdown step failed", "failing loop", "Redis went away")
        ]

    async def test_it_says_it_started_once_its_loops_run_and_no_more_once_its_stop_begins(self) -> None:
        """What the api's and a worker's readiness go by."""
        one = Toy("one")
        runtime = Toys(one)
        assert runtime.started is False

        await runtime.start()
        assert runtime.started is True

        seen: list[bool] = []
        stop = one.stop

        async def noted() -> None:
            seen.append(runtime.started)
            await stop()

        one.stop = noted  # type: ignore[method-assign]
        await runtime.stop()
        assert seen == [False] and runtime.started is False

    async def test_its_background_tasks_are_its_loops_tasks_while_they_run(self) -> None:
        """What the node lists on the System page for a role that runs nothing besides its loops."""
        runtime = Toys(Toy("one"), Toy("other"))
        assert runtime.background_tasks == []

        await runtime.start()
        try:
            assert [task.get_name() for task in runtime.background_tasks] == ["one", "other"]
        finally:
            await runtime.stop()

        assert runtime.background_tasks == []

    def test_its_streams_are_every_loop_s_merged(self) -> None:
        runtime = Toys(
            Toy("follower", {"changes": StreamState(position="1-0")}),
            Toy("timer"),
            Toy("feed follower", {"feed": StreamState(position="2-0")}),
        )

        assert runtime.streams() == {"changes": StreamState(position="1-0"), "feed": StreamState(position="2-0")}

    def test_a_runtime_must_say_what_its_loops_are_and_when_it_is_ready(self) -> None:
        class NoLoops(RoleRuntime):
            def readiness(self) -> str | None:
                return None

        class NoReadiness(RoleRuntime):
            def loops(self) -> Sequence[Loop]:
                return ()

        with pytest.raises(TypeError):
            NoLoops()  # type: ignore[abstract]
        with pytest.raises(TypeError):
            NoReadiness()  # type: ignore[abstract]
