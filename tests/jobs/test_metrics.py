# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the metrics jobs: a flush moves each window into Postgres once, and a compaction folds each row once.

Windows are written as metering writes them, and the jobs run on the real Postgres and Redis test containers.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.stats import FailureCount, FailureCountKey, MetricDelta, TunnelPeerMetricDelta
from api.db.models import (
    ConnectorExitIpModel,
    ConnectorMetricsModel,
    FailureMetricsModel,
    HostMetricsModel,
    IpObservationModel,
    MetricFlushModel,
    ProjectMetricsModel,
    ProxyMetricsModel,
    TunnelPeerMetricsModel,
)
from api.db.openvpn_repository import OpenVpnPeerRepository
from api.db.redis import (
    METRIC_WINDOW_KINDS,
    METRICS_TAKEN_KEY,
    PROJECT_WINDOWS,
    PROXY_WINDOWS,
    TUNNEL_PEER_WINDOWS,
    EntityWindows,
    MetricWindows,
    RedisClient,
)
from api.db.repository import MetricsRepository
from api.db.wireguard_repository import WireGuardPeerRepository
from api.geo.runtime import GeoStack
from api.jobs.job import JobBatch
from api.jobs.metrics import CompactionJob, MetricsFlushJob
from api.models.connector import Connector
from api.models.openvpn import OpenVpnPeer
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.models.wireguard import WireGuardPeer
from tests.jobs.conftest import Rows
from tests.waits import until

# Metrics flush entries, by the IDs their job stream gave them.
FIRST, SECOND = "1760000000000-0", "1760000060000-0"

# The six history tables, each with the counter its tests add up.
HISTORY: tuple[tuple[Any, Any], ...] = (
    (ProjectMetricsModel, ProjectMetricsModel.request_count),
    (ProxyMetricsModel, ProxyMetricsModel.request_count),
    (ConnectorMetricsModel, ConnectorMetricsModel.request_count),
    (TunnelPeerMetricsModel, TunnelPeerMetricsModel.request_count),
    (HostMetricsModel, HostMetricsModel.request_count),
    (FailureMetricsModel, FailureMetricsModel.failure_count),
)


def delivery(entry_id: str = FIRST) -> JobBatch:
    """The batch a runner hands a job for one entry of its stream."""
    return JobBatch(ids=(entry_id,), keys=("all",))


def requests(count: int, latency_ms: float = 100.0) -> MetricDelta:
    """``count`` successful requests, each of 10 bytes out and 20 back."""
    delta = MetricDelta()
    for _ in range(count):
        delta.add_request(True, latency_ms, 10, 20)
    return delta


async def meter(redis_client: RedisClient, windows: MetricWindows) -> None:
    """Add ``windows`` to the live ones, as metering and the failure log add them."""
    pipe = redis_client.client.pipeline(transaction=True)
    now = utc_now().isoformat()
    for kind, key, counts in windows.keyed():
        kind.add(pipe, key, counts, now)
    await pipe.execute()


async def live(redis_client: RedisClient) -> MetricWindows:
    """Every live window the indexes list, read as a flush takes them."""
    found: dict[str, str] = {}
    for kind in METRIC_WINDOW_KINDS:
        for key in await redis_client.client.smembers(kind.index):
            found[key] = json.dumps(await redis_client.client.hgetall(key))
    return MetricWindows.of(found)


async def staged(redis_client: RedisClient, flush_id: str) -> dict[str, str]:
    """The windows the staging hash of ``flush_id`` holds, by key."""
    held: dict[str, str] = await redis_client.client.hgetall(METRICS_TAKEN_KEY.format(message_id=flush_id))
    return held


async def summed(sessions: async_sessionmaker[AsyncSession], counter: Any) -> int:
    """A history counter added up over every row of its table."""
    async with sessions() as session:
        return int((await session.execute(select(func.coalesce(func.sum(counter), 0)))).scalar_one())


async def totals(sessions: async_sessionmaker[AsyncSession], kind: EntityWindows, *ids: str) -> dict[str, int]:
    """Each entity's all-time request count in metric_totals; an entity without totals is absent."""
    async with sessions() as session:
        found = await MetricsRepository(session).get_totals(kind, ids)
    return {entity_id: delta.request_count for entity_id, delta in found.items()}


async def recorded(sessions: async_sessionmaker[AsyncSession]) -> set[str]:
    """The flush IDs metric_flushes holds."""
    async with sessions() as session:
        return set((await session.execute(select(MetricFlushModel.message_id))).scalars().all())


async def stored(sessions: async_sessionmaker[AsyncSession], *rows: Any) -> None:
    """Write ``rows``, committed."""
    async with sessions() as session:
        session.add_all(rows)
        await session.commit()


async def failing(*_: Any, **__: Any) -> Any:
    """Stands in for a write, to Postgres or Redis, that does not land."""
    raise ConnectionError("The connection went away mid-write")


def unreachable() -> Any:
    """A session factory whose Postgres refuses every connection."""
    raise ConnectionRefusedError("Postgres is down")


async def wireguard_device(sessions: async_sessionmaker[AsyncSession], project: Project, name: str) -> WireGuardPeer:
    peer = WireGuardPeer(project_id=project.id, name=name, public_key=f"key-{name}=", address="10.13.0.2")
    async with sessions() as session:
        await WireGuardPeerRepository(session).create(peer)
        await session.commit()
    return peer


async def openvpn_device(sessions: async_sessionmaker[AsyncSession], project: Project, name: str) -> OpenVpnPeer:
    peer = OpenVpnPeer(
        project_id=project.id,
        name=name,
        certificate="cert",
        private_key="key",
        serial="1",
        certificate_expires_at=utc_now() + timedelta(days=365),
        address="10.14.0.2",
    )
    async with sessions() as session:
        await OpenVpnPeerRepository(session).create(peer)
        await session.commit()
    return peer


@pytest.fixture
def flush(sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient) -> MetricsFlushJob:
    return MetricsFlushJob(sessions, redis_client)


@pytest.fixture
async def project(rows: Rows) -> Project:
    return await rows.project()


class TestFlush:
    async def test_it_writes_history_and_totals_and_records_its_id(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        rows: Rows,
        project: Project,
    ) -> None:
        connector = await rows.connector(project)
        proxy = await rows.proxy(connector)
        refused: FailureCountKey = (project.id, connector.id, "upstream_rejected", 407)
        await meter(
            redis_client,
            MetricWindows(
                proxies={proxy.id: requests(2)},
                projects={project.id: requests(3, latency_ms=40.0)},
                connectors={connector.id: requests(4)},
                hosts={(project.id, connector.id, "example.com"): requests(5)},
                failures={refused: FailureCount(6)},
            ),
        )

        await flush.run(delivery(FIRST))

        assert await summed(sessions, ProxyMetricsModel.request_count) == 2
        assert await summed(sessions, ProjectMetricsModel.request_count) == 3
        assert await summed(sessions, ProjectMetricsModel.avg_latency_ms) == pytest.approx(40.0)
        assert await summed(sessions, ConnectorMetricsModel.bytes_received) == 80
        assert await summed(sessions, HostMetricsModel.request_count) == 5
        assert await summed(sessions, FailureMetricsModel.failure_count) == 6
        assert await totals(sessions, PROXY_WINDOWS, proxy.id) == {proxy.id: 2}
        assert await totals(sessions, PROJECT_WINDOWS, project.id) == {project.id: 3}
        assert await recorded(sessions) == {FIRST}
        # Nothing of it left in Redis: no window, no index member, no staging hash.
        assert (await live(redis_client)).empty
        assert not await redis_client.client.exists(*(kind.index for kind in METRIC_WINDOW_KINDS))
        assert not await staged(redis_client, FIRST)

    async def test_each_proxy_row_records_the_status_its_column_holds(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        rows: Rows,
        project: Project,
    ) -> None:
        connector = await rows.connector(project)
        working = await rows.proxy(connector, status=ProxyStatus.HEALTHY)
        leaving = await rows.proxy(connector, status=ProxyStatus.DRAINING)
        await meter(redis_client, MetricWindows(proxies={working.id: requests(1), leaving.id: requests(1)}))

        await flush.run(delivery())

        async with sessions() as session:
            statuses = dict((await session.execute(select(ProxyMetricsModel.proxy_id, ProxyMetricsModel.status))).all())
        assert statuses == {working.id: "healthy", leaving.id: "draining"}

    async def test_every_device_window_is_written_with_its_protocol_and_project(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        project: Project,
    ) -> None:
        phone = await wireguard_device(sessions, project, "phone")
        laptop = await openvpn_device(sessions, project, "laptop")
        phone_window = TunnelPeerMetricDelta(by_address=2, encrypted_dns_blocked=1)
        phone_window.add_request(True, 50.0, 100, 200)
        devices = {
            phone.id: phone_window,
            laptop.id: TunnelPeerMetricDelta(request_count=4),
            "gone": TunnelPeerMetricDelta(request_count=1),
        }
        await meter(redis_client, MetricWindows(tunnel_peers=devices))

        await flush.run(delivery())

        async with sessions() as session:
            written = {row.peer_id: row for row in (await session.execute(select(TunnelPeerMetricsModel))).scalars()}
            device_totals = await MetricsRepository(session).get_totals(TUNNEL_PEER_WINDOWS, [phone.id, laptop.id, "gone"])
        assert set(written) == {phone.id, laptop.id}
        phone_row = written[phone.id]
        assert (phone_row.protocol, phone_row.project_id, phone_row.granularity) == ("wireguard", project.id, 60)
        assert (phone_row.bytes_sent, phone_row.by_address, phone_row.encrypted_dns_blocked) == (100, 2, 1)
        assert phone_row.avg_latency_ms == pytest.approx(50.0)
        assert (written[laptop.id].protocol, written[laptop.id].request_count) == ("openvpn", 4)
        assert set(device_totals) == {phone.id, laptop.id}
        phone_total = device_totals[phone.id]
        assert isinstance(phone_total, TunnelPeerMetricDelta) and phone_total.by_address == 2
        # The gone device's window is taken with the rest.
        assert (await live(redis_client)).empty

    async def test_windows_of_entities_that_are_gone_are_dropped_and_leave_redis(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
    ) -> None:
        """Left in Redis, a deleted entity's window would be read and dropped at every flush."""
        await meter(
            redis_client,
            MetricWindows(
                proxies={"gone-proxy": requests(1)},
                projects={"gone-project": requests(2)},
                connectors={"gone-connector": requests(3)},
                tunnel_peers={"gone-device": TunnelPeerMetricDelta(request_count=4)},
                hosts={("gone-project", "gone-connector", "example.com"): requests(5)},
                failures={("gone-project", None, "no_proxy_available", None): FailureCount(6)},
            ),
        )

        await flush.run(delivery())

        for _model, counter in HISTORY:
            assert await summed(sessions, counter) == 0
        assert await totals(sessions, PROXY_WINDOWS, "gone-proxy") == {}
        assert await totals(sessions, PROJECT_WINDOWS, "gone-project") == {}
        assert await totals(sessions, TUNNEL_PEER_WINDOWS, "gone-device") == {}
        assert (await live(redis_client)).empty
        assert not await redis_client.client.exists(*(kind.index for kind in METRIC_WINDOW_KINDS))
        assert not await staged(redis_client, FIRST)

    async def test_nothing_to_flush_writes_and_records_nothing(
        self, flush: MetricsFlushJob, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        await flush.run(delivery())

        assert await recorded(sessions) == set()
        assert not await staged(redis_client, FIRST)

    async def test_increments_during_a_flush_go_to_the_next_one(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        refused: FailureCountKey = (project.id, None, "no_proxy_available", None)
        await meter(redis_client, MetricWindows(projects={project.id: requests(3)}, failures={refused: FailureCount(4)}))
        save = MetricsRepository.save_project_metrics_snapshots

        async def save_while_a_gateway_meters(self: MetricsRepository, batch: dict[str, MetricDelta]) -> list[str]:
            more = MetricWindows(projects={project.id: requests(2)}, failures={refused: FailureCount(1)})
            await meter(redis_client, more)
            return await save(self, batch)

        with monkeypatch.context() as patched:
            patched.setattr(MetricsRepository, "save_project_metrics_snapshots", save_while_a_gateway_meters)
            await flush.run(delivery(FIRST))

        # This flush wrote what it took; what came meanwhile waits in Redis.
        assert await totals(sessions, PROJECT_WINDOWS, project.id) == {project.id: 3}
        assert await summed(sessions, FailureMetricsModel.failure_count) == 4
        waiting = await live(redis_client)
        assert waiting.projects[project.id].request_count == 2
        assert waiting.failures == {refused: FailureCount(1)}

        await flush.run(delivery(SECOND))

        assert await totals(sessions, PROJECT_WINDOWS, project.id) == {project.id: 5}
        assert await summed(sessions, ProjectMetricsModel.request_count) == 5
        assert await summed(sessions, FailureMetricsModel.failure_count) == 5
        assert (await live(redis_client)).empty


class TestRedelivery:
    async def test_a_failing_commit_keeps_the_staging(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await meter(redis_client, MetricWindows(projects={project.id: requests(3)}))
        monkeypatch.setattr(MetricsRepository, "save_project_metrics_snapshots", failing)

        with pytest.raises(ConnectionError):
            await flush.run(delivery(FIRST))

        # Out of the live windows, held in the staging hash, and nothing in Postgres.
        assert (await live(redis_client)).empty
        assert set(await staged(redis_client, FIRST)) == {PROJECT_WINDOWS.key(project.id)}
        assert await summed(sessions, ProjectMetricsModel.request_count) == 0
        assert await totals(sessions, PROJECT_WINDOWS, project.id) == {}
        assert await recorded(sessions) == set()

    async def test_a_redelivered_id_not_recorded_commits_its_staged_windows_once(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        rows: Rows,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        other = await rows.project()
        await meter(redis_client, MetricWindows(projects={project.id: requests(3)}))
        with monkeypatch.context() as patched:
            patched.setattr(MetricsRepository, "save_project_metrics_snapshots", failing)
            with pytest.raises(ConnectionError):
                await flush.run(delivery(FIRST))
        # Gateways meter on, for the staged project and for another.
        await meter(redis_client, MetricWindows(projects={project.id: requests(2), other.id: requests(4)}))

        await flush.run(delivery(FIRST))

        # The staged window once and the other project's; the staged project's new counts wait for the next flush.
        assert await totals(sessions, PROJECT_WINDOWS, project.id, other.id) == {project.id: 3, other.id: 4}
        assert await recorded(sessions) == {FIRST}
        assert not await staged(redis_client, FIRST)
        assert (await live(redis_client)).projects == {project.id: requests(2)}

        await flush.run(delivery(SECOND))

        assert await totals(sessions, PROJECT_WINDOWS, project.id, other.id) == {project.id: 5, other.id: 4}
        assert await summed(sessions, ProjectMetricsModel.request_count) == 9

    async def test_a_redelivered_id_already_recorded_only_clears(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        rows: Rows,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        other = await rows.project()
        await meter(redis_client, MetricWindows(projects={project.id: requests(3)}))
        # The commit lands, and the staging hash's delete does not.
        with monkeypatch.context() as patched:
            patched.setattr(redis_client, "clear_metric_windows", failing)
            with pytest.raises(ConnectionError):
                await flush.run(delivery(FIRST))
        assert await recorded(sessions) == {FIRST} and await staged(redis_client, FIRST)
        metered_since = {project.id: requests(2), other.id: requests(4)}
        await meter(redis_client, MetricWindows(projects=metered_since))

        await flush.run(delivery(FIRST))

        assert not await staged(redis_client, FIRST)
        assert await totals(sessions, PROJECT_WINDOWS, project.id, other.id) == {project.id: 3}
        assert await summed(sessions, ProjectMetricsModel.request_count) == 3
        # Nothing is taken under an ID that can no longer commit: what came since waits for the next flush.
        assert (await live(redis_client)).projects == metered_since
        await flush.run(delivery(SECOND))
        assert await totals(sessions, PROJECT_WINDOWS, project.id, other.id) == {project.id: 5, other.id: 4}

    async def test_a_delivery_whose_id_another_committed_meanwhile_writes_nothing(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A worker stalled past the claim timeout runs beside the one that reclaimed its entry: one of them commits."""
        await meter(redis_client, MetricWindows(projects={project.id: requests(3)}))
        take = RedisClient.take_metric_windows

        async def take_while_the_other_commits(self: RedisClient, message_id: str) -> MetricWindows:
            windows = await take(self, message_id)
            async with sessions() as session:
                await MetricsRepository(session).record_flush(message_id)
                await session.commit()
            return windows

        monkeypatch.setattr(RedisClient, "take_metric_windows", take_while_the_other_commits)

        await flush.run(delivery(FIRST))

        assert await summed(sessions, ProjectMetricsModel.request_count) == 0
        assert await totals(sessions, PROJECT_WINDOWS, project.id) == {}
        assert not await staged(redis_client, FIRST)


class TestFlushesAtOnce:
    async def test_two_flushes_at_once_count_every_request_once(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        rows: Rows,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The first takes, the second takes what came since and commits it, then the first commits."""
        connector = await rows.connector(project)
        host = (project.id, connector.id, "example.com")
        await meter(redis_client, MetricWindows(projects={project.id: requests(3)}, hosts={host: requests(3)}))
        record = MetricsRepository.record_flush
        second_done = asyncio.Event()

        async def record_after_the_second(self: MetricsRepository, message_id: str) -> bool:
            if message_id == FIRST:
                await second_done.wait()
            return await record(self, message_id)

        monkeypatch.setattr(MetricsRepository, "record_flush", record_after_the_second)
        first = asyncio.create_task(flush.run(delivery(FIRST)))

        async def first_took() -> bool:
            return bool(await staged(redis_client, FIRST))

        await until(first_took)
        await meter(redis_client, MetricWindows(projects={project.id: requests(2)}, hosts={host: requests(2)}))
        await flush.run(delivery(SECOND))
        second_done.set()
        await asyncio.wait_for(first, 5)

        assert await totals(sessions, PROJECT_WINDOWS, project.id) == {project.id: 5}
        assert await summed(sessions, ProjectMetricsModel.request_count) == 5
        assert await summed(sessions, HostMetricsModel.request_count) == 5
        assert await recorded(sessions) == {FIRST, SECOND}
        # No window left behind, so none negative and none lost.
        assert (await live(redis_client)).empty
        assert not await staged(redis_client, FIRST) and not await staged(redis_client, SECOND)

    async def test_flushes_racing_over_many_windows_write_each_once(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        rows: Rows,
        project: Project,
    ) -> None:
        """More windows than one take script handles, so the two takes interleave."""
        connector = await rows.connector(project)
        hosts = {(project.id, connector.id, f"h{number}.example.com"): requests(1) for number in range(1200)}
        await meter(redis_client, MetricWindows(hosts=hosts))

        await asyncio.gather(flush.run(delivery(FIRST)), flush.run(delivery(SECOND)))

        async with sessions() as session:
            written = (await session.execute(select(func.count()).select_from(HostMetricsModel))).scalar_one()
        assert written == 1200
        assert await summed(sessions, HostMetricsModel.request_count) == 1200
        assert (await live(redis_client)).empty
        assert not await staged(redis_client, FIRST) and not await staged(redis_client, SECOND)


class TestGiveUp:
    async def test_one_whose_id_is_recorded_only_clears_and_puts_nothing_back(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The fifth delivery committed, and its staging hash outlived it."""
        await meter(redis_client, MetricWindows(projects={project.id: requests(3)}))
        with monkeypatch.context() as patched:
            patched.setattr(redis_client, "clear_metric_windows", failing)
            with pytest.raises(ConnectionError):
                await flush.run(delivery(FIRST))
        await meter(redis_client, MetricWindows(projects={project.id: requests(2)}))

        await flush.give_up(delivery(FIRST))

        assert not await staged(redis_client, FIRST)
        assert (await live(redis_client)).projects == {project.id: requests(2)}
        await flush.run(delivery(SECOND))
        assert await totals(sessions, PROJECT_WINDOWS, project.id) == {project.id: 5}

    async def test_one_whose_id_is_not_recorded_puts_the_counts_back_for_the_next_flush(
        self,
        flush: MetricsFlushJob,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        refused: FailureCountKey = (project.id, None, "no_proxy_available", None)
        taken = MetricWindows(projects={project.id: requests(3, latency_ms=12.5)}, failures={refused: FailureCount(4)})
        await meter(redis_client, taken)
        with monkeypatch.context() as patched:
            patched.setattr(MetricsRepository, "save_project_metrics_snapshots", failing)
            with pytest.raises(ConnectionError):
                await flush.run(delivery(FIRST))
        await meter(redis_client, MetricWindows(projects={project.id: requests(2, latency_ms=12.5)}))

        await flush.give_up(delivery(FIRST))

        assert not await staged(redis_client, FIRST)
        back = await live(redis_client)
        assert back.projects == {project.id: requests(5, latency_ms=12.5)}
        assert back.failures == {refused: FailureCount(4)}
        await flush.run(delivery(SECOND))
        assert await totals(sessions, PROJECT_WINDOWS, project.id) == {project.id: 5}
        assert await summed(sessions, FailureMetricsModel.failure_count) == 4
        assert await recorded(sessions) == {SECOND}

    async def test_one_with_postgres_down_raises_and_keeps_the_staging(
        self,
        flush: MetricsFlushJob,
        redis_client: RedisClient,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Whether the flush committed cannot be told, so the entry stays pending for the next reclaim."""
        await meter(redis_client, MetricWindows(projects={project.id: requests(3)}))
        with monkeypatch.context() as patched:
            patched.setattr(MetricsRepository, "save_project_metrics_snapshots", failing)
            with pytest.raises(ConnectionError):
                await flush.run(delivery(FIRST))
        held = await staged(redis_client, FIRST)

        with pytest.raises(ConnectionRefusedError):
            await MetricsFlushJob(unreachable, redis_client).give_up(delivery(FIRST))

        assert held and await staged(redis_client, FIRST) == held
        assert (await live(redis_client)).empty


# The one entry a compaction run is handed.
COMPACTION = delivery("1760000000000-0")


async def history(sessions: async_sessionmaker[AsyncSession]) -> dict[tuple[str, int], tuple[int, int]]:
    """Each history table's rows by granularity: (table, granularity) -> (rows, the counter added up)."""
    found: dict[tuple[str, int], tuple[int, int]] = {}
    async with sessions() as session:
        for model, counter in HISTORY:
            result = await session.execute(
                select(model.granularity, func.count(), func.sum(counter)).group_by(model.granularity)
            )
            found.update(
                ((model.__tablename__, granularity), (count, int(total))) for granularity, count, total in result.all()
            )
    return found


async def waiting_for_locks(sessions: async_sessionmaker[AsyncSession]) -> int:
    """How many statements of the test database wait for a lock another transaction holds."""
    async with sessions() as session:
        result = await session.execute(
            text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()")
        )
        return int(result.scalar_one())


@dataclass
class Series:
    """A project with one connector and one proxy: what the six history tables keep series of."""

    project: Project
    connector: Connector
    proxy: Proxy

    @classmethod
    async def of(cls, rows: Rows, project: Project) -> Series:
        connector = await rows.connector(project)
        return cls(project, connector, await rows.proxy(connector))

    async def write(self, sessions: async_sessionmaker[AsyncSession], at: datetime, granularity: int) -> None:
        """One row in each history table, of two requests or two failures, written at ``at`` with ``granularity``."""
        counts = {"timestamp": at, "request_count": 2, "avg_latency_ms": 100.0, "granularity": granularity}
        await stored(
            sessions,
            ProjectMetricsModel(project_id=self.project.id, **counts),
            ProxyMetricsModel(proxy_id=self.proxy.id, status="healthy", **counts),
            ConnectorMetricsModel(connector_id=self.connector.id, **counts),
            TunnelPeerMetricsModel(peer_id="device", protocol="wireguard", project_id=self.project.id, **counts),
            HostMetricsModel(project_id=self.project.id, connector_id=self.connector.id, host="example.com", **counts),
            FailureMetricsModel(
                project_id=self.project.id, kind="no_proxy_available", timestamp=at, failure_count=2, granularity=granularity
            ),
        )


class TestCompaction:
    @pytest.fixture
    def compaction(self, sessions: async_sessionmaker[AsyncSession], geo: GeoStack) -> CompactionJob:
        return CompactionJob(sessions, geo.geo_service)

    @pytest.fixture
    async def series(self, rows: Rows, project: Project) -> Series:
        return await Series.of(rows, project)

    async def test_every_table_folds_its_old_minutes_into_hours(
        self, compaction: CompactionJob, sessions: async_sessionmaker[AsyncSession], series: Series
    ) -> None:
        hour = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
        for minute in (5, 25, 45):
            await series.write(sessions, hour + timedelta(minutes=minute), 60)
        await series.write(sessions, utc_now(), 60)

        await compaction.run(COMPACTION)

        # The three old minutes in one hour, and the fresh one untouched.
        expected = {(model.__tablename__, 3600): (1, 6) for model, _counter in HISTORY}
        expected.update({(model.__tablename__, 60): (1, 2) for model, _counter in HISTORY})
        assert await history(sessions) == expected
        async with sessions() as session:
            hours = await session.execute(select(HostMetricsModel.timestamp).where(HostMetricsModel.granularity == 3600))
            assert hours.scalars().all() == [hour]

    async def test_tiers_run_in_order_so_a_minute_a_month_old_reaches_its_day(
        self, compaction: CompactionJob, sessions: async_sessionmaker[AsyncSession], series: Series
    ) -> None:
        day = utc_now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=40)
        await series.write(sessions, day + timedelta(hours=1), 60)
        await series.write(sessions, day + timedelta(hours=13), 60)

        await compaction.run(COMPACTION)

        assert await history(sessions) == {(model.__tablename__, 86400): (1, 4) for model, _counter in HISTORY}

    async def test_two_compactions_at_once_fold_each_row_once(
        self, compaction: CompactionJob, sessions: async_sessionmaker[AsyncSession], series: Series
    ) -> None:
        hour = utc_now().replace(minute=0, second=0, microsecond=0) - timedelta(days=2)
        for minute in (5, 25, 45):
            await series.write(sessions, hour + timedelta(minutes=minute), 60)

        async with sessions() as holder:
            # Held by a third transaction, so both passes reach the same rows before either may fold them.
            await holder.execute(select(ProjectMetricsModel.id).with_for_update())
            passes = [asyncio.create_task(compaction.run(COMPACTION)) for _ in range(2)]

            async def both_wait() -> bool:
                return await waiting_for_locks(sessions) == 2

            await until(both_wait)
            await holder.commit()
        await asyncio.wait_for(asyncio.gather(*passes), 10)

        assert await history(sessions) == {(model.__tablename__, 3600): (1, 6) for model, _counter in HISTORY}

    async def test_a_second_pass_adds_nothing(
        self, compaction: CompactionJob, sessions: async_sessionmaker[AsyncSession], series: Series
    ) -> None:
        now = utc_now()
        await series.write(sessions, now - timedelta(days=2), 60)
        await series.write(sessions, now - timedelta(days=8), 3600)
        await series.write(sessions, now - timedelta(days=31), 21600)
        await series.write(sessions, now - timedelta(hours=1), 60)
        await compaction.run(COMPACTION)
        first = await history(sessions)

        await compaction.run(COMPACTION)

        assert await history(sessions) == first
        assert {granularity for _table, granularity in first} == {60, 3600, 21600, 86400}

    async def test_metrics_past_a_projects_retention_are_deleted_and_none_at_zero_days(
        self, compaction: CompactionJob, sessions: async_sessionmaker[AsyncSession], rows: Rows
    ) -> None:
        limited = await Series.of(rows, await rows.project(metrics_retention_days=30))
        unlimited = await Series.of(rows, await rows.project(metrics_retention_days=0))
        now = utc_now()
        for owner, ages in ((limited, (40, 10)), (unlimited, (41, 11))):
            for age in ages:
                await owner.write(sessions, now - timedelta(days=age), 86400)

        await compaction.run(COMPACTION)

        async with sessions() as session:
            for model, _counter in HISTORY:
                left = (await session.execute(select(model.timestamp))).scalars().all()
                assert sorted((now - at).days for at in left) == [10, 11, 41], model.__tablename__

    async def test_observations_and_exit_ips_past_the_settings_retention_are_deleted(
        self,
        compaction: CompactionJob,
        sessions: async_sessionmaker[AsyncSession],
        rows: Rows,
        project: Project,
        geo: GeoStack,
    ) -> None:
        connector = await rows.connector(project)
        now = utc_now()
        await stored(
            sessions,
            IpObservationModel(observed_at=now - timedelta(days=4), source="echo", ip="198.51.100.1"),
            IpObservationModel(observed_at=now - timedelta(days=2), source="echo", ip="198.51.100.2"),
            ConnectorExitIpModel(
                connector_id=connector.id, ip="198.51.100.1", first_seen=now - timedelta(days=20),
                last_seen=now - timedelta(days=11),
            ),
            ConnectorExitIpModel(
                connector_id=connector.id, ip="198.51.100.2", first_seen=now - timedelta(days=20),
                last_seen=now - timedelta(days=9),
            ),
        )
        store = geo.settings_store

        async def kept() -> tuple[list[str], list[str]]:
            async with sessions() as session:
                observed = (await session.execute(select(IpObservationModel.ip).order_by(IpObservationModel.ip))).scalars()
                exits = (await session.execute(select(ConnectorExitIpModel.ip).order_by(ConnectorExitIpModel.ip))).scalars()
                return list(observed), list(exits)

        store.adopt(store.settings.model_copy(update={"observation_retention_days": 0, "exit_ip_retention_days": 0}), stored=True)
        await compaction.run(COMPACTION)
        assert await kept() == (["198.51.100.1", "198.51.100.2"], ["198.51.100.1", "198.51.100.2"])

        # The settings as the worker holds them now, saved by an admin since the last pass.
        store.adopt(store.settings.model_copy(update={"observation_retention_days": 3, "exit_ip_retention_days": 10}), stored=True)
        await compaction.run(COMPACTION)
        assert await kept() == (["198.51.100.2"], ["198.51.100.2"])

    async def test_flush_records_older_than_a_day_are_forgotten(
        self, compaction: CompactionJob, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        now = utc_now()
        await stored(
            sessions,
            MetricFlushModel(message_id="1-0", flushed_at=now - timedelta(hours=25)),
            MetricFlushModel(message_id="2-0", flushed_at=now - timedelta(hours=23)),
        )

        await compaction.run(COMPACTION)

        assert await recorded(sessions) == {"2-0"}

    async def test_totals_of_gone_entities_are_dropped(
        self, compaction: CompactionJob, sessions: async_sessionmaker[AsyncSession], project: Project
    ) -> None:
        async with sessions() as session:
            repo = MetricsRepository(session)
            await repo.upsert_totals(
                PROJECT_WINDOWS, {project.id: MetricDelta(request_count=3), "gone": MetricDelta(request_count=1)}
            )
            await repo.upsert_totals(PROXY_WINDOWS, {"gone-proxy": MetricDelta(request_count=1)})
            await session.commit()

        await compaction.run(COMPACTION)

        assert await totals(sessions, PROJECT_WINDOWS, project.id, "gone") == {project.id: 3}
        assert await totals(sessions, PROXY_WINDOWS, "gone-proxy") == {}
