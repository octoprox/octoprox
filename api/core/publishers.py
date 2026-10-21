# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Batch publishers: loops that gather what a process records in memory and write it to Redis a window at a time.

Every part of a window carries a batch identity its script checks first, so a part sent again after its reply was
lost is applied once; what Redis did not confirm goes again before the next window.
"""

from __future__ import annotations

from abc import abstractmethod
from collections import deque
from collections.abc import Sequence
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel

from api.core.logging import error_text
from api.core.loops import PeriodicLoop
from api.db.redis import BatchOutcome, BatchSender

if TYPE_CHECKING:
    from api.core.workers import WorkerName
    from api.db.redis import BatchChannel, BatchPart, BatchReply, RedisClient

logger = structlog.get_logger()


class BatchPublisher(PeriodicLoop):
    """Sends the window it gathers to Redis every ``interval``, one send at a time, and once more at :meth:`stop`.

    Args:
        redis_client: where the windows go; None keeps them, for a process or a test without Redis.
    """

    def __init__(self, redis_client: RedisClient | None, *, name: WorkerName, interval: float) -> None:
        super().__init__(name=name, interval=interval)
        self._redis = redis_client
        self._sender = None if redis_client is None else BatchSender(redis_client, on_answer=self._count)
        # Items Redis took, and items lost (a script that failed part-way, or the writer's own bounds), since start.
        self.published = 0
        self.dropped = 0

    @property
    def owed(self) -> int:
        """Items in parts Redis has not confirmed: they go again, unchanged or renumbered, before the next window."""
        return 0 if self._sender is None else sum(part.items for part in self._sender.owed)

    @property
    def pending(self) -> int:
        """Items not in Redis yet: the window's, and those of the parts Redis has not confirmed."""
        return self._windowed() + self.owed

    async def flush(self) -> int:
        """Send what is owed, then, once Redis confirmed all of it, the window; how many items landed.

        Raises when Redis did not answer, the parts it was sent kept for the next flush.
        """
        return sum(part.items for part, reply in await self._send() if reply.landed)

    async def stop(self) -> None:
        """End the loop, then flush once more; what Redis does not take now is logged and lost. Never raises."""
        await super().stop()
        try:
            await self.flush()
        except Exception as exc:
            logger.warning("Batch not sent at stop", loop=self.name, error=error_text(exc))
        if self.pending:
            logger.warning("Batch items lost at stop", loop=self.name, pending=self.pending)

    @abstractmethod
    def _windowed(self) -> int:
        """How many items the window gathered so far holds."""

    @abstractmethod
    def _cut(self, redis_client: RedisClient) -> Sequence[BatchPart]:
        """The window as batch parts prepared on ``redis_client``, without I/O, and a new window begun; none when empty."""

    async def _cycle(self) -> bool:
        """Flush; a cycle that had nothing to send is idle."""
        return bool(await self._send())

    async def _send(self) -> list[tuple[BatchPart, BatchReply]]:
        """One send through the batch sender; each part sent, with Redis's answer."""
        redis_client, sender = self._redis, self._sender
        if redis_client is None or sender is None:
            return []
        return await sender.send(lambda: self._cut(redis_client))

    def _count(self, answered: list[tuple[BatchPart, BatchReply]]) -> None:
        """Count one send's answers: a part that landed is published, one whose script failed part-way is dropped."""
        for part, reply in answered:
            if reply.landed:
                self.published += part.items
            elif reply.outcome is BatchOutcome.FAILED:
                self.dropped += part.items


class ItemPublisher[T: BaseModel](BatchPublisher):
    """A batch publisher whose window is a buffer of items, appended to a work stream an entry each, one part a window.

    Args:
        channel: the batch channel of the appends: one publisher per channel and process.
        key: the work stream the items go to, each in an entry's ``item`` field.
        max_items: the stream's approximate length cap, 0 for none; what it trims is counted in ``dropped``.
        max_pending: the most items the buffer holds, through a Redis outage too; when full the oldest goes.
    """

    def __init__(
        self,
        redis_client: RedisClient | None,
        *,
        name: WorkerName,
        interval: float,
        channel: BatchChannel,
        key: str,
        max_items: int,
        max_pending: int,
    ) -> None:
        super().__init__(redis_client, name=name, interval=interval)
        self._channel = channel
        self._key = key
        self._max_items = max_items
        self._max_pending = max_pending
        self._buffer: deque[T] = deque()

    def push(self, item: T) -> None:
        """Buffer one item for the next window, never waiting, so the request path may call it."""
        if len(self._buffer) >= self._max_pending:
            self._buffer.popleft()
            self.dropped += 1
        self._buffer.append(item)

    def _windowed(self) -> int:
        """How many items the buffer holds."""
        return len(self._buffer)

    def _cut(self, redis_client: RedisClient) -> Sequence[BatchPart]:
        """The buffer, serialised off the request path, as one append part; the buffer starts again empty."""
        items = [item.model_dump_json() for item in self._buffer]
        self._buffer.clear()
        return redis_client.append_batch(self._channel, self._key, items, max_items=self._max_items)

    def _count(self, answered: list[tuple[BatchPart, BatchReply]]) -> None:
        """Count one send's answers, and the entries the stream's cap trimmed as dropped."""
        super()._count(answered)
        self.dropped += sum(reply.value for _part, reply in answered if reply.outcome is BatchOutcome.APPLIED)
