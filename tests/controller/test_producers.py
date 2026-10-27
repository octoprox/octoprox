# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the controller's job producers and the job keys they publish, against the real Redis test container.

A producer is driven a cycle a second on a clock the test moves (the ``time`` its module reads), and run as its runtime
runs it where the lease is the point.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Iterable

import pytest

from api.controller import producers
from api.controller.producers import (
    CATCH_UP_SECONDS,
    INSTALL_WIDE_KEYS,
    JobKeys,
    JobProducer,
    stable_jitter,
)
from api.core.leadership import Lease
from api.core.roles import JobKind
from api.core.workers import LeaseName
from api.db.redis import LEASE_KEY, RedisClient
from api.feed.models import ConnectorSection
from api.jobs.queue import JobQueue
from api.models.connector import Connector
from api.models.proxy import Proxy, ProxyStatus
from api.providers.registry import ProviderRegistry
from api.providers.sdk.strategies import META_DYNAMIC_SESSIONS
from tests.conftest import Clock, queued_job_keys
from tests.waits import running, until

HOUR = 3600


class Recorder(JobQueue):
    """The job queue, recording each publish; it refuses the next ``refuse`` of them as Redis would."""

    def __init__(self, redis_client: RedisClient) -> None:
        super().__init__(redis_client)
        self.calls: list[tuple[JobKind, list[str]]] = []
        self.refuse = 0

    async def publish(self, kind: JobKind, keys: Iterable[str]) -> int:
        if self.refuse:
            self.refuse -= 1
            raise ConnectionError("Redis refused the entries")
        listed = list(keys)
        self.calls.append((kind, listed))
        return await super().publish(kind, listed)


class Compiles:
    """The feed builder as a producer reads it: the job keys of its last compile, which a test compiles anew."""

    def __init__(self, **keys: frozenset[str]) -> None:
        self.job_keys = JobKeys(**keys)

    def compile(self, **keys: frozenset[str]) -> None:
        """A new compile, making keys of its own."""
        self.job_keys = JobKeys(**keys)


@pytest.fixture
def jobs(redis_client: RedisClient) -> Recorder:
    return Recorder(redis_client)


@pytest.fixture
def lease(redis_client: RedisClient) -> Lease:
    """The controller lease, never kept: a producer driven a cycle at a time does not ask it."""
    return Lease(redis_client, name=LeaseName.CONTROLLER, owner_id="controller-1")


def proxy_keys(count: int) -> frozenset[str]:
    return frozenset(f"proxy-{n}" for n in range(count))


async def cycles(producer: JobProducer, clock: Clock, jobs: Recorder, seconds: int) -> dict[int, list[str]]:
    """A cycle at each of the next ``seconds`` seconds of the clock; the keys published at each second that had any."""
    published: dict[int, list[str]] = {}
    for _ in range(seconds):
        before = len(jobs.calls)
        await producer._cycle()
        keys = [key for _kind, called in jobs.calls[before:] for key in called]
        if keys:
            published[int(clock.now)] = keys
        clock.now += 1
    return published


def slice_of(producer: JobProducer, keys: Collection[str], second: int) -> list[str]:
    """The keys published at ``second``: those whose slot it is, in key order."""
    return sorted(key for key in keys if producer.slot(key) == second % producer.period)


def section(connector_id: str, credential_type: str, *proxies: Proxy, **fields: object) -> ConnectorSection:
    connector = Connector(
        id=connector_id,
        name=connector_id,
        credential_id=f"credential-{connector_id}",
        credential_type=credential_type,
        project_id="project-1",
        **fields,  # type: ignore[arg-type]
    )
    return ConnectorSection(connector=connector, proxies=list(proxies))


def proxy(proxy_id: str, connector_id: str, status: ProxyStatus = ProxyStatus.HEALTHY, **metadata: str) -> Proxy:
    return Proxy(id=proxy_id, host="10.0.0.1", port=8080, connector_id=connector_id, status=status, metadata=metadata)


class TestStableJitter:
    def test_the_jitter_is_the_same_on_every_process(self) -> None:
        # A digest, not the per-process salted hash(): every controller places a key alike.
        assert stable_jitter("proxy-1") == pytest.approx(0.4659061451357437)
        assert stable_jitter("proxy-1") != stable_jitter("proxy-2")
        assert all(0 <= stable_jitter(f"proxy-{n}") < 1 for n in range(100))


class TestJobKeys:
    def test_each_kind_takes_the_keys_its_jobs_run_for(self, registry: ProviderRegistry) -> None:
        connectors = {
            "static": section(
                "static",
                "static_proxy_provider",
                proxy("healthy", "static"),
                proxy("unknown", "static", ProxyStatus.UNKNOWN),
                proxy("unhealthy", "static", ProxyStatus.UNHEALTHY),
                proxy("draining", "static", ProxyStatus.DRAINING),
                proxy("terminating", "static", ProxyStatus.TERMINATING),
            ),
            "disabled": section("disabled", "static_proxy_provider", proxy("off", "disabled"), enabled=False),
            "pool": section("pool", "oxylabs", proxy("slot", "pool")),
            "dynamic": section(
                "dynamic", "oxylabs", proxy("gateway-row", "dynamic", **{META_DYNAMIC_SESSIONS: "true"})
            ),
            "paused-pool": section("paused-pool", "oxylabs", proxy("paused-slot", "paused-pool"), enabled=False),
            "cloud": section("cloud", "aws", proxy("instance", "cloud", ProxyStatus.INITIALIZING)),
            "deleted-cloud": section(
                "deleted-cloud",
                "aws",
                proxy("leaving", "deleted-cloud", ProxyStatus.TERMINATING),
                enabled=False,
                pending_deletion=True,
            ),
        }
        keys = JobKeys.of(connectors, registry)
        # Not leaving the pool, of an enabled connector, and with an exit of its own to check.
        assert sorted(keys.health) == ["healthy", "instance", "slot", "unhealthy", "unknown"]
        # Enabled connectors whose provider syncs: a dynamic-sessions pool still syncs.
        assert sorted(keys.sync) == ["dynamic", "pool"]
        # Every cloud connector: a disabled or deleted one is drained by its scale job.
        assert sorted(keys.scale) == ["cloud", "deleted-cloud"]

    def test_health_sync_and_scale_take_the_compiles_keys_and_every_other_kind_the_installs(
        self, registry: ProviderRegistry
    ) -> None:
        keys = JobKeys.of({"cloud": section("cloud", "aws", proxy("instance", "cloud"))}, registry)
        assert keys.of_kind(JobKind.HEALTH) == frozenset({"instance"})
        assert keys.of_kind(JobKind.SYNC) == frozenset()
        assert keys.of_kind(JobKind.SCALE) == frozenset({"cloud"})
        others = (
            JobKind.METRICS_FLUSH, JobKind.COMPACTION, JobKind.SNAPSHOT, JobKind.GEO_UPDATE, JobKind.INDEX_HYGIENE,
            JobKind.UPDATE_CHECK,
        )
        assert all(keys.of_kind(kind) == INSTALL_WIDE_KEYS == ("all",) for kind in (*others, JobKind.TUNNEL_LAST_SEEN))
        # Before the first compile: none of a compile's, the install's all the same.
        assert JobKeys().of_kind(JobKind.HEALTH) == frozenset()
        assert JobKeys().of_kind(JobKind.METRICS_FLUSH) == INSTALL_WIDE_KEYS


class TestSlots:
    def test_the_period_is_whole_seconds_and_at_least_one(self, jobs: Recorder, lease: Lease) -> None:
        def period(seconds: float) -> int:
            return JobProducer(jobs, JobKind.HEALTH, Compiles(), period=seconds, lease=lease).period

        assert (period(0.4), period(2.6), period(3600.0)) == (1, 3, HOUR)

    def test_a_producer_is_named_for_its_kind_and_cycles_every_second(self, jobs: Recorder, lease: Lease) -> None:
        producer = JobProducer(jobs, JobKind.SYNC, Compiles(), period=HOUR, lease=lease)
        assert (producer.name, producer.interval, producer.first_at_once) == ("job_producer_sync", 1.0, True)
        # A kind only ever asked for has no producer.
        with pytest.raises(ValueError):
            JobProducer(jobs, JobKind.PROVISION, Compiles(), period=HOUR, lease=lease)

    def test_a_keys_slot_is_its_stable_hash_in_the_period_named_by_kind(self, jobs: Recorder, lease: Lease) -> None:
        health = JobProducer(jobs, JobKind.HEALTH, Compiles(), period=60, lease=lease)
        sync = JobProducer(jobs, JobKind.SYNC, Compiles(), period=60, lease=lease)
        assert health.slot("key-1") == int(stable_jitter("health:key-1") * 60)
        assert {health.slot(f"key-{n}") for n in range(50)} != {sync.slot(f"key-{n}") for n in range(50)}


class TestPublishing:
    async def test_each_key_is_published_once_a_period_at_its_second_a_slice_a_second(
        self, clock: Clock, jobs: Recorder, lease: Lease, redis_client: RedisClient
    ) -> None:
        keys = proxy_keys(300)
        producer = JobProducer(jobs, JobKind.HEALTH, Compiles(health=keys), period=60, lease=lease)
        await producer._on_term(1)

        published = await cycles(producer, clock, jobs, 120)

        # Each second publishes its slice, and only its slice.
        assert all(slice_of(producer, keys, second) == got for second, got in published.items())
        times: dict[str, list[int]] = {}
        for second, got in published.items():
            for key in got:
                times.setdefault(key, []).append(second)
        # Every key twice in two periods, a period apart, at its own second.
        assert set(times) == keys
        assert all(len(seconds) == 2 and seconds[1] - seconds[0] == 60 for seconds in times.values())
        # Spread over the period rather than all at once.
        assert len({second % 60 for second in published}) > 40
        assert max(len(got) for got in published.values()) < 20
        # What was published is on the kind's stream.
        assert sorted(await queued_job_keys(redis_client, JobKind.HEALTH)) == sorted(list(keys) * 2)

    async def test_the_keys_are_the_latest_compiles(self, clock: Clock, jobs: Recorder, lease: Lease) -> None:
        compiles = Compiles(health=proxy_keys(100))
        producer = JobProducer(jobs, JobKind.HEALTH, compiles, period=60, lease=lease)
        await producer._on_term(1)
        first = await cycles(producer, clock, jobs, 60)
        assert {key for got in first.values() for key in got} == proxy_keys(100)

        # A compile drops half the proxies and adds others.
        compiles.compile(health=frozenset(f"proxy-{n}" for n in range(50, 150)))
        second = await cycles(producer, clock, jobs, 60)
        assert {key for got in second.values() for key in got} == compiles.job_keys.health

    async def test_nothing_is_published_while_there_are_no_keys_to_go_by(
        self, clock: Clock, jobs: Recorder, lease: Lease
    ) -> None:
        compiles = Compiles()
        producer = JobProducer(jobs, JobKind.HEALTH, compiles, period=10, lease=lease)
        await producer._on_term(1)
        # Before the first compile.
        assert await cycles(producer, clock, jobs, 30) == {}

        # The first compile: from the current second on, with no burst of the seconds before it.
        compiles.compile(health=proxy_keys(20))
        first = int(clock.now)
        published = await cycles(producer, clock, jobs, 10)
        assert {key for got in published.values() for key in got} == proxy_keys(20)
        assert published.get(first, []) == slice_of(producer, proxy_keys(20), first)

        # A compile with no keys of the kind: nothing is published.
        compiles.compile(sync=frozenset({"connector-1"}))
        assert await cycles(producer, clock, jobs, 30) == {}

    async def test_after_a_stall_at_most_five_seconds_of_slots_are_published(
        self, clock: Clock, jobs: Recorder, lease: Lease
    ) -> None:
        """A whole period at once after a stall would ask KEDA for workers the next second does not need."""
        keys = proxy_keys(600)
        producer = JobProducer(jobs, JobKind.HEALTH, Compiles(health=keys), period=60, lease=lease)
        await producer._on_term(1)
        await cycles(producer, clock, jobs, 1)

        # The controller's loop stalls for a minute.
        clock.now += 59
        published = await cycles(producer, clock, jobs, 1)

        now = int(clock.now) - 1
        seconds = range(now - CATCH_UP_SECONDS + 1, now + 1)
        caught_up = [key for second in seconds for key in slice_of(producer, keys, second)]
        assert published == {now: caught_up}

    async def test_a_publish_redis_refused_is_sent_by_the_next_cycle(
        self, clock: Clock, jobs: Recorder, lease: Lease
    ) -> None:
        keys = proxy_keys(300)
        producer = JobProducer(jobs, JobKind.HEALTH, Compiles(health=keys), period=60, lease=lease)
        await producer._on_term(1)
        await cycles(producer, clock, jobs, 1)
        refused_at = int(clock.now)

        jobs.refuse = 1
        with pytest.raises(ConnectionError):
            await producer._cycle()
        clock.now += 1
        published = await cycles(producer, clock, jobs, 1)

        both = slice_of(producer, keys, refused_at) + slice_of(producer, keys, refused_at + 1)
        assert published == {refused_at + 1: both}

    async def test_a_new_lead_starts_at_the_current_second_with_no_burst(
        self, clock: Clock, jobs: Recorder, lease: Lease
    ) -> None:
        keys = proxy_keys(600)
        producer = JobProducer(jobs, JobKind.HEALTH, Compiles(health=keys), period=60, lease=lease)
        await producer._on_term(1)
        await cycles(producer, clock, jobs, 3)

        # Another controller leads for half a minute, then this one leads again.
        clock.now += 30
        await producer._on_term(2)
        published = await cycles(producer, clock, jobs, 1)

        now = int(clock.now) - 1
        assert published == {now: slice_of(producer, keys, now)}

    async def test_an_install_wide_kind_publishes_all_once_a_period(
        self, clock: Clock, jobs: Recorder, lease: Lease
    ) -> None:
        producer = JobProducer(jobs, JobKind.METRICS_FLUSH, Compiles(), period=60, lease=lease)
        await producer._on_term(1)

        published = await cycles(producer, clock, jobs, 180)

        assert list(published.values()) == [["all"]] * 3
        assert all(second % 60 == producer.slot("all") for second in published)
        # A cycle that published nothing is an idle one.
        clock.now += 60 - (int(clock.now) - producer.slot("all")) % 60
        assert await producer._cycle() is True
        clock.now += 1
        assert await producer._cycle() is False

    async def test_the_slot_map_is_made_once_per_compile(
        self, clock: Clock, jobs: Recorder, lease: Lease, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A large pool's slots are hashed when a compile hands new keys over, not every second."""
        hashed: list[str] = []

        def counted(key: str) -> float:
            hashed.append(key)
            return stable_jitter(key)

        monkeypatch.setattr(producers, "stable_jitter", counted)
        compiles = Compiles(health=proxy_keys(50))
        producer = JobProducer(jobs, JobKind.HEALTH, compiles, period=60, lease=lease)
        await producer._on_term(1)
        await cycles(producer, clock, jobs, 30)
        assert len(hashed) == 50

        # Another compile, of the same proxies: its own keys, hashed again.
        compiles.compile(health=proxy_keys(50))
        await cycles(producer, clock, jobs, 30)
        assert len(hashed) == 100


class TestLeadChanges:
    async def test_compaction_runs_each_hour_from_the_first_whatever_the_lead_does(
        self, clock: Clock, jobs: Recorder, lease: Lease
    ) -> None:
        """A lead taken every ten minutes still compacts each hour, the first in the hour the producer was built."""
        producer = JobProducer(jobs, JobKind.COMPACTION, Compiles(), period=HOUR, lease=lease)
        built = int(clock.now)
        published: dict[int, list[str]] = {}
        for term in range(1, 25):
            await producer._on_term(term)
            published |= await cycles(producer, clock, jobs, 600)

        seconds = sorted(published)
        # Four hours of leads: the first compaction in the first hour, then once an hour, each at the slot second.
        assert seconds[0] < built + HOUR
        assert len(seconds) == 4
        assert [b - a for a, b in zip(seconds, seconds[1:], strict=False)] == [HOUR, HOUR, HOUR]
        assert all(second % HOUR == producer.slot("all") for second in seconds)

    async def test_metrics_flush_is_published_within_a_period_of_a_lead_change(
        self, clock: Clock, jobs: Recorder, lease: Lease
    ) -> None:
        producer = JobProducer(jobs, JobKind.METRICS_FLUSH, Compiles(), period=60, lease=lease)
        for term, standby in enumerate((7, 23, 41, 59), start=1):
            # Another controller led for a while; this one leads again, and the flush comes within the period.
            clock.now += standby
            await producer._on_term(term)
            published = await cycles(producer, clock, jobs, 60)
            assert list(published.values()) == [["all"]]


class TestLease:
    async def test_a_standby_publishes_nothing_and_a_leader_publishes_on_the_kinds_stream(
        self, redis_client: RedisClient
    ) -> None:
        await redis_client.client.set(LEASE_KEY.format(name=LeaseName.CONTROLLER), "controller-2")
        lease = Lease(redis_client, name=LeaseName.CONTROLLER, owner_id="controller-1", poll_ms=50)
        lease.keep()
        producer = JobProducer(
            JobQueue(redis_client), JobKind.SYNC, Compiles(sync=frozenset({"a", "b"})), period=1, lease=lease
        )
        try:
            async with running(producer):
                await asyncio.sleep(1.2)
                assert await queued_job_keys(redis_client, JobKind.SYNC) == []

                # The other controller steps down: this one leads, and publishes each key every second.
                await redis_client.client.delete(LEASE_KEY.format(name=LeaseName.CONTROLLER))
                await until(lambda: lease.is_held)
                await asyncio.sleep(1.5)
                queued = await queued_job_keys(redis_client, JobKind.SYNC)
                assert queued.count("a") == queued.count("b") and 2 <= queued.count("a") <= 4
        finally:
            await lease.release()
