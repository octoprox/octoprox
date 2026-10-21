# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The waits the tests share: until a condition holds, and a loop run as a task for the length of a block."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from api.core.loops import Loop


async def until(condition: Callable[[], object | Awaitable[object]], timeout: float = 10.0) -> float:
    """Seconds until ``condition()`` held, awaited when it answers an awaitable, looking every 10 ms; fails the test after ``timeout``."""
    started = time.monotonic()
    async with asyncio.timeout(timeout):
        while True:
            held = condition()
            if inspect.isawaitable(held):
                held = await held
            if held:
                return time.monotonic() - started
            await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def running(loop: Loop) -> AsyncIterator[asyncio.Task[None]]:
    """``loop.run()`` as a task, as its runtime runs it; stopped on the way out, its task ended within 5 s."""
    task = asyncio.create_task(loop.run())
    try:
        yield task
    finally:
        await asyncio.wait_for(loop.stop(), timeout=5)
        await asyncio.wait_for(task, timeout=5)
