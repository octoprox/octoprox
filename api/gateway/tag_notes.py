# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The exit each tagged request of a recording project went through, noted for the exit reports that name the tag.

A tag's note is due in Redis when its exit changes and once half its TTL has passed since its last write, never once per
request of a tag a session reuses. The notes due go in one pipelined write every 100 ms, and Redis failing never fails a
request.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from api.core.logging import error_text
from api.core.loops import PeriodicLoop
from api.core.workers import WorkerName
from api.db.redis import TAG_NOTE_TTL_SECONDS
from api.reputation.tag_note import TagNote

if TYPE_CHECKING:
    from api.db.redis import RedisClient
    from api.geo.verifier import ExitDecision
    from api.models.project import Project

logger = structlog.get_logger()

# Seconds between two writes of the notes due: about how long after its request a tag's note can be read.
TAG_NOTE_WRITE_INTERVAL_SECONDS = 0.1
# Expired entries are swept every this many new ones, so tags used once cannot grow the map between their own requests.
_SWEEP_EVERY = 1024


@dataclass(slots=True)
class _Noted:
    note: TagNote
    # Local expiry on the clock, half the TTL after the tag's last request: by then its next request is due to write
    # anyway, so keeping it longer would only grow the map.
    expires_at: float
    # When the Redis copy is next due; zero makes it due at once.
    redis_due_at: float = 0.0


class TagNotes(PeriodicLoop):
    """The exit of each tag this gateway forwarded a request of, kept here and copied to Redis for the api: the notes due,
    in one pipelined write every ``interval`` and once more at :meth:`stop`.

    Args:
        ttl_seconds: how long Redis keeps a tag's note after a write; a tag in use is written again once half of it has
            passed, so its note stays readable at least that half after the tag's last request.
        interval: seconds between two writes.
        clock: monotonic seconds.
    """

    def __init__(
        self,
        redis_client: RedisClient,
        *,
        ttl_seconds: float = TAG_NOTE_TTL_SECONDS,
        interval: float = TAG_NOTE_WRITE_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name=WorkerName.TAG_NOTE_PUBLISHER, interval=interval)
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._clock = clock
        # Each tag's note by its Redis key.
        self._noted: dict[str, _Noted] = {}
        self._new_since_sweep = 0
        # The keys whose note is due, written as it stands by the next write.
        self._due: set[str] = set()
        # One write at a time, so a tag's notes land in the order it moved.
        self._writing = asyncio.Lock()

    @property
    def pending(self) -> int:
        """Notes due and not written yet."""
        return len(self._due)

    def note(self, project: Project, tag: str | None, decision: ExitDecision) -> None:
        """Note the exit ``decision`` forwards a request tagged ``tag`` through, for the next write when the note is due;
        nothing for a request without a tag or of a project whose exit reputation records nothing. Never waits."""
        if not tag or not project.exit_reputation.records:
            return
        proxy = decision.proxy
        current = TagNote(connector_id=proxy.connector_id, proxy_id=proxy.id, ip=proxy.static_exit_ip)
        key = TagNote.key(project.id, tag)
        now = self._clock()
        half = self._ttl / 2
        noted = self._noted.get(key)
        if noted is None or noted.note != current or noted.expires_at <= now:
            if noted is None:
                self._count_new()
            noted = self._noted[key] = _Noted(current, now + half)
        else:
            noted.expires_at = now + half
        if noted.redis_due_at > now:
            return
        noted.redis_due_at = now + half
        self._due.add(key)

    async def flush(self) -> int:
        """Write each note due as it stands, after the write under way, in one pipeline under the request deadline; how
        many were written.

        Raises when Redis did not take them: each is then written again at its tag's next due request.
        """
        async with self._writing:
            keys, self._due = self._due, set()
            notes = [(key, noted.note) for key in keys if (noted := self._noted.get(key)) is not None]
            if not notes:
                return 0
            ttl = max(1, int(self._ttl))
            async with self._redis.request_deadline():
                pipe = self._redis.client.pipeline(transaction=False)
                for key, note in notes:
                    pipe.set(key, note.model_dump_json(), ex=ttl)
                await pipe.execute()
            return len(notes)

    async def stop(self) -> None:
        """End the loop, then write what is due once more; what Redis does not take now is logged and lost. Never raises."""
        await super().stop()
        try:
            await self.flush()
        except Exception as exc:
            logger.warning("Tag notes not stored at stop", error=error_text(exc))

    async def _cycle(self) -> bool:
        """Write the notes due; a cycle with none is idle."""
        return bool(await self.flush())

    def _cadence(self) -> float | None:
        """None: a write slower than the interval only delays the next, which the job stats must not count as behind."""
        return None

    def _count_new(self) -> None:
        """Count one more tag, sweeping the expired ones every ``_SWEEP_EVERY``."""
        self._new_since_sweep += 1
        if self._new_since_sweep < _SWEEP_EVERY:
            return
        self._new_since_sweep = 0
        now = self._clock()
        for key, noted in list(self._noted.items()):
            if noted.expires_at <= now:
                del self._noted[key]
