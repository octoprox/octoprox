# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The exit each tagged request of a recording project went through, noted for the exit reports that name the tag.

A tag's note goes to Redis when its exit changes and once half its TTL has passed since the last write, never once per
request of a tag a session reuses, and Redis failing never fails a request.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from api.core.logging import error_text
from api.core.tasks import HeldTasks
from api.db.redis import TAG_NOTE_TTL_SECONDS
from api.feed.models import static_exit_ip
from api.reputation.tag_note import TagNote

if TYPE_CHECKING:
    from api.db.redis import RedisClient
    from api.geo.verifier import ExitDecision
    from api.models.project import Project

logger = structlog.get_logger()

# Expired entries are swept every this many new ones, so tags used once cannot grow the map between their own requests.
_SWEEP_EVERY = 1024


@dataclass(slots=True)
class _Noted:
    note: TagNote
    # Local expiry on the clock, half the TTL after the tag's last request: by then its next request is due to write
    # anyway, so keeping it longer would only grow the map.
    expires_at: float
    # When the Redis copy is next written; zero writes it at once.
    redis_due_at: float = 0.0


class TagNotes:
    """The exit of each tag this gateway forwarded a request of, kept here and copied to Redis for the api.

    Args:
        ttl_seconds: how long Redis keeps a tag's note after a write; a tag in use is written again once half of it has
            passed, so its note stays readable at least that half after the tag's last request.
        clock: monotonic seconds.
    """

    def __init__(
        self,
        redis_client: RedisClient,
        *,
        ttl_seconds: float = TAG_NOTE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds
        self._clock = clock
        # Each tag's note by its Redis key.
        self._noted: dict[str, _Noted] = {}
        self._new_since_sweep = 0
        # Writes on their way to Redis, held so none is collected before it runs.
        self._writes = HeldTasks()

    def note(self, project: Project, tag: str | None, decision: ExitDecision) -> None:
        """Note the exit ``decision`` forwards a request tagged ``tag`` through; nothing for a request without a tag or of
        a project whose exit reputation records nothing. Never waits."""
        if not tag or not project.exit_reputation.records:
            return
        proxy = decision.proxy
        current = TagNote(connector_id=proxy.connector_id, proxy_id=proxy.id, ip=static_exit_ip(proxy))
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
        self._writes.spawn(self._store(key), name="tag_note_write", project_id=project.id)

    async def settle(self) -> None:
        """Wait for the writes under way; the gateway calls it once its listeners are closed."""
        await self._writes.settle()

    async def _store(self, key: str) -> None:
        """Write one tag's note to Redis as it stands, and again while its exit moved during the write: writes go out on
        several connections and may land out of order, and the last to land then writes where the tag went. One Redis
        does not take is written again at the tag's next due request."""
        written: TagNote | None = None
        while (noted := self._noted.get(key)) is not None and noted.note != written:
            written = noted.note
            try:
                async with self._redis.request_deadline():
                    await self._redis.client.set(key, written.model_dump_json(), ex=max(1, int(self._ttl)))
            except Exception as exc:
                logger.debug("Tag note could not be stored", error=error_text(exc))
                return

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
