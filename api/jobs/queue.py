# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The job streams: :class:`JobQueue` appends one entry per key for the workers of each kind to take."""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial
from typing import TYPE_CHECKING

from api.db.redis import JOB_STREAM_KEY, JOB_STREAM_MAXLEN

if TYPE_CHECKING:
    from redis.asyncio.client import Pipeline

    from api.core.roles import JobKind
    from api.db.redis import RedisClient

# Keys one pipeline appends at most: a write that adds thousands of rows, or a
# producer's slice of a large pool, costs a few short exchanges with Redis,
# each well within a request deadline, rather than one per key or one so long
# that the deadline cuts it.
PUBLISH_BATCH = 500


class JobQueue:
    """Appends entries to the job streams, one per key in the ``key`` field, for the workers of each kind to take."""

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis = redis_client

    @staticmethod
    def append(pipe: Pipeline, kind: JobKind, key: str) -> None:
        """Queue on ``pipe`` the entry that asks for a run of ``kind`` for ``key``, so it lands with the pipeline's other writes."""
        pipe.xadd(JOB_STREAM_KEY.format(kind=kind), {"key": key}, maxlen=JOB_STREAM_MAXLEN, approximate=True)

    async def publish(self, kind: JobKind, keys: Iterable[str]) -> int:
        """Append an entry for each key to the kind's stream, in order, ``PUBLISH_BATCH`` to a pipeline; how many.

        Raises when Redis does not take an entry, sending no pipeline after that one; what landed before stays.
        """
        pending = list(keys)
        for start in range(0, len(pending), PUBLISH_BATCH):
            pipe = self._redis.client.pipeline(transaction=False)
            for key in pending[start : start + PUBLISH_BATCH]:
                self.append(pipe, kind, key)
            await pipe.execute()
        return len(pending)

    async def request_now(self, kind: JobKind, keys: Iterable[str]) -> int:
        """After a committed write, publish each key once, each pipeline under the request deadline; how many were asked for.

        Never raises: the first pipeline that fails ends the call, logged, and its keys and those after it are not counted.
        """
        pending = list(dict.fromkeys(keys))
        for start in range(0, len(pending), PUBLISH_BATCH):
            batch = pending[start : start + PUBLISH_BATCH]
            asked = await self._redis.after_commit(
                partial(self.publish, kind, batch),
                "request jobs",
                kind=kind.value,
                key=batch[0],
                not_requested=len(pending) - start,
            )
            if not asked:
                return start
        return len(pending)
