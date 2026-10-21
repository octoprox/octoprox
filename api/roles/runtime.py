# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The base of the role runtimes: a role's loops, started and stopped together, its readiness and the streams it follows."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING

from api.core.tasks import TaskSet, quietly

if TYPE_CHECKING:
    from api.core.loops import Loop
    from api.models.system import StreamState


class RoleRuntime(ABC):
    """What one role runs in a process: its loops, each a task named after it, and why it cannot work when it cannot.

    A runtime adds its own steps around the base's start and stop, such as keeping a lease before its loops start.
    """

    def __init__(self) -> None:
        self.tasks = TaskSet()
        # Set once start has started every loop, and cleared as stop begins.
        self.started = False

    @abstractmethod
    def loops(self) -> Sequence[Loop]:
        """The role's loops in the order they start, the same ones at every call."""

    @abstractmethod
    def readiness(self) -> str | None:
        """None while the role can do its work, else why it cannot."""

    async def start(self) -> None:
        """Start every loop, each as a task named after it."""
        for loop in self.loops():
            self.tasks.spawn(loop.name, loop.run())
        self.started = True

    async def stop(self) -> None:
        """Stop every loop together, each quietly, then await their tasks, cancelling any still running; never raises."""
        self.started = False
        await asyncio.gather(*(quietly(f"{loop.name} loop", loop.stop()) for loop in self.loops()))
        await self.tasks.stop()

    @property
    def background_tasks(self) -> list[asyncio.Task[None]]:
        """Every task the role runs, for the System page: its loops' by default."""
        return self.tasks.tasks

    def streams(self) -> dict[str, StreamState]:
        """How far the role's loops have read each stream they follow from a position of their own, by stream name."""
        streams: dict[str, StreamState] = {}
        for loop in self.loops():
            streams.update(loop.streams())
        return streams
