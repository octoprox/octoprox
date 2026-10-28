# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The node: one process's shared infrastructure, the runtimes of its role, the loops it runs once, and what it reports.

The app's lifespan builds and starts one per process; role ``all`` runs every runtime in it, and they talk only through
Redis and Postgres, as separate processes would.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

import structlog
from sqlalchemy import text

from api import __version__
from api.core import utc_now
from api.core.job_stats import job_stats
from api.core.logging import error_text
from api.core.loop_lag import LoopLagMonitor
from api.core.loops import PeriodicLoop
from api.core.roles import Role
from api.core.seed import seed_admin_user
from api.core.tasks import TaskSet, quietly
from api.core.telemetry import Telemetry
from api.core.workers import WORKERS, WorkerName, worker_info
from api.db.migrations import run_migrations
from api.db.redis import INSTANCE_HEARTBEAT_INTERVAL, RedisClient
from api.db.session import get_async_session_factory
from api.db.streams import ChangeKind, ChangeLog
from api.geo.runtime import GeoStack
from api.geo.store import DatabaseRetry, GeoDatabaseStore, HttpBlobSource, PostgresBlobSource
from api.jobs.queue import JobQueue
from api.models.system import InstanceRuntime, InstanceSnapshot, WorkerTask
from api.providers.registry import registry_from_settings
from api.providers.store import ProviderStore
from api.roles.api import ApiRuntime
from api.roles.controller import ControllerRuntime
from api.roles.gateway import GatewayRuntime
from api.roles.refresh import DefinitionsRefresher
from api.roles.worker import WorkerRuntime

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from api.core.config import Settings
    from api.core.loops import Loop
    from api.gateway.latency import LatencyHistogram
    from api.models.system import StreamState
    from api.providers.registry import ProviderRegistry
    from api.roles.runtime import RoleRuntime

logger = structlog.get_logger()

# The roles whose processes use Postgres: each migrates before it serves, and
# is unready while the heartbeat's SELECT 1 gets no answer.
POSTGRES_ROLES = frozenset({Role.API, Role.CONTROLLER, Role.WORKER, Role.ALL})
# The roles that are unready while the heartbeat cannot write to Redis: a
# controller leads, compiles and publishes through it, and a worker reads its
# jobs, sightings and observations there. An api serves its reads without the
# soft state and its writes without their job requests, and a gateway routes
# on the feed it holds and fails its request-path calls open, so both stay ready.
REDIS_READINESS_ROLES = frozenset({Role.CONTROLLER, Role.WORKER, Role.ALL})
# The roles that seed the first admin user: the ones that serve sign-in.
ADMIN_SEED_ROLES = frozenset({Role.API, Role.ALL})
# How long, in seconds, the heartbeat waits for its SELECT 1 before it counts
# Postgres unreachable: well within the 10 s its keys live. A probe that has
# not answered by then is left to finish, and cancelled only at the stop.
DEPENDENCY_CHECK_SECONDS = 2.0
# What the heartbeat asks Postgres: nothing a table holds, only an answer.
_SELECT_ONE = text("SELECT 1")


class _Phase(StrEnum):
    """Where a node is in its life; every phase but ``serving`` is also its reason not to be ready."""

    BUILT = "not started"
    STARTING = "starting"
    SERVING = "serving"
    STOPPING = "stopping"
    STOPPED = "stopped"


class Heartbeat(PeriodicLoop):
    """The process's heartbeat: each beat checks Postgres, then advertises the process in Redis; the first as it starts.

    A failed check or write raises nothing, so the loop neither logs a failed cycle nor backs off: each is logged as its
    streak begins, and :meth:`readiness` reports it until a beat finds the store answering again.

    Args:
        engine: the pool the ``SELECT 1`` runs on; None for a role without Postgres.
        snapshot: the process's snapshot, which each beat publishes.
    """

    first_at_once: ClassVar[bool] = True

    def __init__(
        self,
        redis_client: RedisClient,
        engine: AsyncEngine | None,
        *,
        instance_id: str,
        role: Role,
        snapshot: Callable[[], InstanceSnapshot],
        interval: float,
    ) -> None:
        super().__init__(name=WorkerName.HEARTBEAT, interval=interval)
        self._redis = redis_client
        self._engine = engine
        self._instance_id = instance_id
        self._role = role
        self._snapshot = snapshot
        # Why the last beat's write to Redis failed, and why its last SELECT 1
        # did; None while each works (and before the first beat). Readiness
        # says which store, never this text: the probes answer without sign-in.
        self._redis_error: str | None = None
        self._postgres_error: str | None = None
        # The SELECT 1 while it runs: one at a time, however late.
        self._postgres_probe: asyncio.Task[str | None] | None = None

    def readiness(self) -> list[str]:
        """Each store the last beat found unreachable that this role cannot serve without, as readiness names it."""
        reasons: list[str] = []
        if self._postgres_error is not None:
            reasons.append("Postgres unreachable")
        if self._redis_error is not None and self._role in REDIS_READINESS_ROLES:
            reasons.append("Redis unreachable")
        return reasons

    def cancel_probe(self) -> None:
        """Cancel a ``SELECT 1`` still running at the process's stop, without awaiting it.

        A probe on a server that went silent ends only when its connection gives up.
        """
        if self._postgres_probe is not None:
            self._postgres_probe.cancel()

    async def _cycle(self) -> bool:
        """One beat: check Postgres, then advertise; it always counts as worked."""
        if self._engine is not None:
            error = await self._check_postgres(self._engine)
            if error is not None and self._postgres_error is None:
                logger.warning("Postgres check failed", error=error)
            self._postgres_error = error
        await self._advertise()
        return True

    def _count_cycle(self, duration_ms: float, worked: bool) -> None:
        """File the beat as a run, failed with why its write to Redis failed when it did."""
        job_stats.record(self.name, duration_ms, self._redis_error, idle=not worked)

    async def _advertise(self) -> None:
        """Write the process's registry key and snapshot under the request deadline, noting why it failed, if it did."""
        snapshot = self._snapshot_json()
        try:
            async with self._redis.request_deadline():
                await self._redis.advertise_instance(self._instance_id, self._role.value, snapshot)
        except Exception as exc:
            if self._redis_error is None:
                logger.warning("Instance heartbeat failed", error=error_text(exc))
            self._redis_error = error_text(exc)
        else:
            self._redis_error = None

    async def _check_postgres(self, engine: AsyncEngine) -> str | None:
        """Why Postgres cannot be used now, or None: a ``SELECT 1`` on the pool, answered within ``DEPENDENCY_CHECK_SECONDS``.

        A probe that has not answered by then counts as a failure and is left to finish, the next beats waiting on it.
        """
        probe = self._postgres_probe
        if probe is None or probe.done():
            probe = self._postgres_probe = asyncio.create_task(
                self._postgres_answers(engine), name="postgres_check"
            )
        done, _ = await asyncio.wait({probe}, timeout=DEPENDENCY_CHECK_SECONDS)
        return probe.result() if done else f"no answer within {DEPENDENCY_CHECK_SECONDS:g} s"

    def _snapshot_json(self) -> str | None:
        """The snapshot as the heartbeat publishes it; None, logged, when it cannot be built.

        The registry key is written either way: a snapshot that fails to build must not take the process off the System
        page.
        """
        try:
            return self._snapshot().model_dump_json()
        except Exception:
            logger.warning("Instance snapshot could not be built", exc_info=True)
            return None

    @staticmethod
    async def _postgres_answers(engine: AsyncEngine) -> str | None:
        """None when a ``SELECT 1`` on ``engine``'s pool answers, else why not; never raises but for a cancel."""
        try:
            async with engine.connect() as connection:
                await connection.execute(_SELECT_ONE)
        except Exception as exc:
            return error_text(exc)
        return None


class Node:
    """One process: its infrastructure, the runtimes of its role, its own loops, its readiness and its snapshot.

    Built without I/O; :meth:`start` migrates, connects and starts, and :meth:`stop` undoes it all and never raises.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.role = settings.role
        self.started_at: datetime | None = None
        self.tasks = TaskSet()
        # One per process: what its heartbeat and metrics report as loop lag.
        self.loop_lag = LoopLagMonitor()
        self.telemetry = Telemetry(self.snapshot, self.latencies)
        self._phase = _Phase.BUILT
        # The runtimes asked to start, in start order: stopped in reverse, one whose start raised included.
        self._starting: list[RoleRuntime] = []
        # The loops the process runs once whatever its runtimes: the observation recorder where the role records
        # observations, the IP database retry where it has databases, and the definitions refresher in role api,
        # where no worker runtime runs one.
        self._loops: list[Loop] = []
        # Started after everything else and stopped after the node's own loops; None in role echo.
        self._heartbeat: Heartbeat | None = None
        self._redis_connected = False
        self._advertised = False
        self.redis_client: RedisClient | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None
        self.engine: AsyncEngine | None = None
        # What the process's writes announce themselves on; Postgres roles only.
        self.changes: ChangeLog | None = None
        self.registry: ProviderRegistry | None = None
        self.provider_store: ProviderStore | None = None
        self.geo: GeoStack | None = None
        self.echo_store: GeoDatabaseStore | None = None
        self.api: ApiRuntime | None = None
        self.controller: ControllerRuntime | None = None
        self.worker: WorkerRuntime | None = None
        self.gateway: GatewayRuntime | None = None
        # The process's definitions refresher: a worker runtime's own, else
        # the node's (role api); None in the gateway and controller roles,
        # which follow the feed and the compile instead.
        self.refresher: DefinitionsRefresher | None = None
        # The one the node runs itself: role api, where no worker runtime does.
        self._own_refresher: DefinitionsRefresher | None = None
        if self.role == Role.ECHO:
            self.echo_store = GeoDatabaseStore.echo(settings)
            return
        self._build(settings)

    def _build(self, settings: Settings) -> None:
        """The shared infrastructure, the role's runtimes and the process's own loops, constructed and not started."""
        role = self.role
        redis_client = RedisClient(
            settings.redis_url, name=settings.instance_id, request_timeout=settings.redis_request_timeout
        )
        session_factory: async_sessionmaker[AsyncSession] | None = None
        if role in POSTGRES_ROLES:
            # The cached factory, so the routes' get_db shares its engine and pool.
            session_factory = get_async_session_factory(
                settings.database_url, settings.db_application_name, settings.debug
            )
            self.engine = session_factory.kw["bind"]
        registry = registry_from_settings(settings)
        provider_store = ProviderStore(registry, session_factory)
        geo = GeoStack.build(
            settings,
            session_factory=session_factory,
            redis_client=redis_client,
            # A gateway fetches the files the feed lists from the api; a process with Postgres reads them there.
            blob_source=PostgresBlobSource(session_factory)
            if session_factory is not None
            else HttpBlobSource.from_settings(settings),
        )
        self.redis_client = redis_client
        self.session_factory = session_factory
        self.registry = registry
        self.provider_store = provider_store
        self.geo = geo
        self._heartbeat = Heartbeat(
            redis_client,
            self.engine,
            instance_id=settings.instance_id,
            role=role,
            snapshot=self.snapshot,
            interval=INSTANCE_HEARTBEAT_INTERVAL,
        )
        if WORKERS[WorkerName.GEO_OBSERVATION_PUBLISHER].runs_in(role):
            self._loops.append(geo.observation_recorder)
        if WORKERS[WorkerName.GEO_DATABASE_RETRY].runs_in(role):
            self._loops.append(DatabaseRetry(geo.database_store))
        if session_factory is not None:
            self._build_postgres_runtimes(settings, redis_client, session_factory, provider_store, geo)
        if role in (Role.GATEWAY, Role.ALL):
            self.gateway = GatewayRuntime(settings, redis_client=redis_client, provider_store=provider_store, geo=geo)

    def _build_postgres_runtimes(
        self,
        settings: Settings,
        redis_client: RedisClient,
        session_factory: async_sessionmaker[AsyncSession],
        provider_store: ProviderStore,
        geo: GeoStack,
    ) -> None:
        """The change log, the runtimes of the roles that use Postgres, and the process's definitions refresher."""
        role = self.role
        changes = self.changes = ChangeLog(redis_client)
        if role in (Role.API, Role.ALL):
            self.api = ApiRuntime(
                settings,
                engine=session_factory.kw["bind"],
                session_factory=session_factory,
                redis_client=redis_client,
                jobs=JobQueue(redis_client),
                provider_store=provider_store,
                geo=geo,
                changes=changes,
            )
        if role in (Role.CONTROLLER, Role.ALL):
            self.controller = ControllerRuntime(
                settings,
                session_factory=session_factory,
                redis_client=redis_client,
                provider_store=provider_store,
                geo_settings=geo.settings_store,
            )
        if role in (Role.WORKER, Role.ALL):
            self.worker = WorkerRuntime(
                settings,
                redis_client=redis_client,
                session_factory=session_factory,
                provider_store=provider_store,
                geo=geo,
                changes=changes,
            )
            self.refresher = self.worker.refresher
        elif role == Role.API:
            self.refresher = self._own_refresher = DefinitionsRefresher(
                provider_store,
                geo.settings_store,
                geo.database_store,
                redis_client=redis_client,
                reconcile_interval=settings.reconcile_interval,
            )
            self._loops.append(self._own_refresher)

    @property
    def instance_id(self) -> str:
        """This process's instance id: its heartbeat key, its lease owner and every log line's ``instance``."""
        return self.settings.instance_id

    # --- Lifecycle ----------------------------------------------------------------------

    async def start(self) -> None:
        """Migrate, connect, seed, read the definitions, start the role's runtimes and then the process's own loops.

        Raises what failed, once everything that had started is stopped: a process never serves half a role.
        """
        if self._phase is not _Phase.BUILT:
            raise RuntimeError(f"a node starts once; this one is {self._phase.value}")
        self._phase = _Phase.STARTING
        self.started_at = utc_now()
        logger.info("Starting Octoprox", version=__version__, role=self.role.value)
        try:
            self.tasks.spawn(WorkerName.LOOP_LAG_MONITOR, self.loop_lag.run())
            if self.echo_store is not None:
                await self._start_echo(self.echo_store)
            elif self.redis_client is not None:
                await self._start_role(self.redis_client)
        except BaseException:
            await self.stop()
            raise
        self._phase = _Phase.SERVING
        logger.info("Octoprox started", version=__version__, role=self.role.value)

    async def _start_echo(self, store: GeoDatabaseStore) -> None:
        """Open the configured IP database files: all an echo process serves from."""
        loaded = await store.sync_all()
        logger.info("Echo databases opened", databases=loaded)

    async def _start_role(self, redis_client: RedisClient) -> None:
        """Every role but echo, in the order :meth:`start` gives; the heartbeat last."""
        migrated = False
        if self.session_factory is not None:
            # Blocking (Alembic, and a wait for the lock), so off the event loop.
            migrated = await asyncio.to_thread(run_migrations, self.settings.database_url_sync)
        await redis_client.connect()
        self._redis_connected = True
        if migrated and self.changes is not None:
            # A revision may have changed any row, so every running process reads all it holds again.
            await self.changes.record(ChangeKind.ALL)
        if self.session_factory is not None:
            if self.role in ADMIN_SEED_ROLES:
                await seed_admin_user(self.session_factory, self.settings)
            await self._load_definitions()
        for runtime in self._runtimes():
            self._starting.append(runtime)
            await runtime.start()
        for loop in self._loops:
            self.tasks.spawn(loop.name, loop.run())
        self._advertised = True
        if self._heartbeat is not None:
            self.tasks.spawn(self._heartbeat.name, self._heartbeat.run())

    async def _load_definitions(self) -> None:
        """Read the definitions a Postgres role starts with, before any runtime starts.

        An api or worker process makes its refresher's first read, and the controller reads the descriptors only.
        """
        if self.refresher is not None:
            await self.refresher.start()
        elif self.provider_store is not None:
            await self.provider_store.sync_all()

    async def stop(self) -> None:
        """Stop the runtimes in reverse, then the process's own loops, then close what the process holds; never raises.

        A second call does nothing.
        """
        if self._phase in (_Phase.STOPPING, _Phase.STOPPED):
            return
        self._phase = _Phase.STOPPING
        logger.info("Stopping Octoprox", role=self.role.value)
        for runtime in reversed(self._starting):
            await quietly("runtime", runtime.stop())
        self._starting.clear()
        # After the runtimes, so the observation recorder sends what their stops recorded.
        for loop in self._loops:
            await quietly(f"{loop.name} loop", loop.stop())
        # After the node's own loops, so the process beats on while the runtimes and those loops stop.
        if self._heartbeat is not None:
            await quietly(f"{self._heartbeat.name} loop", self._heartbeat.stop())
        # Before the readers close, so no retry opens one after.
        await quietly("loops", self.tasks.stop())
        if self.geo is not None:
            await quietly("attribution", self.geo.close())
        if self._heartbeat is not None:
            self._heartbeat.cancel_probe()
        if self.echo_store is not None:
            self.echo_store.close_all()
        if self.changes is not None:
            await quietly("change log", self.changes.close())
        if self.redis_client is not None and self._redis_connected:
            if self._advertised:
                await quietly("withdraw", self.redis_client.withdraw_instance(self.instance_id))
            await quietly("Redis", self.redis_client.close())
        if self.engine is not None:
            await quietly("Postgres", self.engine.dispose())
        self._phase = _Phase.STOPPED
        logger.info("Octoprox stopped", role=self.role.value)

    # --- What the process reports ------------------------------------------------------------

    def readiness(self) -> list[str]:
        """Why the process should not take traffic now, empty when ready: each store the last heartbeat missed, then each runtime's reason.

        A runtime's reason is prefixed with its name, and the node's own refresher's with ``definitions``.
        """
        if self._phase is not _Phase.SERVING:
            return [self._phase.value]
        reasons = self._heartbeat.readiness() if self._heartbeat is not None else []
        reasons += [
            f"{name}: {reason}"
            for name, runtime in self._named_runtimes()
            if (reason := runtime.readiness()) is not None
        ]
        if self._own_refresher is not None:
            reason = self._own_refresher.readiness()
            if reason is not None:
                reasons.append(f"definitions: {reason}")
        return reasons

    def snapshot(self) -> InstanceSnapshot:
        """What this process reports about itself now, from memory: the heartbeat's payload and the metrics' source."""
        reasons = self.readiness()
        lag = self.loop_lag.last
        started = self.started_at
        runs = job_stats.snapshot()
        return InstanceSnapshot(
            runtime=InstanceRuntime(
                version=__version__,
                role=self.role,
                instance_id=self.instance_id,
                pid=os.getpid(),
                started_at=started,
                uptime_seconds=round((utc_now() - started).total_seconds(), 1) if started is not None else 0.0,
                loop_lag_max_ms=lag.max_ms if lag is not None else None,
                loop_lag_mean_ms=lag.mean_ms if lag is not None else None,
            ),
            ready=not reasons,
            reasons=reasons,
            tasks=[
                WorkerTask.of(task, runs.get(task.get_name()), worker_info(task.get_name()))
                for task in self.background_tasks
            ],
            streams=self._streams(),
            api=self.api.snapshot() if self.api is not None else None,
            gateway=self.gateway.snapshot() if self.gateway is not None else None,
            controller=self.controller.snapshot() if self.controller is not None else None,
            worker=self.worker.snapshot() if self.worker is not None else None,
        )

    def latencies(self) -> list[LatencyHistogram]:
        """The gateway's request latency histograms as they stand, for the metrics alone; none without a gateway."""
        return self.gateway.metering.latencies.snapshot() if self.gateway is not None else []

    @property
    def background_tasks(self) -> list[asyncio.Task[None]]:
        """Every loop of the process: the node's own and each runtime's."""
        handles = self.tasks.tasks
        for runtime in self._runtimes():
            handles.extend(runtime.background_tasks)
        return handles

    # --- Helpers ------------------------------------------------------------------------

    def _named_runtimes(self) -> list[tuple[str, RoleRuntime]]:
        """The role's runtimes with their names, in start order."""
        runtimes: list[tuple[str, RoleRuntime | None]] = [
            (Role.API.value, self.api),
            (Role.CONTROLLER.value, self.controller),
            (Role.WORKER.value, self.worker),
            (Role.GATEWAY.value, self.gateway),
        ]
        return [(name, runtime) for name, runtime in runtimes if runtime is not None]

    def _runtimes(self) -> list[RoleRuntime]:
        """The role's runtimes, in start order."""
        return [runtime for _, runtime in self._named_runtimes()]

    def _streams(self) -> dict[str, StreamState]:
        """Every stream the process follows, by name: each runtime's in start order, then the node's own loops'.

        So in role all the ``changes`` row is the worker's refresher's, and the controller shows its own in its section.
        """
        streams: dict[str, StreamState] = {}
        for runtime in self._runtimes():
            streams.update(runtime.streams())
        for loop in self._loops:
            streams.update(loop.streams())
        return streams

