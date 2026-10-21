# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The controller's feed builder: the definitions in Postgres and the runtime state in Redis, published as the routing feed.

It follows the change and runtime streams, compiles at most once per ``feed_compile_interval`` and publishes fenced by
the controller lease, while the controller leads.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from api.controller.producers import JobKeys
from api.core import utc_now
from api.core.consumers import BroadcastFollower
from api.core.logging import error_text
from api.core.roles import JobKind
from api.core.workers import WorkerName
from api.db.redis import CHANGES_STREAM_KEY, RUNTIME_STREAM_KEY
from api.db.streams import retry_delay
from api.feed.models import RUNTIME_SECTION, RuntimeSection
from api.feed.state import RuntimeStateReader
from api.jobs.attribution import REATTRIBUTE_ALL

if TYPE_CHECKING:
    from api.core.leadership import Lease
    from api.db.redis import RedisClient
    from api.db.streams import StreamEntry
    from api.feed.compiler import CompiledFeed, FullFeedCompiler
    from api.feed.models import Section
    from api.feed.publisher import FeedPublisher
    from api.jobs.queue import JobQueue
    from api.providers.registry import ProviderRegistry

logger = structlog.get_logger()


class FeedBuilder(BroadcastFollower):
    """Compiles the definitions, reads the runtime state and publishes the feed as the streams ask; the leader runs it.

    Args:
        compile_interval: the least time between two compiles, in seconds.
    """

    def __init__(
        self,
        compiler: FullFeedCompiler,
        publisher: FeedPublisher,
        lease: Lease,
        jobs: JobQueue,
        redis_client: RedisClient,
        *,
        registry: ProviderRegistry,
        compile_interval: float,
        reconcile_interval: float,
    ) -> None:
        super().__init__(
            redis_client,
            [CHANGES_STREAM_KEY, RUNTIME_STREAM_KEY],
            name=WorkerName.FEED_BUILDER,
            reconcile_interval=reconcile_interval,
            lease=lease,
        )
        self._compiler = compiler
        self._publisher = publisher
        self._jobs = jobs
        self._registry = registry
        self._state = RuntimeStateReader(redis_client)
        self._compile_interval = compile_interval
        # The changes stream's cursor: how far a compile has read it.
        self._changes = self._cursors[0]
        # What is owed, each until it is done, so a cycle that fails part-way
        # loses none of it.
        self._compile_owed = False
        self._runtime_owed = False
        self._unpublished = False
        self._reattribute_owed = False
        # The last compile and the term it was made in: a lead publishes
        # nothing until a compile of its own has succeeded.
        self._compiled: CompiledFeed | None = None
        self._compiled_term: int | None = None
        # The feed as this process would publish it, and the runtime state
        # last read: quarantines and traffic blocks by ID, each with its end.
        self._sections: dict[str, Section] = {}
        self._quarantined: dict[str, datetime] = {}
        self._blocked: dict[str, datetime] = {}
        # When the last compile began (monotonic), and the retry after a
        # streak of failed compiles: the next waits for both.
        self._compile_started = -math.inf
        self._compile_failures = 0
        self._compile_retry_at = 0.0
        # The keys the job producers publish, made anew by each compile.
        self.job_keys = JobKeys()
        # For the controller's snapshot, readiness and metrics; an error
        # stays until a compile or a publish succeeds.
        self.compiles = 0
        self.compile_ms: float | None = None
        self.compile_error: str | None = None
        self.publishes = 0
        self.publish_ms: float | None = None
        self.published_at: datetime | None = None
        self.sections_written = 0
        self.sections_removed = 0
        self.publish_error: str | None = None

    @property
    def compiled(self) -> CompiledFeed | None:
        """The last compile, whichever term it was made in; None before the first."""
        return self._compiled

    @property
    def sections(self) -> Mapping[str, Section]:
        """The feed as this process last built it."""
        return self._sections

    @property
    def quarantined(self) -> int:
        """How many proxies the feed carries in quarantine."""
        runtime = self._sections.get(RUNTIME_SECTION)
        return len(runtime.quarantined) if isinstance(runtime, RuntimeSection) else 0

    @property
    def blocked_connectors(self) -> int:
        """How many connectors the feed carries a traffic block for that is still in its period."""
        runtime = self._sections.get(RUNTIME_SECTION)
        if not isinstance(runtime, RuntimeSection):
            return 0
        now = utc_now()
        return sum(1 for until in runtime.traffic_blocked_until.values() if until > now)

    # --- What the streams ask for ---------------------------------------------------------

    def _on_entries(self, key: str, entries: list[StreamEntry]) -> bool:
        """A change entry owes a compile and a runtime entry the runtime section; neither owes a full read."""
        if key == CHANGES_STREAM_KEY:
            self._compile_owed = True
        else:
            self._runtime_owed = True
        return False

    async def _full_read(self) -> None:
        """Owe a compile and the runtime section: the streams may have missed anything."""
        self._compile_owed = self._runtime_owed = True

    async def _on_term(self, term: int) -> None:
        """A new lead reads in full and compiles at once, whatever the compiles of the lead before waited for."""
        await super()._on_term(term)
        self._compile_started = -math.inf
        self._compile_failures = 0
        self._compile_retry_at = 0.0

    def _next_duty(self) -> float:
        """Seconds until something is owed that no entry wakes the builder for; 0 while something can be done now."""
        if self._runtime_owed or (self._may_publish() and (self._unpublished or self._reattribute_owed)):
            return 0.0
        duty = super()._next_duty()
        if self._compile_owed:
            duty = min(duty, max(0.0, self._compile_ready_at() - time.monotonic()))
        return duty

    def _cadence(self) -> float:
        """The compile interval: a cycle longer than it is a compile the builder could not keep up with."""
        return self._compile_interval

    # --- The work -----------------------------------------------------------------------------

    async def _act(self) -> bool:
        """Do what is owed and may be done now, in order: the runtime state, the compile, the publish, the reattribute.

        Redis failures raise and leave the rest owed; a compile that fails keeps the last one and is retried later.
        """
        worked = await super()._act()
        term = self._term
        if self._runtime_owed:
            await self._read_runtime()
            worked = True
        if self._compile_owed and time.monotonic() >= self._compile_ready_at():
            await self._compile(term)
            worked = True
        compiled = self._compiled
        if compiled is None or not self._may_publish():
            return worked
        if self._unpublished:
            await self._publish(compiled)
            worked = True
        if self._reattribute_owed:
            await self._jobs.publish(JobKind.REATTRIBUTE, [REATTRIBUTE_ALL])
            self._reattribute_owed = False
            worked = True
        return worked

    def _may_publish(self) -> bool:
        """Whether this lead has compiled: a lead never publishes a compile made before it began."""
        return self._compiled_term is not None and self._compiled_term == self._term

    def _compile_ready_at(self) -> float:
        """When the owed compile may run (monotonic): ``compile_interval`` after the last began, and after its retry."""
        return max(self._compile_started + self._compile_interval, self._compile_retry_at)

    async def _read_runtime(self) -> None:
        """Read the quarantines and the traffic blocks, and make the runtime section again from the compile in hand.

        A pure read: Postgres is not read and nothing is trimmed, and an unchanged section is not published again.
        """
        quarantined = await self._state.quarantined()
        blocked = await self._state.blocked()
        self._quarantined, self._blocked = quarantined, blocked
        self._runtime_owed = False
        if self._compiled is None:
            return
        section = self._compiler.runtime_section(quarantined, blocked)
        if section != self._sections.get(RUNTIME_SECTION):
            self._sections[RUNTIME_SECTION] = section
            self._unpublished = True

    async def _compile(self, term: int | None) -> None:
        """Compile every definition, as of the change position read so far; a failure keeps the last compile.

        A failure is logged once per streak and tried again 1 s later, doubling to 30 s.
        """
        started = self._compile_started = time.monotonic()
        try:
            compiled = await self._compiler.compile(
                quarantined=self._quarantined, blocked_until=self._blocked, changes=self._changes.position
            )
        except Exception as exc:
            self._compile_failures += 1
            delay = retry_delay(self._compile_failures)
            self._compile_retry_at = time.monotonic() + delay
            error = error_text(exc)
            log = logger.error if error != self.compile_error else logger.debug
            log("Routing feed compile failed; publishing from the last compile", error=error, retry_in=delay)
            self.compile_error = error
            return
        self._compile_failures = 0
        self._compile_retry_at = 0.0
        self.compile_error = None
        self.compile_ms = round((time.monotonic() - started) * 1000, 1)
        self.compiles += 1
        self._compiled, self._compiled_term = compiled, term
        self._sections = dict(compiled.sections)
        self.job_keys = JobKeys.of(compiled.connectors, self._registry)
        self._compile_owed = False
        self._unpublished = True
        logger.debug("Routing feed compiled", changes=compiled.changes, duration_ms=self.compile_ms)

    async def _publish(self, compiled: CompiledFeed) -> None:
        """Write the feed this process built, fenced by the lease; a head found gone owes ``reattribute all``."""
        started = time.monotonic()
        try:
            result = await self._publisher.publish(
                self._sections, changes=compiled.changes, compiled_at=compiled.compiled_at
            )
        except Exception as exc:
            self.publish_error = error_text(exc)
            raise
        self.publish_ms = round((time.monotonic() - started) * 1000, 1)
        if result.lease_lost:
            # Another controller leads: what it publishes stands.
            if self._lease is not None:
                self._lease.mark_lost()
            return
        self.publish_error = None
        self._unpublished = False
        self.publishes += 1
        self.published_at = utc_now()
        self.sections_written += result.written
        self.sections_removed += result.removed
        if result.head_missing:
            # A fresh Redis (a first start, a switch-over from 5.x) or one that
            # lost its data, which this process cannot tell apart: the one-shot
            # jobs asked for before are gone either way.
            logger.warning(
                "Routing feed head absent from Redis (a first start, or Redis lost its data); "
                "re-attributing every proxy",
                sections=len(self._sections),
            )
            self._reattribute_owed = True
