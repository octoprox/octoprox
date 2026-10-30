# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the worker runtime: which loops it runs, what it reports, and the work it takes end to end.

It is built and started as the node does it, over the real Postgres and Redis; the work appended to its streams is read
back from Postgres, the job streams and the changes stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.job_stats import job_stats
from api.core.roles import JobKind
from api.core.workers import WorkerName, job_runner_name
from api.db.redis import SIGHTING_STREAM_KEY, RedisClient
from api.db.streams import ChangeLog
from api.geo.models import IpObservation, ObservationSource
from api.geo.observations import ObservationRecorder
from api.geo.runtime import GeoStack
from api.geo.updater import GeoDatabaseUpdater
from api.jobs.messages import SightingMessage
from api.models.proxy import ProxyStatus
from api.models.system import WorkerSnapshot
from api.providers.registry import ProviderRegistry
from api.providers.sdk.strategies import META_DISCOVERED_IP
from api.providers.store import ProviderStore
from api.roles.worker import WorkerRuntime
from tests.conftest import queued_job_keys
from tests.jobs.conftest import LONDON_IP, FakeUpstream, Rows, announced, geo_settings
from tests.waits import until

# Builds a runtime with the given settings overrides.
Build = Callable[..., Awaitable[WorkerRuntime]]


@pytest.fixture
async def build(
    tmp_path: Path,
    sessions: async_sessionmaker[AsyncSession],
    redis_client: RedisClient,
    registry: ProviderRegistry,
    change_log: ChangeLog,
) -> AsyncIterator[Build]:
    runtimes: list[WorkerRuntime] = []
    stacks: list[GeoStack] = []

    async def build(session_factory: Any = None, **overrides: Any) -> WorkerRuntime:
        values: dict[str, Any] = {
            "health_check_timeout": 2,
            "health_check_interval": 60,
            "health_check_concurrency": 5,
            "geo_lookup_enabled": False,
        }
        values.update(overrides)
        settings = geo_settings(tmp_path, **values)
        factory = session_factory or sessions
        stack = GeoStack.build(settings, session_factory=factory, redis_client=redis_client, blob_source=None)
        runtime = WorkerRuntime(
            settings,
            redis_client=redis_client,
            session_factory=factory,
            provider_store=ProviderStore(registry, factory),
            geo=stack,
            changes=change_log,
        )
        runtimes.append(runtime)
        stacks.append(stack)
        return runtime

    yield build
    for runtime in runtimes:
        await runtime.stop()
    for stack in stacks:
        await stack.close()


async def start(runtime: WorkerRuntime) -> None:
    """Start the runtime as the node does: its refresher's first read before any loop starts."""
    await runtime.refresher.start()
    await runtime.start()


def _task_names(runtime: WorkerRuntime) -> list[str]:
    return sorted(task.get_name() for task in runtime.tasks.tasks)


class TestLoops:
    async def test_it_starts_and_stops_its_refresher_a_runner_per_kind_it_takes_and_both_ingests(
        self, build: Build
    ) -> None:
        runtime = await build(worker_jobs=[JobKind.SYNC, JobKind.HEALTH])
        assert list(runtime.runners) == [JobKind.SYNC, JobKind.HEALTH]

        await start(runtime)

        assert _task_names(runtime) == sorted([
            WorkerName.DEFINITIONS_REFRESHER, WorkerName.JOB_RUNNER_SYNC, WorkerName.JOB_RUNNER_HEALTH,
            WorkerName.SIGHTING_INGEST, WorkerName.OBSERVATION_INGEST,
        ])
        assert not any(task.done() for task in runtime.tasks.tasks)
        await runtime.stop()
        assert runtime.tasks.tasks == []
        assert all(loop.stopped for loop in runtime.loops())

    async def test_by_default_it_runs_a_runner_for_every_job_kind(self, build: Build) -> None:
        runtime = await build()

        await start(runtime)

        assert list(runtime.runners) == list(JobKind)
        assert {name for name in _task_names(runtime) if name.startswith("job_runner_")} == {
            job_runner_name(kind) for kind in JobKind
        }

    async def test_a_worker_that_takes_no_job_kind_runs_the_refresher_and_the_ingests(self, build: Build) -> None:
        runtime = await build(worker_jobs=[])

        await start(runtime)

        assert _task_names(runtime) == sorted([
            WorkerName.DEFINITIONS_REFRESHER, WorkerName.SIGHTING_INGEST, WorkerName.OBSERVATION_INGEST,
        ])

    async def test_stop_never_raises_and_can_be_called_twice(self, build: Build) -> None:
        runtime = await build(worker_jobs=[JobKind.ENRICH])
        await start(runtime)

        await runtime.stop()
        await runtime.stop()

        assert runtime.readiness() == "worker not started"

    async def test_it_follows_the_changes_stream_alone_from_a_position_of_its_own(self, build: Build) -> None:
        """The job and work streams are read through the workers' group, whose position is the group's."""
        runtime = await build(worker_jobs=[JobKind.HEALTH])

        await start(runtime)

        streams = runtime.streams()
        assert set(streams) == {"changes"} and streams["changes"].full_reads == {"start": 1}


class TestReadiness:
    async def test_a_started_worker_with_its_definitions_read_is_ready(self, build: Build) -> None:
        runtime = await build(worker_jobs=[])
        assert runtime.readiness() == "worker not started"

        await start(runtime)

        assert runtime.readiness() is None

    async def test_a_worker_whose_definitions_were_never_read_is_not_ready(self, build: Build) -> None:
        """Postgres down from the start: its runs would judge with nothing."""

        def down() -> Any:
            raise OSError("connection refused")

        runtime = await build(session_factory=down, worker_jobs=[])
        with pytest.raises(OSError):
            await runtime.refresher.start()
        await runtime.start()

        assert runtime.readiness() == "definitions not loaded yet"


class TestWork:
    async def test_a_published_health_check_runs_its_write_is_announced_and_its_counters_reach_the_snapshot(
        self, build: Build, rows: Rows, upstream: FakeUpstream, redis_client: RedisClient
    ) -> None:
        job_stats.reset()
        upstream.answer({"ip": LONDON_IP})
        project = await rows.project()
        proxy = await rows.proxy(await rows.connector(project), port=upstream.port)
        runtime = await build(worker_jobs=[JobKind.HEALTH])
        await start(runtime)

        await runtime.jobs.publish(JobKind.HEALTH, [proxy.id])

        await until(lambda: runtime.snapshot().jobs[JobKind.HEALTH].runs == 1)
        stored = await rows.get(proxy.id)
        assert stored is not None and stored.status == ProxyStatus.HEALTHY
        snapshot = runtime.snapshot()
        assert set(snapshot.jobs) == {JobKind.HEALTH}
        health = snapshot.jobs[JobKind.HEALTH]
        assert (health.runs, health.failures, health.in_flight) == (1, 0, 0) and health.duration_seconds > 0
        # The status the check moved and the exit attribution wrote, each announced on the process's change log.
        assert await announced(redis_client) == [("proxy", proxy.id), ("proxy", proxy.id)]
        # The snapshot travels as JSON in the heartbeat.
        assert WorkerSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot

    async def test_a_kind_s_failure_streak_reaches_the_snapshot_and_ends_when_its_loop_recovers(
        self, build: Build
    ) -> None:
        """The System page colours a kind on its streak, so a quiet kind that failed in an outage is not failing for good."""
        job_stats.reset()
        runtime = await build(worker_jobs=[JobKind.PROVISION])
        name = runtime.runners[JobKind.PROVISION].name
        job_stats.record(name, 1.0, "ConnectionError: Connection refused")
        job_stats.record(name, 1.0, "TimeoutError: Timeout connecting to server")

        failing = runtime.snapshot().jobs[JobKind.PROVISION]
        job_stats.recovered(name)
        recovered = runtime.snapshot().jobs[JobKind.PROVISION]

        assert (failing.failures, failing.consecutive_failures) == (2, 2)
        assert (recovered.failures, recovered.consecutive_failures) == (2, 0)

    async def test_appended_sightings_are_attributed_and_counted(
        self, build: Build, rows: Rows, redis_client: RedisClient
    ) -> None:
        project = await rows.project()
        proxy = await rows.proxy(await rows.connector(project))
        runtime = await build(worker_jobs=[])
        await start(runtime)
        message = SightingMessage(
            proxy_id=proxy.id, ip=LONDON_IP, source=ObservationSource.PREFLIGHT, candidates=[],
            observed_at=datetime(2026, 10, 18, 12, 0),
        )

        await redis_client.client.xadd(SIGHTING_STREAM_KEY, {"item": message.model_dump_json()})

        await until(lambda: runtime.snapshot().sightings_consumed == 1)
        stored = await rows.get(proxy.id)
        assert stored is not None and stored.metadata.get(META_DISCOVERED_IP) == LONDON_IP

    async def test_published_observations_are_ingested_and_counted(
        self, build: Build, rows: Rows, redis_client: RedisClient
    ) -> None:
        project = await rows.project()
        proxy = await rows.proxy(await rows.connector(project))
        runtime = await build(worker_jobs=[])
        await start(runtime)
        recorder = ObservationRecorder(redis_client)

        recorder.record(IpObservation(proxy_id=proxy.id, ip=LONDON_IP, source=ObservationSource.HEALTH_CHECK))
        await recorder.flush()

        await until(lambda: runtime.snapshot().observations_consumed == 1)

    async def test_a_new_ip_database_file_asks_for_every_proxy_to_be_re_attributed(
        self, build: Build, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[GeoDatabaseUpdater] = []
        original = GeoStack.updater

        def capturing(stack: GeoStack, *args: Any, **kwargs: Any) -> GeoDatabaseUpdater:
            updater = original(stack, *args, **kwargs)
            built.append(updater)
            return updater

        monkeypatch.setattr(GeoStack, "updater", capturing)
        await build(worker_jobs=[])

        (updater,) = built
        assert updater._after_update is not None
        await updater._after_update()

        assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == ["all"]
