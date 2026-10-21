# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the controller runtime: the feed builder and a job producer per periodic kind, under one lease.

Each runtime is composed as the node composes it over the real Postgres and Redis test containers; two with different
instance IDs share them, as two controller pods share an install.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.controller.producers import INSTALL_WIDE_KEYS, JobProducer
from api.core import utc_now
from api.core.config import Settings
from api.core.leadership import LEASE_REPLY_MS
from api.core.roles import JobKind
from api.db.openvpn_repository import OpenVpnSettingsRepository
from api.db.redis import (
    CHANGES_STREAM_KEY,
    FEED_HEAD_KEY,
    LEASE_KEY,
    QUARANTINE_KEY,
    RUNTIME_STREAM_KEY,
    TRAFFIC_BLOCKED_KEY,
    RedisClient,
)
from api.db.streams import ChangeKind, ChangeLog, RuntimeSignal
from api.db.wireguard_repository import WireGuardSettingsRepository
from api.feed.models import GLOBAL_SECTION, GlobalSection
from api.geo.settings import GeoSettingsStore
from api.models.system import ControllerSnapshot
from api.providers.registry import build_registry
from api.providers.store import ProviderStore
from api.roles.controller import ControllerRuntime
from tests.conftest import Clock, queued_job_keys
from tests.controller.conftest import SECRET, START, Services, healthy_proxy
from tests.services.conftest import STATIC
from tests.waits import until

LEASE = LEASE_KEY.format(name="controller")
# Every periodic kind's producer, and the builder.
LOOPS = {
    "feed_builder",
    "job_producer_health",
    "job_producer_sync",
    "job_producer_scale",
    "job_producer_metrics_flush",
    "job_producer_compaction",
    "job_producer_snapshot",
    "job_producer_geo_update",
    "job_producer_tunnel_last_seen",
    "job_producer_index_hygiene",
}

# Builds a controller runtime for an instance ID, with any settings changed: build("controller-a", health_check_interval=1).
Build = Callable[..., ControllerRuntime]


@pytest.fixture
async def runtimes(
    sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient, test_settings: Settings
) -> AsyncIterator[Build]:
    """Builds a controller runtime per instance ID; every one is stopped when the test ends."""
    built: list[ControllerRuntime] = []

    def build(instance_id: str, **changed: Any) -> ControllerRuntime:
        settings = test_settings.model_copy(
            update={"instance_id": instance_id, "internal_secret": SECRET, "feed_compile_interval": 0.05, **changed}
        )
        registry = build_registry(include_plugins=False)
        runtime = ControllerRuntime(
            settings,
            session_factory=sessions,
            redis_client=redis_client,
            provider_store=ProviderStore(registry, sessions),
            geo_settings=GeoSettingsStore(settings, sessions),
        )
        built.append(runtime)
        return runtime

    yield build
    for runtime in built:
        await runtime.stop()


def producers_of(runtime: ControllerRuntime) -> dict[JobKind, JobProducer]:
    return {producer.kind: producer for producer in runtime.producers}


class TestLeadership:
    async def test_one_controller_leads_the_other_stands_by_ready_and_a_stop_hands_over(
        self, runtimes: Build, redis_client: RedisClient
    ) -> None:
        first, second = runtimes("controller-a"), runtimes("controller-b")
        await first.start()
        await until(lambda: first.lease.is_held)
        await until(lambda: first.builder.publishes > 0)
        assert await redis_client.client.exists(FEED_HEAD_KEY)

        await second.start()
        assert second.readiness() is None and not second.snapshot().is_leader
        leader = first.snapshot()
        assert (leader.is_leader, leader.lease_terms) == (True, 1)
        assert leader.changes is not None and leader.publishes >= 1 and leader.sections >= 1
        assert first.readiness() is None

        await first.stop()
        assert not first.lease.is_held
        # The released lease is taken within a poll, and the new leader publishes.
        took = await until(lambda: second.lease.is_held, timeout=3)
        assert took < 2.5
        await until(lambda: second.builder.publishes > 0)
        assert await redis_client.client.get(LEASE) == "controller-b"

        await second.stop()
        assert not await redis_client.client.exists(LEASE)

    async def test_stopping_twice_never_raises(self, runtimes: Build) -> None:
        runtime = runtimes("controller-a")
        await runtime.start()
        await runtime.stop()
        await runtime.stop()

    async def test_a_fresh_install_gets_no_tunnel_settings_from_the_controller(
        self, runtimes: Build, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        """The api seeds them, the one role that holds the tunnel defaults."""
        runtime = runtimes("controller-a")
        await runtime.start()
        await until(lambda: runtime.builder.publishes > 0)
        async with sessions() as session:
            assert await WireGuardSettingsRepository(session).get() is None
            assert await OpenVpnSettingsRepository(session).get() is None
        global_section = runtime.builder.sections[GLOBAL_SECTION]
        assert isinstance(global_section, GlobalSection)
        assert (global_section.wireguard, global_section.openvpn) == (None, None)
        assert await redis_client.client.xlen(CHANGES_STREAM_KEY) == 0


class TestLoops:
    async def test_the_builder_and_a_producer_per_periodic_kind_run_as_tasks_named_for_them(
        self, runtimes: Build
    ) -> None:
        runtime = runtimes(
            "controller-a",
            health_check_interval=45,
            ip_refresh_interval=1800,
            metrics_flush_interval=50,
            system_metrics_interval=120,
            geo_updater_interval=7200.0,
        )
        await runtime.start()
        assert {task.get_name() for task in runtime.tasks.tasks} == LOOPS
        assert {kind: producer.period for kind, producer in producers_of(runtime).items()} == {
            JobKind.HEALTH: 45,
            JobKind.SYNC: 1800,
            JobKind.SCALE: 30,
            JobKind.METRICS_FLUSH: 50,
            JobKind.COMPACTION: 3600,
            JobKind.SNAPSHOT: 120,
            JobKind.GEO_UPDATE: 7200,
            JobKind.TUNNEL_LAST_SEEN: 10,
            JobKind.INDEX_HYGIENE: 60,
        }

    def test_no_snapshots_are_published_while_they_are_off(self, runtimes: Build) -> None:
        runtime = runtimes("controller-a", system_metrics_interval=0)
        assert JobKind.SNAPSHOT not in producers_of(runtime)
        assert {loop.name for loop in runtime.loops()} == LOOPS - {"job_producer_snapshot"}

    def test_a_period_a_setting_gives_in_fractions_is_rounded_to_whole_seconds(self, runtimes: Build) -> None:
        runtime = runtimes("controller-a", geo_updater_interval=90.6)
        assert producers_of(runtime)[JobKind.GEO_UPDATE].period == 91

    async def test_compaction_is_published_at_its_slot_in_its_first_period(
        self, runtimes: Build, clock: Clock, redis_client: RedisClient
    ) -> None:
        """Nothing holds the first compaction back: it comes at its slot second in the period the runtime was built in."""
        # Built at START, the start of an hour.
        clock.now = START
        producer = producers_of(runtimes("controller-a"))[JobKind.COMPACTION]
        await producer._on_term(1)

        clock.now = START + producer.slot("all")
        assert await producer._cycle() is True
        assert await queued_job_keys(redis_client, JobKind.COMPACTION) == ["all"]

    async def test_every_producer_takes_its_keys_from_the_builders_last_compile(
        self, runtimes: Build, services: Services
    ) -> None:
        project = await services.project()
        proxy = await healthy_proxy(services, await services.connect(project, "ISP", STATIC), "10.0.0.1")
        runtime = runtimes("controller-a", system_metrics_interval=300)
        assert all(producer._builder is runtime.builder for producer in producers_of(runtime).values())
        # Nothing of a compile's before the first, but an install-wide kind needs none.
        assert runtime.builder.job_keys.of_kind(JobKind.HEALTH) == frozenset()
        assert runtime.builder.job_keys.of_kind(JobKind.METRICS_FLUSH) == INSTALL_WIDE_KEYS

        await runtime.start()
        await until(lambda: runtime.builder.compiles > 0)
        assert runtime.builder.job_keys.of_kind(JobKind.HEALTH) == {proxy.id}

    async def test_a_leader_publishes_the_periodic_jobs_on_their_streams(
        self, runtimes: Build, services: Services, redis_client: RedisClient
    ) -> None:
        project = await services.project()
        proxy = await healthy_proxy(services, await services.connect(project, "ISP", STATIC), "10.0.0.1")
        runtime = runtimes("controller-a", health_check_interval=1, metrics_flush_interval=1)
        await runtime.start()

        async def published() -> bool:
            expected = ((JobKind.HEALTH, proxy.id), (JobKind.METRICS_FLUSH, "all"))
            return all([key in await queued_job_keys(redis_client, kind) for kind, key in expected])

        await until(published, timeout=5)



class TestTheFeed:
    async def test_a_stored_strategy_no_router_has_is_served_as_the_default_strategy_setting(
        self, runtimes: Build, services: Services, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        project = await services.project()
        async with sessions() as session:
            # Saved before names were checked, or since removed: past the check a save makes.
            await session.execute(
                text("UPDATE projects SET routing_strategy = 'fastest' WHERE id = :id"), {"id": project.id}
            )
            await session.commit()
        runtime = runtimes("controller-a", default_strategy="health_based")
        await runtime.start()
        await until(lambda: runtime.builder.publishes > 0)

        global_section = runtime.builder.sections[GLOBAL_SECTION]
        assert isinstance(global_section, GlobalSection)
        assert [served.routing_strategy for served in global_section.projects] == ["health_based"]


class TestStartAndStop:
    async def test_the_lease_is_kept_before_the_loops_start(
        self, runtimes: Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime = runtimes("controller-a")
        keep = runtime.lease.keep
        tasks_at_keep: list[int] = []

        def counted() -> None:
            tasks_at_keep.append(len(runtime.tasks.tasks))
            keep()

        monkeypatch.setattr(runtime.lease, "keep", counted)
        await runtime.start()
        assert tasks_at_keep == [0]

    async def test_the_lease_is_released_once_every_loop_has_stopped(
        self, runtimes: Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A standby must not lead while a producer of this process could still publish."""
        runtime = runtimes("controller-a")
        await runtime.start()
        await until(lambda: runtime.lease.is_held)
        release = runtime.lease.release
        tasks = runtime.tasks.tasks
        stopped: list[bool] = []

        async def released() -> None:
            stopped.append(len(tasks) == len(runtime.loops()) and all(task.done() for task in tasks))
            await release()

        monkeypatch.setattr(runtime.lease, "release", released)
        await runtime.stop()
        assert stopped == [True]

    def test_the_controller_lease_waits_ten_seconds_for_a_reply(self, runtimes: Build) -> None:
        """A compile that holds the event loop for seconds must not cut a refresh reply that arrived."""
        lease = runtimes("controller-a").lease
        assert (lease._reply_ms, lease._refresh_ms) == (LEASE_REPLY_MS, 2000) and LEASE_REPLY_MS == 10_000


class TestReadiness:
    async def test_a_leader_whose_feed_cannot_be_published_is_not_ready(
        self, runtimes: Build, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime = runtimes("controller-a")

        async def unreachable(*_: object, **__: object) -> None:
            raise ConnectionError("Redis rejected the write")

        monkeypatch.setattr(runtime.builder._publisher, "publish", unreachable)
        await runtime.start()
        await until(lambda: runtime.builder.publish_error is not None)
        assert runtime.readiness() == "routing feed not published"
        assert runtime.snapshot().publish_error == "Redis rejected the write"

    async def test_postgres_is_the_heartbeats_to_report_and_a_failing_compile_keeps_the_leader_ready(
        self, runtimes: Build, change_log: ChangeLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The process's heartbeat says "Postgres unreachable" for every role; the feed goes on meanwhile."""
        runtime = runtimes("controller-a")
        await runtime.start()
        await until(lambda: runtime.builder.publishes > 0)

        async def unreachable(**_: object) -> None:
            raise ConnectionError("Postgres is down")

        monkeypatch.setattr(runtime.builder._compiler, "compile", unreachable)
        await change_log.record(ChangeKind.ALL)
        await until(lambda: runtime.builder.compile_error is not None)
        assert runtime.readiness() is None
        assert runtime.snapshot().compile_error == "Postgres is down"

    async def test_a_leader_that_cannot_refresh_its_lease_reports_redis_and_keeps_its_term(
        self, runtimes: Build, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redis slow or away for a moment: unready while it lasts, and no new term (a full recompile) after it."""
        runtime = runtimes("controller-a")
        await runtime.start()
        await until(lambda: runtime.lease.is_held)

        async def unreachable(*_: object, **__: object) -> object:
            raise ConnectionError("Redis is down")

        monkeypatch.setattr(redis_client.client, "eval", unreachable)
        await until(lambda: runtime.readiness() is not None, timeout=5)
        assert runtime.readiness() == "Redis unreachable"
        monkeypatch.undo()
        await until(lambda: runtime.readiness() is None, timeout=5)
        assert runtime.lease.is_held and runtime.lease.term == 1


class TestSnapshot:
    async def test_the_snapshot_reports_the_builder_and_serialises(
        self, runtimes: Build, services: Services, redis_client: RedisClient
    ) -> None:
        await services.project()
        runtime = runtimes("controller-a")
        await runtime.start()
        await until(lambda: runtime.builder.publishes > 0)
        snapshot = runtime.snapshot()
        assert snapshot.changes == await redis_client.stream_head(CHANGES_STREAM_KEY)
        assert snapshot.compiles >= 1 and snapshot.publishes >= 1 and snapshot.sections >= 1
        assert ControllerSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot

    async def test_the_counts_are_what_the_feed_carries_and_blocks_still_in_their_period(
        self, runtimes: Build, services: Services, redis_client: RedisClient
    ) -> None:
        project = await services.project()
        limited = {"limit_bytes": 1000, "action": "block"}
        current = await services.connect(project, "Current", STATIC, traffic_config=limited)
        ended = await services.connect(project, "Ended", STATIC, traffic_config=limited)
        tripped = await healthy_proxy(services, current, "10.0.0.1")
        runtime = runtimes("controller-a")
        await runtime.start()
        await until(lambda: runtime.builder.publishes > 0)

        now = utc_now()
        await redis_client.client.hset(
            TRAFFIC_BLOCKED_KEY,
            mapping={current.id: (now + timedelta(days=1)).isoformat(), ended.id: (now - timedelta(minutes=1)).isoformat()},
        )
        # A quarantine of a proxy the feed holds, and one of a proxy it does not.
        await redis_client.client.zadd(QUARANTINE_KEY, {tripped.id: time.time() + 600, "gone": time.time() + 600})
        await redis_client.client.xadd(RUNTIME_STREAM_KEY, {"kind": RuntimeSignal.TRAFFIC.value, "ref": current.id})
        await until(lambda: runtime.snapshot().blocked_connectors > 0)
        snapshot = runtime.snapshot()
        assert (snapshot.blocked_connectors, snapshot.quarantined) == (1, 1)

    async def test_the_streams_the_builder_follows_are_reported(self, runtimes: Build) -> None:
        runtime = runtimes("controller-a")
        assert {name: state.position for name, state in runtime.streams().items()} == {
            "changes": None,
            "runtime": None,
        }
        await runtime.start()
        await until(lambda: runtime.builder.publishes > 0)
        streams = runtime.streams()
        assert streams["changes"].position == "0-0" and streams["changes"].full_reads == {"start": 1}
        assert streams["runtime"].position == "0-0"
