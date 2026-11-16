# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The controller's feed builder: the definitions in Postgres and the runtime state in Redis, published as the routing feed.

It follows the change and runtime streams, compiles at most once per ``feed_compile_interval`` (and reads exit
reputation from Postgres at most as often, for the projects that owe a read) and publishes fenced by the controller
lease, while the controller leads.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
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
from api.db.streams import REF_FIELD, RuntimeSignal, retry_delay
from api.feed.models import REPUTATION_SECTION, RUNTIME_SECTION, RuntimeSection
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


class _Pace:
    """When a Postgres read the builder owes may run: at most once per interval, and after a streak of failures only once
    a back-off has passed, 1 s doubling to 30 s."""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        # When the last read began (monotonic), and the retry after a streak
        # of failed reads: the next waits for both.
        self._started = -math.inf
        self._failures = 0
        self._retry_at = 0.0

    @property
    def ready_at(self) -> float:
        """When the next read may begin (monotonic): an interval after the last began, and after its retry."""
        return max(self._started + self._interval, self._retry_at)

    def begin(self) -> float:
        """Note that a read begins now (monotonic), which is returned."""
        self._started = time.monotonic()
        return self._started

    def failed(self) -> float:
        """Note that the read failed; the seconds until it may be tried again."""
        self._failures += 1
        delay = retry_delay(self._failures)
        self._retry_at = time.monotonic() + delay
        return delay

    def succeeded(self) -> None:
        """Note that the read succeeded: a streak of failures before it is over."""
        self._failures = 0
        self._retry_at = 0.0

    def restart(self) -> None:
        """Forget the last read and any failures: a new lead reads at once."""
        self._started = -math.inf
        self.succeeded()


class _ReputationOwed:
    """The exit reputation reads the builder owes: some projects', or every one's (a full read, or an entry naming
    several)."""

    def __init__(self) -> None:
        self.projects: set[str] = set()
        self.everything = False

    def __bool__(self) -> bool:
        return self.everything or bool(self.projects)

    def owe(self, project_ids: Iterable[str]) -> None:
        """Owe a read of each of ``project_ids``; an empty id (an entry naming several projects) owes every one."""
        for project_id in project_ids:
            if project_id:
                self.projects.add(project_id)
            else:
                self.everything = True

    def owe_everything(self) -> None:
        self.everything = True

    def asked(self) -> list[str] | None:
        """What a read pays now: the projects in order, or None for every one."""
        return None if self.everything else sorted(self.projects)

    def paid(self, asked: list[str] | None) -> None:
        """A read of ``asked`` succeeded: what it covered is owed no more."""
        if asked is None:
            self.projects.clear()
            self.everything = False
        else:
            self.projects.difference_update(asked)


class FeedBuilder(BroadcastFollower):
    """Compiles the definitions, reads the runtime state and publishes the feed as the streams ask; the leader runs it.

    Args:
        compile_interval: the least time between two compiles, in seconds, and between two reads of exit reputation.
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
        # loses none of it. Exit reputation is read per project: those a
        # reputation entry names, every one at a full read, and those whose
        # exits a compile moved.
        self._compile_owed = False
        self._runtime_owed = False
        self._reputation_owed = _ReputationOwed()
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
        # The pace of the two Postgres reads: compiles, and the reads of exit
        # reputation between them, each at most once per compile interval.
        self._compile_pace = _Pace(compile_interval)
        self._reputation_pace = _Pace(compile_interval)
        # The last reputation read's error, logged once per streak.
        self._reputation_error: str | None = None
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
        """A change entry owes a compile; a runtime entry owes the runtime section, a reputation one a read from
        Postgres of the exit reputation of the project it names instead. None owes a full read."""
        if key == CHANGES_STREAM_KEY:
            self._compile_owed = True
            return False
        for entry in entries:
            if entry.kind == RuntimeSignal.REPUTATION:
                self._reputation_owed.owe([entry.fields.get(REF_FIELD, "")])
            else:
                self._runtime_owed = True
        return False

    async def _full_read(self) -> None:
        """Owe a compile, the runtime section and the exit reputation of every project: the streams may have missed
        anything."""
        self._compile_owed = self._runtime_owed = True
        self._reputation_owed.owe_everything()

    async def _on_term(self, term: int) -> None:
        """A new lead reads in full and compiles at once, whatever the compiles of the lead before waited for."""
        await super()._on_term(term)
        self._compile_pace.restart()
        self._reputation_pace.restart()

    def _next_duty(self) -> float:
        """Seconds until something is owed that no entry wakes the builder for; 0 while something can be done now."""
        if self._runtime_owed or (self._may_publish() and (self._unpublished or self._reattribute_owed)):
            return 0.0
        duty = super()._next_duty()
        now = time.monotonic()
        if self._compile_owed:
            duty = min(duty, max(0.0, self._compile_pace.ready_at - now))
        if self._reputation_owed and self._compiled is not None:
            # Read for what a compile holds: none before the first.
            duty = min(duty, max(0.0, self._reputation_pace.ready_at - now))
        return duty

    def _cadence(self) -> float:
        """The compile interval: a cycle longer than it is a compile the builder could not keep up with."""
        return self._compile_interval

    # --- The work -----------------------------------------------------------------------------

    async def _act(self) -> bool:
        """Do what is owed and may be done now, in order: the runtime state, the compile, the publish, the reattribute,
        the exit reputation.

        The exit reputation is read right after a compile, when one is owed, so a new lead's first publish and a moved
        exit carry it; otherwise last, so a quarantine or a traffic block read in the same cycle never waits for
        Postgres, and what the read changes is published by the next cycle, which runs at once while it is unpublished.
        Redis failures raise and leave the rest owed; a compile or a reputation read that fails keeps the last one and is
        retried later.
        """
        worked = await super()._act()
        term = self._term
        if self._runtime_owed:
            await self._read_runtime()
            worked = True
        if self._compile_owed and time.monotonic() >= self._compile_pace.ready_at:
            if await self._compile(term) and self._reputation_due():
                await self._read_reputation()
            worked = True
        compiled = self._compiled
        if compiled is not None and self._may_publish():
            if self._unpublished:
                await self._publish(compiled)
                worked = True
            if self._reattribute_owed:
                await self._jobs.publish(JobKind.REATTRIBUTE, [REATTRIBUTE_ALL])
                self._reattribute_owed = False
                worked = True
        if self._reputation_due():
            await self._read_reputation()
            worked = True
        return worked

    def _reputation_due(self) -> bool:
        """Whether a read of exit reputation is owed and may run now: after a compile, at its pace."""
        return (
            bool(self._reputation_owed)
            and self._compiled is not None
            and time.monotonic() >= self._reputation_pace.ready_at
        )

    def _may_publish(self) -> bool:
        """Whether this lead has compiled: a lead never publishes a compile made before it began."""
        return self._compiled_term is not None and self._compiled_term == self._term

    async def _read_runtime(self) -> None:
        """Read the quarantines and the traffic blocks, and make the runtime section again from the compile in hand.

        A pure read of Redis: Postgres is not read and nothing is trimmed, and an unchanged section is not published
        again.
        """
        quarantined = await self._state.quarantined()
        blocked = await self._state.blocked()
        self._quarantined, self._blocked = quarantined, blocked
        self._runtime_owed = False
        if self._compiled is not None:
            self._rebuild(RUNTIME_SECTION, self._compiler.runtime_section(quarantined, blocked))

    async def _read_reputation(self) -> None:
        """Read again from Postgres the exit reputation of the projects owed a read, for what the compile in hand holds,
        and make its section again; a failure keeps the last read and leaves the read owed.

        A failure is logged once per streak and tried again 1 s later, doubling to 30 s, as a compile is.
        """
        asked = self._reputation_owed.asked()
        self._reputation_pace.begin()
        try:
            await self._compiler.read_reputation(asked)
        except Exception as exc:
            delay = self._reputation_pace.failed()
            error = error_text(exc)
            log = logger.error if error != self._reputation_error else logger.debug
            log("Exit reputation read failed; routing on the last read", error=error, retry_in=delay)
            self._reputation_error = error
            return
        self._reputation_pace.succeeded()
        self._reputation_error = None
        self._reputation_owed.paid(asked)
        self._rebuild(REPUTATION_SECTION, self._compiler.reputation_section())

    def _rebuild(self, name: str, section: Section) -> None:
        """Hold ``section`` as the feed's section ``name``, owing a publish when it changed."""
        if section != self._sections.get(name):
            self._sections[name] = section
            self._unpublished = True

    async def _compile(self, term: int | None) -> bool:
        """Compile every definition, as of the change position read so far; whether it did, a failure keeping the last
        compile.

        The compile reads no exit reputation: it names the projects it gave something new to be read for, which then
        owe a read. A failure is logged once per streak and tried again 1 s later, doubling to 30 s.
        """
        started = self._compile_pace.begin()
        try:
            compiled = await self._compiler.compile(
                quarantined=self._quarantined, blocked_until=self._blocked, changes=self._changes.position
            )
        except Exception as exc:
            delay = self._compile_pace.failed()
            error = error_text(exc)
            log = logger.error if error != self.compile_error else logger.debug
            log("Routing feed compile failed; publishing from the last compile", error=error, retry_in=delay)
            self.compile_error = error
            return False
        self._compile_pace.succeeded()
        self.compile_error = None
        self.compile_ms = round((time.monotonic() - started) * 1000, 1)
        self.compiles += 1
        self._compiled, self._compiled_term = compiled, term
        self._sections = dict(compiled.sections)
        self.job_keys = JobKeys.of(compiled.connectors, self._registry)
        self._compile_owed = False
        self._reputation_owed.owe(compiled.reputation_owed)
        self._unpublished = True
        logger.debug("Routing feed compiled", changes=compiled.changes, duration_ms=self.compile_ms)
        return True

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
