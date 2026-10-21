# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Background work that must not be forgotten: a runtime's named loops (``TaskSet``) and an owner's one-off tasks (``HeldTasks``).

Each task is kept until it ends and its failure logged; ``quietly`` runs one step of a runtime's shutdown, so a step that
fails is logged and the steps after it still run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Coroutine
from typing import TYPE_CHECKING, Any

import structlog

from api.core.logging import error_text

if TYPE_CHECKING:
    from api.core.workers import WorkerName

logger = structlog.get_logger()


class TaskSet:
    """The named background loops one runtime owns, each task named with its :class:`~api.core.workers.WorkerName`.

    The System page lists them by that name; a loop that ends with an exception is logged as it ends and stays listed, as
    failed, until :meth:`stop`.
    """

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def tasks(self) -> list[asyncio.Task[None]]:
        """The loops started since the last :meth:`stop`, for the System page."""
        return list(self._tasks)

    def spawn(self, name: WorkerName, coro: Coroutine[Any, Any, None]) -> None:
        """Start ``coro`` as the loop called ``name`` and keep its handle."""
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(_log_failure)
        self._tasks.append(task)

    async def stop(self) -> None:
        """Cancel every loop, all before any is awaited, and wait until each has ended; calling it again does nothing."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _log_failure(task: asyncio.Task[None]) -> None:
    """Log a loop that ended with an exception, stack included: its cycles stopped there."""
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.error("Background loop failed", task=task.get_name(), exc_info=exc)


class HeldTasks:
    """The one-off tasks an owner keeps until each ends, so none is collected before it runs; a failure is logged."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def busy(self) -> bool:
        """Whether a task still runs; one that has ended is not counted, though its callbacks may not have dropped it yet."""
        return any(not task.done() for task in self._tasks)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str, **context: Any) -> asyncio.Task[Any]:
        """Run ``coro`` as a task held until it ends, then dropped; a failure is logged with ``context``."""
        return spawn_held(self._tasks, coro, name=name, **context)

    async def settle(self, timeout: float | None = None) -> None:
        """Wait until no task is left, those spawned meanwhile included; with a timeout, wait that long once, then cancel the rest.

        Args:
            timeout: seconds to wait before cancelling what is left and waiting for it; None waits for every task.
        """
        if timeout is None:
            while self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            return
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)
        await self.cancel()

    async def cancel(self) -> None:
        """Cancel every task and wait until each has ended."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def spawn_held(
    held: set[asyncio.Task[Any]], coro: Coroutine[Any, Any, Any], *, name: str, **context: Any
) -> asyncio.Task[Any]:
    """Run ``coro`` as a task kept in ``held`` until it finishes; a failure is logged with ``context``."""
    task: asyncio.Task[Any] = asyncio.create_task(coro, name=name)
    held.add(task)

    def ended(finished: asyncio.Task[Any]) -> None:
        held.discard(finished)
        if not finished.cancelled() and (exc := finished.exception()) is not None:
            logger.warning("Background task failed", task=name, error=error_text(exc), **context)

    task.add_done_callback(ended)
    return task


async def quietly(step: str, stopping: Awaitable[object]) -> None:
    """Await one shutdown step; a failure is logged, never raised, so the steps after it still run."""
    try:
        await stopping
    except Exception as exc:
        logger.warning("Shutdown step failed", step=step, error=error_text(exc))
