# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The routing feed end to end: a write in Postgres, the controller's feed builder, a gateway's follower and router.

The builder runs as its runtime runs it, and each test waits until it has caught up, then checks what reaches the
router, what each entry costs, the reads in full, and what a lost feed or lease does.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.controller.feed_builder import FeedBuilder
from api.controller.producers import JobKeys
from api.core import utc_now
from api.core.config import Settings
from api.core.job_stats import job_stats
from api.core.leadership import Lease
from api.core.roles import JobKind
from api.core.workers import LeaseName
from api.db import streams
from api.db.redis import (
    CHANGES_STREAM_KEY,
    FEED_HEAD_KEY,
    FEED_MANIFEST_KEY,
    FEED_SECTIONS_KEY,
    FEED_STREAM_KEY,
    JOB_STREAM_KEY,
    LEASE_KEY,
    QUARANTINE_KEY,
    RUNTIME_STREAM_KEY,
    RUNTIME_STREAM_MAXLEN,
    TRAFFIC_BLOCKED_KEY,
    RedisClient,
)
from api.db.repository import ProxyRepository
from api.db.streams import ChangeKind, ChangeLog, RuntimeSignal
from api.feed.codec import FeedCodec
from api.feed.compiler import FullFeedCompiler
from api.feed.models import RUNTIME_SECTION, FeedHead, RuntimeSection, connector_section
from api.feed.publisher import FeedPublisher
from api.geo.settings import GeoSettingsStore, defaults_from_config
from api.models.connector import Connector, ConnectorUpdate
from api.models.project import Project
from api.models.proxy import Proxy, ProxyCreate, ProxyStatus
from api.providers.registry import ProviderRegistry
from api.providers.store import ProviderStore
from api.services.proxies import StatusMove
from tests.conftest import queued_job_keys
from tests.controller.conftest import (
    OWNER,
    SECRET,
    Gateway,
    MakeBuilder,
    Services,
    caught_up,
    healthy_proxy,
)
from tests.services.conftest import STATIC
from tests.waits import running, until

# A connector that stops at a kilobyte.
LIMITED = {"limit_bytes": 1000, "action": "block"}


async def unannounced_proxy(sessions: async_sessionmaker[AsyncSession], connector: Connector, host: str) -> Proxy:
    """A working proxy written by a writer that announces nothing: manual SQL, an older release during an upgrade."""
    proxy = Proxy(host=host, port=8080, connector_id=connector.id, status=ProxyStatus.HEALTHY)
    async with sessions() as session:
        await ProxyRepository(session).create(proxy)
        await session.commit()
    return proxy


async def selected(gateway: Gateway, project: Project) -> str | None:
    proxy = await gateway.router.select_proxy_for_project(project.id)
    return proxy.id if proxy is not None else None


async def announce(redis_client: RedisClient, kind: str, ref: str) -> None:
    """A runtime entry of ``kind`` about ``ref``, with nothing changed in Redis."""
    await redis_client.client.xadd(
        RUNTIME_STREAM_KEY, {"kind": kind, "ref": ref}, maxlen=RUNTIME_STREAM_MAXLEN, approximate=True
    )


async def quarantine(redis_client: RedisClient, proxy_id: str, ends: float | None) -> None:
    """A quarantine tripped until ``ends`` (epoch seconds) or lifted (None), announced in the same MULTI."""
    pipe = redis_client.client.pipeline(transaction=True)
    if ends is None:
        pipe.zrem(QUARANTINE_KEY, proxy_id)
    else:
        pipe.zadd(QUARANTINE_KEY, {proxy_id: ends})
    pipe.xadd(
        RUNTIME_STREAM_KEY,
        {"kind": RuntimeSignal.QUARANTINE.value, "ref": proxy_id},
        maxlen=RUNTIME_STREAM_MAXLEN,
        approximate=True,
    )
    await pipe.execute()


async def block(redis_client: RedisClient, connector_id: str, until_: datetime | None) -> None:
    """A traffic block set until ``until_`` (naive UTC) or lifted (None), announced as the traffic job announces it."""
    pipe = redis_client.client.pipeline(transaction=True)
    if until_ is None:
        pipe.hdel(TRAFFIC_BLOCKED_KEY, connector_id)
    else:
        pipe.hset(TRAFFIC_BLOCKED_KEY, connector_id, until_.isoformat())
    pipe.xadd(
        RUNTIME_STREAM_KEY,
        {"kind": RuntimeSignal.TRAFFIC.value, "ref": connector_id},
        maxlen=RUNTIME_STREAM_MAXLEN,
        approximate=True,
    )
    await pipe.execute()


def runtime_of(builder: FeedBuilder) -> RuntimeSection:
    section = builder.sections[RUNTIME_SECTION]
    assert isinstance(section, RuntimeSection)
    return section


def statements_while(sessions: async_sessionmaker[AsyncSession]) -> tuple[list[str], Callable[[], None]]:
    """Every SQL statement the engine runs from now on, and a function that stops recording."""
    statements: list[str] = []
    engine = sessions.kw["bind"].sync_engine

    def record(_conn: object, _cursor: object, statement: str, *_: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return statements, lambda: event.remove(engine, "before_cursor_execute", record)


class TestWriteToRouter:
    async def test_a_proxy_written_through_the_service_is_routed_by_a_gateway(
        self, builder: FeedBuilder, services: Services, gateway: Gateway, redis_client: RedisClient
    ) -> None:
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC)
        proxy = await services.proxies.create(
            project.id, ProxyCreate(host="10.0.0.1", port=8080, connector_id=connector.id, username="u", password="p")
        )
        await caught_up(builder, redis_client)
        assert await gateway.follower.poll()
        # Nobody has checked it yet: no gateway routes to it.
        assert gateway.router.get_proxy(proxy.id) is not None
        assert await selected(gateway, project) is None

        # Its first health check finds it working (the health job's compare-and-set).
        assert await services.proxies.set_statuses_if({proxy.id: StatusMove(ProxyStatus.HEALTHY, ProxyStatus.UNKNOWN)})
        await caught_up(builder, redis_client)
        assert await gateway.follower.poll()
        assert await selected(gateway, project) == proxy.id
        # Only its connector's section travelled.
        assert set(gateway.updates[-1].changed) == {connector_section(connector.id)}

    async def test_a_write_that_announces_nothing_is_not_compiled(
        self,
        builder: FeedBuilder,
        services: Services,
        redis_client: RedisClient,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        """No poll of Postgres: the reconcile is the net for such writes."""
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC)
        await caught_up(builder, redis_client)
        head = await redis_client.client.get(FEED_HEAD_KEY)
        compiles, written = builder.compiles, builder.sections_written

        await unannounced_proxy(sessions, connector, "10.0.0.1")
        await asyncio.sleep(0.2)
        assert (builder.compiles, builder.sections_written) == (compiles, written)
        assert await redis_client.client.get(FEED_HEAD_KEY) == head

    async def test_a_deleted_connector_leaves_the_gateways(
        self, builder: FeedBuilder, services: Services, gateway: Gateway, redis_client: RedisClient
    ) -> None:
        project = await services.project()
        first = await services.connect(project, "First", STATIC)
        second = await services.connect(project, "Second", STATIC)
        await healthy_proxy(services, first, "10.0.0.1")
        kept = await healthy_proxy(services, second, "10.0.0.2")
        await caught_up(builder, redis_client)
        assert await gateway.follower.poll()

        await services.connectors.remove(project.id, first.id)
        await caught_up(builder, redis_client)
        assert await gateway.follower.poll()
        assert gateway.updates[-1].removed == {connector_section(first.id)}
        assert {await selected(gateway, project) for _ in range(4)} == {kept.id}

    async def test_a_publish_that_moves_the_head_announces_it_and_one_that_changes_nothing_does_not(
        self, builder: FeedBuilder, services: Services, redis_client: RedisClient, change_log: ChangeLog
    ) -> None:
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC)
        await caught_up(builder, redis_client)
        await healthy_proxy(services, connector, "10.0.0.1")
        await caught_up(builder, redis_client)
        entries = await redis_client.client.xrange(FEED_STREAM_KEY)
        head = FeedHead.model_validate_json(await redis_client.client.get(FEED_HEAD_KEY))
        assert [fields["digest"] for _id, fields in entries][-1] == head.digest
        announced = len(entries)

        # Anything may have changed (a backup import, say), and nothing did:
        # the compile writes nothing and wakes no gateway.
        compiles = builder.compiles
        await change_log.record(ChangeKind.ALL)
        await caught_up(builder, redis_client)
        assert builder.compiles == compiles + 1
        assert await redis_client.client.xlen(FEED_STREAM_KEY) == announced


class TestCoalescing:
    async def test_a_burst_of_changes_is_compiled_once_per_interval(
        self, make_builder: MakeBuilder, lease: Lease, change_log: ChangeLog, redis_client: RedisClient
    ) -> None:
        """A vendor outage flips thousands of proxies a batch at a time: compiles must not follow each entry."""
        interval = 0.3
        builder = make_builder(lease, compile_interval=interval)
        async with running(builder):
            await until(lambda: builder.publishes > 0)
            before = builder.compiles
            started = time.monotonic()
            for _ in range(40):
                await change_log.record(ChangeKind.PROXY)
                await asyncio.sleep(0.02)
            burst = time.monotonic() - started
            last = await redis_client.stream_head(CHANGES_STREAM_KEY)
            # Every entry is compiled in the end.
            await until(lambda: builder.compiled is not None and builder.compiled.changes == last)
            assert 1 <= builder.compiles - before <= math.ceil(burst / interval) + 1

    async def test_a_change_after_a_quiet_spell_is_compiled_at_once_and_one_right_after_waits_its_turn(
        self, make_builder: MakeBuilder, lease: Lease, change_log: ChangeLog
    ) -> None:
        interval = 0.5
        builder = make_builder(lease, compile_interval=interval)
        async with running(builder):
            await until(lambda: builder.publishes > 0)
            await asyncio.sleep(interval + 0.1)

            before = builder.compiles
            started = time.monotonic()
            await change_log.record(ChangeKind.PROXY)
            await until(lambda: builder.compiles > before)
            assert time.monotonic() - started < interval / 2

            before = builder.compiles
            started = time.monotonic()
            await change_log.record(ChangeKind.PROXY)
            await until(lambda: builder.compiles > before)
            assert time.monotonic() - started > interval / 2


class TestRuntimeSection:
    async def test_a_quarantine_tripped_moved_and_lifted_moves_only_the_runtime_section(
        self, builder: FeedBuilder, services: Services, gateway: Gateway, redis_client: RedisClient
    ) -> None:
        """A rate-limited pool under load trips every few seconds: gateways must not fetch the pool each time."""
        project = await services.project()
        first = await services.connect(project, "First", STATIC)
        second = await services.connect(project, "Second", STATIC)
        tripped = await healthy_proxy(services, first, "10.0.0.1")
        other = await healthy_proxy(services, second, "10.0.0.2")
        await caught_up(builder, redis_client)
        assert await gateway.follower.poll()
        ids = (first.id, second.id)
        definitions = {name: builder.sections[name] for name in map(connector_section, ids)}
        views = {cid: gateway.router.snapshot.connector_view(cid) for cid in ids}
        compiles = builder.compiles

        async def step() -> int:
            written = builder.sections_written
            await caught_up(builder, redis_client)
            assert await gateway.follower.poll()
            assert set(gateway.updates[-1].changed) == {RUNTIME_SECTION}
            return builder.sections_written - written

        # A gateway's rate limiter trips: the other gateways learn of it from the feed.
        ends = time.time() + 600
        await quarantine(redis_client, tripped.id, ends)
        assert await step() == 1
        assert {await selected(gateway, project) for _ in range(4)} == {other.id}
        # Tripped again for longer: the end moves.
        await quarantine(redis_client, tripped.id, ends + 600)
        assert await step() == 1
        moved = gateway.router.snapshot.quarantined[tripped.id]
        assert moved.replace(tzinfo=UTC).timestamp() == pytest.approx(ends + 600, abs=1)
        # Lifted by the api.
        await quarantine(redis_client, tripped.id, None)
        assert await step() == 1
        assert {await selected(gateway, project) for _ in range(4)} == {tripped.id, other.id}
        # Nothing was compiled: no connector's section was rebuilt, and the gateway reused every index.
        assert builder.compiles == compiles
        assert all(builder.sections[name] is section for name, section in definitions.items())
        assert all(gateway.router.snapshot.connector_view(cid) is view for cid, view in views.items())

    async def test_a_quarantine_is_read_when_announced_not_polled(
        self, builder: FeedBuilder, services: Services, redis_client: RedisClient
    ) -> None:
        project = await services.project()
        tripped = await healthy_proxy(services, await services.connect(project, "ISP", STATIC), "10.0.0.1")
        await caught_up(builder, redis_client)

        await redis_client.client.zadd(QUARANTINE_KEY, {tripped.id: time.time() + 600})
        await asyncio.sleep(0.2)
        assert runtime_of(builder).quarantined == {}

        await quarantine(redis_client, tripped.id, time.time() + 600)
        await caught_up(builder, redis_client)
        assert tripped.id in runtime_of(builder).quarantined

    async def test_ended_quarantines_are_left_in_redis_and_unknown_proxies_ignored(
        self, builder: FeedBuilder, services: Services, redis_client: RedisClient
    ) -> None:
        """The rebuild is a pure read: index hygiene trims what has ended."""
        project = await services.project()
        await healthy_proxy(services, await services.connect(project, "ISP", STATIC), "10.0.0.1")
        await caught_up(builder, redis_client)
        await redis_client.client.zadd(QUARANTINE_KEY, {"ended": time.time() - 1})
        written, published = builder.sections_written, builder.publishes

        await quarantine(redis_client, "unknown", time.time() + 600)
        await caught_up(builder, redis_client)
        assert await redis_client.client.zscore(QUARANTINE_KEY, "ended") is not None
        assert await redis_client.client.zscore(QUARANTINE_KEY, "unknown") is not None
        # The section came out the same: not even sent.
        assert (builder.sections_written, builder.publishes) == (written, published)

    async def test_a_traffic_block_set_and_lifted_moves_only_the_runtime_section(
        self, builder: FeedBuilder, services: Services, gateway: Gateway, redis_client: RedisClient
    ) -> None:
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC, traffic_config=LIMITED)
        proxy = await healthy_proxy(services, connector, "10.0.0.1")
        await caught_up(builder, redis_client)
        assert await gateway.follower.poll()
        assert await selected(gateway, project) == proxy.id
        compiles = builder.compiles

        # A worker's traffic decision blocked it until the end of its period.
        ends = (utc_now() + timedelta(days=3)).replace(microsecond=0)
        await block(redis_client, connector.id, ends)
        await caught_up(builder, redis_client)
        assert runtime_of(builder).traffic_blocked_until == {connector.id: ends}
        assert await gateway.follower.poll()
        assert set(gateway.updates[-1].changed) == {RUNTIME_SECTION}
        assert gateway.router.is_traffic_blocked(connector.id)
        assert await selected(gateway, project) is None

        # A raised limit lifted it.
        await block(redis_client, connector.id, None)
        await caught_up(builder, redis_client)
        assert await gateway.follower.poll()
        assert not gateway.router.is_traffic_blocked(connector.id)
        assert await selected(gateway, project) == proxy.id
        assert builder.compiles == compiles

    async def test_a_block_is_read_when_announced_not_polled(
        self, builder: FeedBuilder, services: Services, redis_client: RedisClient
    ) -> None:
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC, traffic_config=LIMITED)
        await caught_up(builder, redis_client)
        ends = (utc_now() + timedelta(days=3)).replace(microsecond=0)

        await redis_client.client.hset(TRAFFIC_BLOCKED_KEY, connector.id, ends.isoformat())
        await asyncio.sleep(0.2)
        assert runtime_of(builder).traffic_blocked_until == {}

        await announce(redis_client, RuntimeSignal.TRAFFIC.value, connector.id)
        await caught_up(builder, redis_client)
        assert runtime_of(builder).traffic_blocked_until == {connector.id: ends}

    async def test_the_rebuild_reads_no_postgres(
        self,
        builder: FeedBuilder,
        services: Services,
        redis_client: RedisClient,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC, traffic_config=LIMITED)
        tripped = await healthy_proxy(services, connector, "10.0.0.1")
        await caught_up(builder, redis_client)
        published = builder.publishes

        statements, stop = statements_while(sessions)
        try:
            await quarantine(redis_client, tripped.id, time.time() + 600)
            await block(redis_client, connector.id, utc_now() + timedelta(days=1))
            await caught_up(builder, redis_client)
        finally:
            stop()
        assert builder.publishes > published
        assert statements == []


class TestJobKeys:
    async def test_the_job_keys_follow_the_compile_and_only_the_compile(
        self,
        make_builder: MakeBuilder,
        lease: Lease,
        services: Services,
        redis_client: RedisClient,
    ) -> None:
        builder = make_builder(lease)
        # Nothing of a compile's to publish by before the first.
        assert builder.job_keys == JobKeys()
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC)
        first = await healthy_proxy(services, connector, "10.0.0.1")
        async with running(builder):
            await caught_up(builder, redis_client)
            keys = builder.job_keys
            assert keys is not None and keys.health == {first.id}

            second = await healthy_proxy(services, connector, "10.0.0.2")
            await caught_up(builder, redis_client)
            assert builder.job_keys is not None and builder.job_keys.health == {first.id, second.id}
            keys = builder.job_keys

            # A runtime rebuild makes no keys: the producers' slot maps stand.
            await quarantine(redis_client, first.id, time.time() + 600)
            await caught_up(builder, redis_client)
            assert builder.job_keys is keys

            await services.connectors.update(project.id, connector.id, ConnectorUpdate(enabled=False))
            await caught_up(builder, redis_client)
            assert builder.job_keys is not None and builder.job_keys.health == frozenset()


class TestReadsInFull:
    async def test_the_hourly_full_read_compiles_again_and_republishes_nothing(
        self, make_builder: MakeBuilder, lease: Lease, services: Services, redis_client: RedisClient
    ) -> None:
        builder = make_builder(lease, reconcile_interval=0.3)
        project = await services.project()
        await healthy_proxy(services, await services.connect(project, "ISP", STATIC), "10.0.0.1")
        async with running(builder):
            await caught_up(builder, redis_client)
            compiles, written = builder.compiles, builder.sections_written
            announced = await redis_client.client.xlen(FEED_STREAM_KEY)

            await until(lambda: builder.streams()["changes"].full_reads.get("reconcile", 0) >= 1, timeout=2)
            await caught_up(builder, redis_client)
            # One compile per reconcile, and nothing written or announced.
            assert builder.compiles - compiles == builder.streams()["changes"].full_reads["reconcile"]
            assert builder.sections_written == written
            assert await redis_client.client.xlen(FEED_STREAM_KEY) == announced

    async def test_the_reconcile_compiles_a_write_that_announced_nothing(
        self,
        make_builder: MakeBuilder,
        lease: Lease,
        services: Services,
        gateway: Gateway,
        redis_client: RedisClient,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        builder = make_builder(lease, reconcile_interval=0.5)
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC)
        async with running(builder):
            await caught_up(builder, redis_client)
            proxy = await unannounced_proxy(sessions, connector, "10.0.0.1")

            await until(lambda: builder.streams()["changes"].full_reads.get("reconcile", 0) >= 1, timeout=2)
            await caught_up(builder, redis_client)
            assert await gateway.follower.poll()
            assert await selected(gateway, project) == proxy.id

    async def test_a_gap_in_the_changes_stream_compiles_everything(
        self,
        builder: FeedBuilder,
        services: Services,
        redis_client: RedisClient,
        sessions: async_sessionmaker[AsyncSession],
        change_log: ChangeLog,
    ) -> None:
        """Entries the builder never read are lost to it; reading in full is what makes up for them."""
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC)
        await caught_up(builder, redis_client)
        proxy = await unannounced_proxy(sessions, connector, "10.0.0.1")
        # The stream went (a wipe of it, or trimmed and recreated), and an entry starts it again.
        await redis_client.client.delete(CHANGES_STREAM_KEY)
        await change_log.record(ChangeKind.PROJECT, project.id)

        await until(lambda: builder.streams()["changes"].full_reads.get("gap") == 1)
        await caught_up(builder, redis_client)
        compiled = builder.compiled
        assert compiled is not None and proxy.id in {p.id for p in compiled.connectors[connector.id].proxies}

    async def test_a_new_term_reads_everything_again_and_compiles_at_once(
        self, make_builder: MakeBuilder, lease: Lease, services: Services, redis_client: RedisClient
    ) -> None:
        """The compile interval of the lead before does not hold a new lead's first compile back."""
        builder = make_builder(lease, compile_interval=5.0)
        await services.project()
        async with running(builder):
            await caught_up(builder, redis_client)
            compiles = builder.compiles

            # The lease is lost and taken again: a new term.
            await lease.release()
            lease.keep()
            await until(lambda: lease.is_held and lease.term == 2)
            await until(lambda: builder.compiles > compiles, timeout=2)
            await caught_up(builder, redis_client)
            assert builder.compiles == compiles + 1
            assert builder.streams()["changes"].full_reads.get("start") == 2

    async def test_a_new_lead_reads_the_quarantines_and_blocks_no_entry_told_it_of(
        self,
        make_builder: MakeBuilder,
        lease: Lease,
        services: Services,
        redis_client: RedisClient,
    ) -> None:
        """Entries written before a lead took its positions are never read: the lead reads the state itself."""
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC, traffic_config=LIMITED)
        tripped = await healthy_proxy(services, connector, "10.0.0.1")
        ends = (utc_now() + timedelta(days=1)).replace(microsecond=0)
        await quarantine(redis_client, tripped.id, time.time() + 600)
        await block(redis_client, connector.id, ends)

        builder = make_builder(lease)
        async with running(builder):
            await caught_up(builder, redis_client)
            runtime = runtime_of(builder)
            assert tripped.id in runtime.quarantined
            assert runtime.traffic_blocked_until == {connector.id: ends}


class TestFeedLoss:
    async def test_a_wiped_head_asks_for_reattribute_all_and_nothing_for_traffic(
        self,
        builder: FeedBuilder,
        services: Services,
        gateway: Gateway,
        redis_client: RedisClient,
        change_log: ChangeLog,
    ) -> None:
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC, traffic_config=LIMITED)
        proxy = await healthy_proxy(services, connector, "10.0.0.1")
        await caught_up(builder, redis_client)
        assert await gateway.follower.poll()
        applied = gateway.follower.applied_head
        # The first publish found Redis empty, as after a wipe.
        assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == ["all"]
        for kind in (JobKind.REATTRIBUTE, JobKind.TRAFFIC):
            await redis_client.client.delete(JOB_STREAM_KEY.format(kind=kind))

        await redis_client.client.delete(FEED_HEAD_KEY)
        # The gateway keeps routing on what it applied.
        assert not await gateway.follower.poll()
        assert await selected(gateway, project) == proxy.id

        await change_log.record(ChangeKind.ALL)
        await caught_up(builder, redis_client)
        assert await redis_client.client.exists(FEED_HEAD_KEY)
        # One-shot jobs pending at a wipe are gone: every proxy is judged again.
        assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == ["all"]
        # Traffic blocks went with Redis too: metering asks again once a limited connector carries bytes.
        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == []
        # The same compile: the gateway has nothing to apply and goes on routing.
        assert not await gateway.follower.poll()
        assert gateway.follower.applied_head == applied
        assert await selected(gateway, project) == proxy.id

    async def test_reattribute_all_is_owed_until_redis_takes_it(
        self,
        builder: FeedBuilder,
        services: Services,
        redis_client: RedisClient,
        change_log: ChangeLog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A one-shot request lost to a Redis hiccup right after the wipe would be lost for good."""
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.05)
        await services.project()
        await caught_up(builder, redis_client)
        await redis_client.client.delete(JOB_STREAM_KEY.format(kind=JobKind.REATTRIBUTE), FEED_HEAD_KEY)
        publish = builder._jobs.publish
        refused: list[JobKind] = []

        async def once_refused(kind: JobKind, keys: Any) -> int:
            if not refused:
                refused.append(kind)
                raise ConnectionError("Redis went away again")
            return await publish(kind, keys)

        monkeypatch.setattr(builder._jobs, "publish", once_refused)
        await change_log.record(ChangeKind.ALL)
        await caught_up(builder, redis_client)
        assert refused == [JobKind.REATTRIBUTE]
        assert await redis_client.client.exists(FEED_HEAD_KEY)
        assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == ["all"]

    async def test_a_lost_section_is_written_again_at_the_next_publish(
        self, builder: FeedBuilder, services: Services, redis_client: RedisClient, change_log: ChangeLog
    ) -> None:
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC)
        await healthy_proxy(services, connector, "10.0.0.1")
        await caught_up(builder, redis_client)
        await redis_client.client.delete(JOB_STREAM_KEY.format(kind=JobKind.REATTRIBUTE))

        await redis_client.client.hdel(FEED_SECTIONS_KEY, connector_section(connector.id))
        await change_log.record(ChangeKind.ALL)
        await caught_up(builder, redis_client)
        assert await redis_client.client.hexists(FEED_SECTIONS_KEY, connector_section(connector.id))
        # Not a wipe: the head was there.
        assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == []

    async def test_after_a_flushdb_the_feed_is_republished_within_a_lease_refresh(
        self, make_builder: MakeBuilder, services: Services, gateway: Gateway, redis_client: RedisClient
    ) -> None:
        refresh_ms, poll_ms = 200, 50
        lease = Lease(
            redis_client, name=LeaseName.CONTROLLER, owner_id=OWNER, ttl_ms=600, refresh_ms=refresh_ms, poll_ms=poll_ms
        )
        lease.keep()
        try:
            builder = make_builder(lease)
            project = await services.project()
            proxy = await healthy_proxy(services, await services.connect(project, "ISP", STATIC), "10.0.0.1")
            await until(lambda: lease.is_held)
            async with running(builder):
                await caught_up(builder, redis_client)
                assert await gateway.follower.poll()
                published = builder.publishes

                await redis_client.client.flushdb()
                assert not await gateway.follower.poll()
                assert await selected(gateway, project) == proxy.id

                # The lease went with the wipe: the next refresh ends the lead, the
                # keeper takes it again as a new term, and the builder republishes.
                took = await until(lambda: lease.term == 2 and builder.publishes > published)
                assert took <= (refresh_ms + poll_ms) / 1000 + 1.0
                assert await redis_client.client.exists(FEED_HEAD_KEY)
                assert await redis_client.client.get(lease.key) == OWNER
                # Asked for right after that publish, in the same act.
                async def reattribution_asked() -> bool:
                    return await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == ["all"]

                await until(reattribution_asked, timeout=5)
                assert not await gateway.follower.poll()
                assert await selected(gateway, project) == proxy.id
        finally:
            await lease.release()


class TestFencing:
    async def test_a_stale_ex_leader_publishes_nothing_and_stops_leading_at_once(
        self, make_builder: MakeBuilder, services: Services, redis_client: RedisClient
    ) -> None:
        # A refresh a minute apart: only the fenced publish can end this lead within the test.
        lease = Lease(redis_client, name=LeaseName.CONTROLLER, owner_id=OWNER, ttl_ms=120_000, refresh_ms=60_000)
        lease.keep()
        builder = make_builder(lease)
        try:
            await until(lambda: lease.is_held)
            project = await services.project()
            connector = await services.connect(project, "ISP", STATIC)
            proxy = await services.proxies.create(
                project.id, ProxyCreate(host="10.0.0.1", port=8080, connector_id=connector.id)
            )
            async with running(builder):
                await caught_up(builder, redis_client)
                head = await redis_client.client.get(FEED_HEAD_KEY)
                manifest = await redis_client.client.hgetall(FEED_MANIFEST_KEY)
                announced = await redis_client.client.xlen(FEED_STREAM_KEY)

                # Its lease expired during a stall, and another controller took it.
                await redis_client.client.set(lease.key, "controller-2")
                assert await services.proxies.set_statuses_if({proxy.id: StatusMove(ProxyStatus.HEALTHY, ProxyStatus.UNKNOWN)})
                await until(lambda: not lease.is_held, timeout=2)

                assert await redis_client.client.get(FEED_HEAD_KEY) == head
                assert await redis_client.client.hgetall(FEED_MANIFEST_KEY) == manifest
                assert await redis_client.client.xlen(FEED_STREAM_KEY) == announced
                # Standing by from now on: nothing compiled, nothing written.
                compiles = builder.compiles
                await services.connect(project, "Second", STATIC)
                await asyncio.sleep(0.2)
                assert builder.compiles == compiles
                assert await redis_client.client.get(FEED_HEAD_KEY) == head
        finally:
            await lease.release()

    async def test_a_standby_does_nothing_until_it_leads(
        self, make_builder: MakeBuilder, services: Services, redis_client: RedisClient
    ) -> None:
        await redis_client.client.set(LEASE_KEY.format(name=LeaseName.CONTROLLER), "controller-2")
        lease = Lease(redis_client, name=LeaseName.CONTROLLER, owner_id=OWNER, poll_ms=50)
        lease.keep()
        standby = make_builder(lease)
        await services.connect(await services.project(), "ISP", STATIC)
        try:
            async with running(standby):
                await asyncio.sleep(0.3)
                assert standby.compiled is None and standby.job_keys == JobKeys()
                before = standby.job_keys
                assert not await redis_client.client.exists(FEED_HEAD_KEY)
                assert standby.streams()["changes"].position is None
                assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == []

                # The other controller stops: this one leads, and publishes.
                await redis_client.client.delete(LEASE_KEY.format(name=LeaseName.CONTROLLER))
                await until(lambda: standby.publishes > 0)
                # Its first compile made keys of its own.
                assert standby.job_keys is not before
        finally:
            await lease.release()

    async def test_a_new_term_never_publishes_the_last_terms_compile(
        self,
        builder: FeedBuilder,
        services: Services,
        redis_client: RedisClient,
        lease: Lease,
        sessions: async_sessionmaker[AsyncSession],
        registry: ProviderRegistry,
        test_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A process that leads again with Postgres away would roll the feed back to what it compiled before."""
        project = await services.project()
        first = await services.connect(project, "First", STATIC)
        tripped = await healthy_proxy(services, first, "10.0.0.1")
        await caught_up(builder, redis_client)
        # Another controller took the lease and published a newer feed, with a second connector.
        await redis_client.client.set(lease.key, "controller-2")
        await until(lambda: not lease.is_held)
        second = await services.connect(project, "Second", STATIC)
        # A quarantine meanwhile: the runtime section this process makes again on its old compile differs.
        await quarantine(redis_client, tripped.id, time.time() + 600)
        other = FullFeedCompiler(
            sessions,
            registry,
            descriptor_sink=ProviderStore(registry, sessions),
            geo_settings_sink=GeoSettingsStore(test_settings, sessions),
            geo_defaults=defaults_from_config(test_settings),
            default_strategy=test_settings.default_strategy,
        )
        newer = await other.compile(quarantined={}, blocked_until={}, changes=None)
        publisher = FeedPublisher(redis_client, FeedCodec(SECRET), lease_key=lease.key, owner_id="controller-2")
        assert (await publisher.publish(newer.sections, changes=newer.changes, compiled_at=newer.compiled_at)).written
        head = await redis_client.client.get(FEED_HEAD_KEY)

        async def unreachable(**_: object) -> None:
            raise ConnectionError("Postgres is down")

        monkeypatch.setattr(builder._compiler, "compile", unreachable)
        # It stopped, and this process leads again.
        await redis_client.client.delete(lease.key)
        await until(lambda: lease.is_held and lease.term == 2)
        await until(lambda: builder.compile_error == "Postgres is down")
        await until(lambda: tripped.id in runtime_of(builder).quarantined)
        await asyncio.sleep(0.2)
        # The newer feed stands: this term published nothing of the last one's compile.
        assert await redis_client.client.get(FEED_HEAD_KEY) == head
        manifest = await redis_client.client.hgetall(FEED_MANIFEST_KEY)
        assert {connector_section(first.id), connector_section(second.id)} <= manifest.keys()


class TestFailures:
    async def test_a_compile_that_fails_mid_term_keeps_the_feed_going_and_is_tried_again(
        self,
        builder: FeedBuilder,
        services: Services,
        gateway: Gateway,
        redis_client: RedisClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.05)
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC)
        proxy = await healthy_proxy(services, connector, "10.0.0.1")
        await caught_up(builder, redis_client)
        compile = builder._compiler.compile

        async def unreachable(**_: object) -> None:
            raise ConnectionError("Postgres is down")

        monkeypatch.setattr(builder._compiler, "compile", unreachable)
        second = await services.connect(project, "Second", STATIC)
        await quarantine(redis_client, proxy.id, time.time() + 600)
        await until(lambda: builder.compile_error is not None and proxy.id in runtime_of(builder).quarantined)
        assert builder.compile_error == "Postgres is down"
        # The quarantine still reaches the gateways, on the last compile.
        await until(lambda: not builder._unpublished)
        assert await gateway.follower.poll()
        assert proxy.id in gateway.router.snapshot.quarantined

        monkeypatch.setattr(builder._compiler, "compile", compile)
        await until(lambda: builder.compile_error is None)
        await caught_up(builder, redis_client)
        assert connector_section(second.id) in builder.sections

    async def test_a_failing_compile_is_tried_again_after_a_back_off_that_doubles(
        self,
        builder: FeedBuilder,
        services: Services,
        redis_client: RedisClient,
        change_log: ChangeLog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Postgres away: a compile every cycle would hammer it and spin the controller."""
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.3)
        await services.project()
        await caught_up(builder, redis_client)
        attempts: list[float] = []

        async def unreachable(**_: object) -> None:
            attempts.append(time.monotonic())
            raise ConnectionError("Postgres is down")

        monkeypatch.setattr(builder._compiler, "compile", unreachable)
        await change_log.record(ChangeKind.ALL)
        await until(lambda: len(attempts) >= 1)
        await asyncio.sleep(1.5)
        # At once, then 0.3 s and 0.6 s later; the next comes 1.2 s after that. Redis ends a
        # blocked read up to a tenth of a second late (its hz), so each wait may run that much over.
        assert len(attempts) == 3
        gaps = [later - earlier for earlier, later in zip(attempts, attempts[1:], strict=False)]
        assert 0.3 <= gaps[0] < 0.5 and 0.6 <= gaps[1] < 0.8

    async def test_what_an_entry_asked_for_survives_a_cycle_that_failed(
        self,
        builder: FeedBuilder,
        services: Services,
        redis_client: RedisClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The entry is read once: a cycle that fails after reading it must not forget what it asked for."""
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.05)
        project = await services.project()
        tripped = await healthy_proxy(services, await services.connect(project, "ISP", STATIC), "10.0.0.1")
        await caught_up(builder, redis_client)
        read = builder._state.quarantined
        failed: list[None] = []

        async def unreachable() -> dict[str, datetime]:
            if not failed:
                failed.append(None)
                raise ConnectionError("Redis went away")
            return await read()

        monkeypatch.setattr(builder._state, "quarantined", unreachable)
        await quarantine(redis_client, tripped.id, time.time() + 600)
        await caught_up(builder, redis_client)
        assert failed == [None]
        assert tripped.id in runtime_of(builder).quarantined

    async def test_a_failed_publish_is_tried_again_with_nothing_announced(
        self,
        builder: FeedBuilder,
        services: Services,
        redis_client: RedisClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.05)
        project = await services.project()
        connector = await services.connect(project, "ISP", STATIC)
        await caught_up(builder, redis_client)
        publish = builder._publisher.publish

        async def refused(*_: object, **__: object) -> None:
            raise ConnectionError("Redis rejected the write")

        monkeypatch.setattr(builder._publisher, "publish", refused)
        await healthy_proxy(services, connector, "10.0.0.1")
        await until(lambda: builder.publish_error == "Redis rejected the write")

        # No entry and no timed duty is coming: only the owed publish brings the next one.
        published = builder.publishes
        monkeypatch.setattr(builder._publisher, "publish", publish)
        await until(lambda: builder.publish_error is None and builder.publishes > published)
        assert await redis_client.client.hexists(FEED_SECTIONS_KEY, connector_section(connector.id))


class TestStreams:
    async def test_a_cycle_longer_than_the_compile_interval_counts_as_an_overrun(
        self, make_builder: MakeBuilder, lease: Lease, redis_client: RedisClient
    ) -> None:
        builder = make_builder(lease, compile_interval=0.25)
        async with running(builder):
            await caught_up(builder, redis_client)
            stats = job_stats.get("feed_builder")
            assert stats is not None and stats.interval_seconds == 0.25

    async def test_the_builder_reports_how_far_it_read_each_stream(
        self, builder: FeedBuilder, services: Services, redis_client: RedisClient
    ) -> None:
        await services.project()
        await caught_up(builder, redis_client)
        states = builder.streams()
        assert set(states) == {"changes", "runtime"}
        assert states["changes"].position == await redis_client.stream_head(CHANGES_STREAM_KEY)
        # Nothing has written the runtime stream: no stream, and no gap for it.
        assert states["runtime"].position == "0-0" and states["runtime"].full_reads == {"start": 1}
        compiled = builder.compiled
        assert compiled is not None and compiled.changes == states["changes"].position
