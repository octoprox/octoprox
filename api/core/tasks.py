# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Fire-and-forget work that must not be forgotten.

The event loop holds only a weak reference to a task, so a task nobody
keeps can be collected before it runs. ``spawn_held`` keeps the task in a
set the owner controls (to await on shutdown, to count in flight), drops it
when it finishes, and logs a failure instead of letting it vanish.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import structlog

logger = structlog.get_logger()


def spawn_held(
    held: set[asyncio.Task[Any]], coro: Coroutine[Any, Any, Any], *, name: str, **context: Any
) -> asyncio.Task[Any]:
    """Run ``coro`` as a task kept in ``held`` until it finishes; a failure is logged with ``context``."""
    task: asyncio.Task[Any] = asyncio.create_task(coro, name=name)
    held.add(task)

    def done(finished: asyncio.Task[Any]) -> None:
        held.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            logger.warning("Background task failed", task=name, error=str(finished.exception()), **context)

    task.add_done_callback(done)
    return task
