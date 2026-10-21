# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the housekeeping jobs (api.jobs.housekeeping), each run as a worker runs it on its one ``all`` entry.

Postgres and Redis are the real test containers, and what a job did is read back from them.
"""

from __future__ import annotations

import asyncio
import gzip
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.roles import JobKind
from api.db.geo_repository import GeoDatabaseRepository
from api.db.models import SystemMetricsModel
from api.db.openvpn_repository import OpenVpnPeerRepository
from api.db.redis import (
    INSTANCE_INDEX_KEY,
    INSTANCE_REGISTRY_KEY,
    JOB_STREAM_KEY,
    OBSERVATION_STREAM_KEY,
    QUARANTINE_KEY,
    SIGHTING_STREAM_KEY,
    TRAFFIC_BLOCKED_KEY,
    TRAFFIC_PENDING_KEY,
    TUNNEL_PEER_STATUS_INDEX_KEY,
    TUNNEL_PEER_STATUS_KEY,
    WORKER_GROUP,
    RedisClient,
)
from api.db.repository import SystemMetricsRepository
from api.db.streams import ChangeLog, StreamGroup
from api.db.wireguard_repository import WireGuardPeerRepository
from api.geo.models import GeoDatabaseRecord, GeoDatabaseSource
from api.geo.store import GeoDatabaseStore
from api.geo.updater import GeoDatabaseUpdater
from api.jobs import housekeeping
from api.jobs.housekeeping import GeoUpdateJob, IndexHygieneJob, SnapshotJob, TunnelLastSeenJob
from api.jobs.job import JobBatch
from api.jobs.queue import JobQueue
from api.jobs.traffic import TrafficJob
from api.models.openvpn import OpenVpnPeer, OpenVpnSessionReading
from api.models.project import Project
from api.models.proxy import ProxyStatus
from api.models.wireguard import WireGuardPeer, WireGuardPeerReading
from api.providers.sdk.egress import EgressPolicy
from tests.conftest import queued_job_keys
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb
from tests.jobs.conftest import Rows

# The one entry an install-wide kind's producer publishes each period.
ALL = JobBatch(ids=("1-0",), keys=("all",))
# Downloads from a test vendor host, answered by a mock transport.
OPEN = EgressPolicy(allow_http=True, allow_private=True, pin_dns=False)


def snapshot_job(sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient, **overrides: Any) -> SnapshotJob:
    options: dict[str, Any] = {"interval": 300, "retention_days": 90, **overrides}
    return SnapshotJob(sessions, redis_client, **options)


async def stored_readings(sessions: async_sessionmaker[AsyncSession]) -> list[dict[str, Any]]:
    """Every ``system_metrics`` row of the last thousand days, oldest first."""
    async with sessions() as session:
        return await SystemMetricsRepository(session).get_history(utc_now() - timedelta(days=1000))


async def add_reading(sessions: async_sessionmaker[AsyncSession], age: timedelta, **gauges: Any) -> None:
    """A reading stored ``age`` ago, as an earlier snapshot left it."""
    async with sessions() as session:
        session.add(SystemMetricsModel(timestamp=utc_now() - age, **gauges))
        await session.commit()


class TestSnapshotJob:
    async def test_stores_one_row_of_live_gauges_with_the_table_sizes(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        await snapshot_job(sessions, redis_client).run(ALL)

        rows = await stored_readings(sessions)
        assert len(rows) == 1
        # Live reads of the real containers.
        assert rows[0]["database_size_bytes"] > 0 and rows[0]["redis_memory_bytes"] > 0
        async with sessions() as session:
            model = (await session.execute(SystemMetricsModel.__table__.select())).one()
        assert model.table_sizes["proxies"] > 0

    async def test_counts_proxies_by_the_status_their_column_holds(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient, rows: Rows
    ) -> None:
        connector = await rows.connector(await rows.project())
        statuses = [ProxyStatus.HEALTHY, ProxyStatus.HEALTHY, ProxyStatus.UNHEALTHY, ProxyStatus.DRAINING]
        for index, status in enumerate(statuses):
            await rows.proxy(connector, host=f"10.0.0.{index}", port=8080, status=status)

        await snapshot_job(sessions, redis_client).run(ALL)

        async with sessions() as session:
            model = (await session.execute(SystemMetricsModel.__table__.select())).one()
        assert (model.proxies_total, model.proxies_healthy, model.proxies_unhealthy) == (4, 2, 1)
        assert model.proxy_status_counts == {"draining": 1, "healthy": 2, "unhealthy": 1}
        assert (model.projects, model.connectors, model.connectors_enabled) == (1, 1, 1)

    async def test_a_reading_younger_than_the_interval_stands_for_this_one(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        """A snapshot entry published twice, or redelivered, adds no second row."""
        job = snapshot_job(sessions, redis_client, interval=3600)

        await job.run(ALL)
        await job.run(ALL)

        assert len(await stored_readings(sessions)) == 1

    async def test_one_is_taken_once_the_newest_is_nine_tenths_of_the_interval_old(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        """The readings set the cadence, with room for a producer's jitter, whichever worker runs the entry."""
        await add_reading(sessions, timedelta(seconds=500))
        await snapshot_job(sessions, redis_client, interval=600).run(ALL)
        assert len(await stored_readings(sessions)) == 1

        async with sessions() as session:
            await session.execute(SystemMetricsModel.__table__.delete())
            await session.commit()
        await add_reading(sessions, timedelta(seconds=570))
        await snapshot_job(sessions, redis_client, interval=600).run(ALL)
        assert len(await stored_readings(sessions)) == 2

    async def test_short_intervals_are_floored_to_a_minute(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        await add_reading(sessions, timedelta(seconds=30))

        await snapshot_job(sessions, redis_client, interval=5).run(ALL)

        assert len(await stored_readings(sessions)) == 1

    async def test_an_interval_of_zero_takes_none(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        await snapshot_job(sessions, redis_client, interval=0).run(ALL)

        assert await stored_readings(sessions) == []


class TestSnapshotRetention:
    async def test_readings_past_the_window_go_once_a_reading_is_stored(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        await add_reading(sessions, timedelta(days=120))
        await add_reading(sessions, timedelta(days=10))

        await snapshot_job(sessions, redis_client, retention_days=90).run(ALL)

        ages = [utc_now() - row["timestamp"] for row in await stored_readings(sessions)]
        assert len(ages) == 2 and all(age < timedelta(days=11) for age in ages)

    async def test_zero_days_keeps_every_reading(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
    ) -> None:
        await add_reading(sessions, timedelta(days=900))

        await snapshot_job(sessions, redis_client, retention_days=0).run(ALL)

        assert len(await stored_readings(sessions)) == 2


class TestSnapshotHistory:
    async def test_buckets_average_rather_than_sum(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Gauges average, unlike the counters' history."""
        # Buckets align to epoch boundaries, so the samples sit inside one known hour.
        base = utc_now().replace(minute=5, second=0, microsecond=0) - timedelta(hours=2)
        async with sessions() as session:
            for offset, size in ((0, 100), (5, 200), (10, 300)):
                session.add(
                    SystemMetricsModel(
                        timestamp=base + timedelta(minutes=offset), database_size_bytes=size, proxies_total=size // 100
                    )
                )
            await session.commit()

        async with sessions() as session:
            rows = await SystemMetricsRepository(session).get_history_aggregated(
                utc_now() - timedelta(hours=4), bucket_seconds=3600
            )

        assert len(rows) == 1
        assert (rows[0]["database_size_bytes"], rows[0]["proxies_total"]) == (200, 2)

    async def test_returns_points_oldest_first(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        for minutes in (30, 10, 20):
            await add_reading(sessions, timedelta(minutes=minutes), database_size_bytes=minutes)

        rows = await stored_readings(sessions)

        assert [row["database_size_bytes"] for row in rows] == [30, 20, 10]

    async def test_table_growth_uses_window_edges(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        await add_reading(sessions, timedelta(hours=2), table_sizes={"proxies": 100})
        await add_reading(sessions, timedelta(minutes=1), table_sizes={"proxies": 180})

        async with sessions() as session:
            first, last = await SystemMetricsRepository(session).get_table_sizes_at_edges(
                utc_now() - timedelta(hours=3)
            )

        assert (first, last) == ({"proxies": 100}, {"proxies": 180})


def serve(monkeypatch: pytest.MonkeyPatch, content: bytes) -> list[httpx.Request]:
    """Answer every download the updater makes with ``content``; the requests it made, in order."""
    requests: list[httpx.Request] = []
    client = httpx.AsyncClient

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=content)

    def mocked(**options: Any) -> httpx.AsyncClient:
        return client(transport=httpx.MockTransport(answer), **options)

    monkeypatch.setattr(httpx, "AsyncClient", mocked)
    return requests


class TestGeoUpdateJob:
    async def test_downloads_what_is_due_and_asks_for_every_proxy_to_be_judged_again_on_a_new_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        sessions: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        change_log: ChangeLog,
    ) -> None:
        mmdb = write_mmdb(tmp_path / "country.mmdb", "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()
        downloads = serve(monkeypatch, gzip.compress(mmdb))
        due = GeoDatabaseRecord(
            name="due", source=GeoDatabaseSource.URL, update_url="https://vendor.test/due.mmdb.gz", update_interval_hours=24
        )
        current = GeoDatabaseRecord(
            name="current",
            source=GeoDatabaseSource.URL,
            update_url="https://vendor.test/current.mmdb.gz",
            update_interval_hours=24,
            last_update_at=utc_now(),
        )
        async with sessions() as session:
            for record in (due, current):
                await GeoDatabaseRepository(session).create(record, None)
            await session.commit()
        jobs = JobQueue(redis_client)

        async def judge_every_proxy_again() -> None:
            await jobs.request_now(JobKind.REATTRIBUTE, ["all"])

        store = GeoDatabaseStore(sessions, tmp_path / "cache")
        updater = GeoDatabaseUpdater(
            sessions, store, changes=change_log, after_update=judge_every_proxy_again, egress_policy=OPEN
        )
        job = GeoUpdateJob(updater)
        try:
            await job.run(ALL)

            assert [request.url.path for request in downloads] == ["/due.mmdb.gz"]
            async with sessions() as session:
                assert await GeoDatabaseRepository(session).get_blob(due.id) == mmdb
            assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == ["all"]

            # Each is downloaded within its interval now: a second run finds nothing due.
            await job.run(ALL)
            assert len(downloads) == 1
            assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == ["all"]
        finally:
            store.close_all()


async def wireguard_device(
    sessions: async_sessionmaker[AsyncSession], project: Project, name: str, address: str
) -> WireGuardPeer:
    peer = WireGuardPeer(project_id=project.id, name=name, public_key=f"key-{name}=", address=address)
    async with sessions() as session:
        await WireGuardPeerRepository(session).create(peer)
        await session.commit()
    return peer


async def openvpn_device(
    sessions: async_sessionmaker[AsyncSession], project: Project, name: str, address: str
) -> OpenVpnPeer:
    peer = OpenVpnPeer(
        project_id=project.id,
        name=name,
        certificate="cert",
        private_key="key",
        serial="1",
        certificate_expires_at=utc_now() + timedelta(days=365),
        address=address,
    )
    async with sessions() as session:
        await OpenVpnPeerRepository(session).create(peer)
        await session.commit()
    return peer


class TestTunnelLastSeenJob:
    async def test_published_sightings_are_recorded_by_key_and_id_and_never_moved_back(
        self, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient, rows: Rows
    ) -> None:
        project = await rows.project()
        phone = await wireguard_device(sessions, project, "phone", "10.13.0.2")
        tablet = await wireguard_device(sessions, project, "tablet", "10.13.0.3")
        laptop = await openvpn_device(sessions, project, "laptop", "10.14.0.2")
        job = TunnelLastSeenJob(sessions, redis_client)
        now = int(time.time())
        # Two gateways carry the phone in turn: the later handshake counts. No device holds the
        # stranger's key, and the tablet has not handshaked since the interface came up.
        earlier = WireGuardPeerReading(latest_handshake=now - 60, endpoint="1.1.1.1:1")
        later = WireGuardPeerReading(latest_handshake=now - 5, endpoint="2.2.2.2:2")
        stranger = WireGuardPeerReading(latest_handshake=now)
        silent = WireGuardPeerReading(endpoint="9.9.9.9:9")
        connected = OpenVpnSessionReading(connected_since=now - 30, endpoint="3.3.3.3:3")
        await redis_client.set_tunnel_peer_status("wireguard", "gw-1", {phone.public_key: earlier.model_dump_json()})
        await redis_client.set_tunnel_peer_status(
            "wireguard",
            "gw-2",
            {
                phone.public_key: later.model_dump_json(),
                "unknown-key=": stranger.model_dump_json(),
                tablet.public_key: silent.model_dump_json(),
            },
        )
        await redis_client.set_tunnel_peer_status("openvpn", "gw-1", {laptop.id: connected.model_dump_json()})

        await job.run(ALL)

        async with sessions() as session:
            stored_phone = await WireGuardPeerRepository(session).get_by_id(phone.id)
            stored_tablet = await WireGuardPeerRepository(session).get_by_id(tablet.id)
            stored_laptop = await OpenVpnPeerRepository(session).get_by_id(laptop.id)
        assert stored_phone is not None and stored_tablet is not None and stored_laptop is not None
        assert (stored_phone.last_handshake_at, stored_phone.last_endpoint) == (later.handshake_at, "2.2.2.2:2")
        assert (stored_tablet.last_handshake_at, stored_tablet.last_endpoint) == (None, None)
        assert (stored_laptop.last_connected_at, stored_laptop.last_endpoint) == (connected.connected_at, "3.3.3.3:3")

        # Only older readings left: nothing moves back.
        await redis_client.client.delete(TUNNEL_PEER_STATUS_KEY.format(protocol="wireguard", instance_id="gw-1"))
        older_handshake = WireGuardPeerReading(latest_handshake=now - 600, endpoint="5.5.5.5:5")
        older_session = OpenVpnSessionReading(connected_since=now - 900, endpoint="6.6.6.6:6")
        await redis_client.set_tunnel_peer_status("wireguard", "gw-2", {phone.public_key: older_handshake.model_dump_json()})
        await redis_client.set_tunnel_peer_status("openvpn", "gw-1", {laptop.id: older_session.model_dump_json()})

        await job.run(ALL)

        async with sessions() as session:
            again_phone = await WireGuardPeerRepository(session).get_by_id(phone.id)
            again_laptop = await OpenVpnPeerRepository(session).get_by_id(laptop.id)
        assert again_phone is not None and again_phone.last_endpoint == "2.2.2.2:2"
        assert again_laptop is not None and again_laptop.last_endpoint == "3.3.3.3:3"

    async def test_without_a_sighting_published_it_opens_no_postgres_session(self, redis_client: RedisClient) -> None:
        """Most installs carry no tunnel, and the job comes every ten seconds."""

        def no_session() -> Any:
            raise AssertionError("no Postgres session expected")

        await redis_client.set_tunnel_peer_status(
            "wireguard", "gw-1", {"key-silent=": WireGuardPeerReading(endpoint="9.9.9.9:9").model_dump_json()}
        )

        await TunnelLastSeenJob(no_session, redis_client).run(ALL)  # type: ignore[arg-type]


@pytest.fixture
def hygiene(redis_client: RedisClient) -> IndexHygieneJob:
    return IndexHygieneJob(redis_client, JobQueue(redis_client))


async def consumer_names(redis_client: RedisClient, stream: str) -> set[str]:
    """The consumers of the workers' group on ``stream``."""
    return {str(consumer["name"]) for consumer in await redis_client.client.xinfo_consumers(stream, WORKER_GROUP)}


class TestIndexHygieneJob:
    async def test_dead_ids_are_dropped_from_the_indexes_and_live_ones_kept(
        self, redis_client: RedisClient, hygiene: IndexHygieneJob
    ) -> None:
        client = redis_client.client
        await client.sadd(INSTANCE_INDEX_KEY, "alive", "dead")
        await client.set(INSTANCE_REGISTRY_KEY.format(instance_id="alive"), "gateway", ex=10)
        for protocol in ("wireguard", "openvpn"):
            await client.sadd(TUNNEL_PEER_STATUS_INDEX_KEY.format(protocol=protocol), "gw-alive", "gw-dead")
            await client.hset(TUNNEL_PEER_STATUS_KEY.format(protocol=protocol, instance_id="gw-alive"), "k", "{}")

        await hygiene.run(ALL)

        assert await client.smembers(INSTANCE_INDEX_KEY) == {"alive"}
        for protocol in ("wireguard", "openvpn"):
            assert await client.smembers(TUNNEL_PEER_STATUS_INDEX_KEY.format(protocol=protocol)) == {"gw-alive"}

    async def test_with_nothing_to_tidy_it_writes_nothing_and_asks_for_no_decision(
        self, redis_client: RedisClient, hygiene: IndexHygieneJob
    ) -> None:
        await hygiene.run(ALL)

        assert await queued_job_keys(redis_client, JobKind.TRAFFIC) == []
        # No stream or group made for a work stream it only looked at.
        assert await redis_client.client.dbsize() == 0

    async def test_a_consumer_with_nothing_pending_is_removed_once_it_idled_past_the_limit(
        self, redis_client: RedisClient, hygiene: IndexHygieneJob, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(housekeeping, "CONSUMER_PRUNE_SECONDS", 0.2)
        client = redis_client.client
        health = JOB_STREAM_KEY.format(kind=JobKind.HEALTH)
        for stream in (health, SIGHTING_STREAM_KEY, OBSERVATION_STREAM_KEY):
            await client.xgroup_create(stream, WORKER_GROUP, id="0", mkstream=True)
            await client.xgroup_createconsumer(stream, WORKER_GROUP, "stopped")
        # A worker that stopped holding an entry: whoever reclaims it later takes it from there.
        await client.xadd(health, {"key": "proxy-1"})
        await client.xreadgroup(WORKER_GROUP, "busy", {health: ">"}, count=1)
        # A stream no worker joined yet, and the other work streams not there at all.
        sync = JOB_STREAM_KEY.format(kind=JobKind.SYNC)
        await client.xadd(sync, {"key": "connector-1"})
        await asyncio.sleep(0.3)
        # A running worker: its reads keep its idle time short, a fraction of the limit.
        await client.xreadgroup(WORKER_GROUP, "running", {health: ">"}, count=1)
        await asyncio.sleep(0.05)

        await hygiene.run(ALL)

        assert await consumer_names(redis_client, health) == {"busy", "running"}
        assert await consumer_names(redis_client, SIGHTING_STREAM_KEY) == set()
        assert await consumer_names(redis_client, OBSERVATION_STREAM_KEY) == set()
        assert (await client.xpending(health, WORKER_GROUP))["pending"] == 1
        assert await client.xinfo_groups(sync) == []

    async def test_a_stopped_consumer_stays_listed_for_an_hour(
        self, redis_client: RedisClient, hygiene: IndexHygieneJob
    ) -> None:
        """Long enough to read on the System page; a live worker's idle time never comes near it."""
        assert housekeeping.CONSUMER_PRUNE_SECONDS == 3600
        client = redis_client.client
        await client.xgroup_create(SIGHTING_STREAM_KEY, WORKER_GROUP, id="0", mkstream=True)
        await client.xgroup_createconsumer(SIGHTING_STREAM_KEY, WORKER_GROUP, "stopped")
        await asyncio.sleep(0.1)

        await hygiene.run(ALL)

        assert await consumer_names(redis_client, SIGHTING_STREAM_KEY) == {"stopped"}

    async def test_entries_dropped_with_a_consumer_are_logged_under_the_stream_s_name_in_the_system_view(
        self, hygiene: IndexHygieneJob, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a removal racing a read drops entries, so the removal is stood in for: two entries of the sync stream."""

        async def prune(group: StreamGroup, idle_ms: int) -> dict[str, int]:
            return {"stopped": 2} if group.key == JOB_STREAM_KEY.format(kind=JobKind.SYNC) else {}

        monkeypatch.setattr(StreamGroup, "prune_idle_consumers", prune)

        with structlog.testing.capture_logs() as logs:
            await hygiene.run(ALL)

        dropped = "Entries pending for a stopped consumer were dropped with it"
        assert [(log["stream"], log["consumer"], log["entries"]) for log in logs if log["event"] == dropped] == [
            ("jobs.sync", "stopped", 2)
        ]

    async def test_any_other_error_from_a_work_stream_fails_the_run(
        self, redis_client: RedisClient, hygiene: IndexHygieneJob
    ) -> None:
        """Left pending, the entry is delivered again and given up after five runs, logged each time."""
        await redis_client.client.set(SIGHTING_STREAM_KEY, "not a stream")

        with pytest.raises(ResponseError, match="WRONGTYPE"):
            await hygiene.run(ALL)

    async def test_ended_quarantines_are_trimmed_and_current_ones_kept(
        self, redis_client: RedisClient, hygiene: IndexHygieneJob
    ) -> None:
        now = time.time()
        await redis_client.client.zadd(QUARANTINE_KEY, {"ended": now - 5, "current": now + 300})

        await hygiene.run(ALL)

        assert await redis_client.client.zrange(QUARANTINE_KEY, 0, -1) == ["current"]

    async def test_a_traffic_decision_is_asked_for_every_blocked_connector_ended_blocks_included(
        self, redis_client: RedisClient, hygiene: IndexHygieneJob
    ) -> None:
        """A lift that was lost, or decided before an edit, holds a block for a minute at most."""
        blocks = {
            "blocked": (utc_now() + timedelta(days=3)).isoformat(),
            "ended": (utc_now() - timedelta(days=1)).isoformat(),
        }
        await redis_client.client.hset(TRAFFIC_BLOCKED_KEY, mapping=blocks)

        await hygiene.run(ALL)

        assert sorted(await queued_job_keys(redis_client, JobKind.TRAFFIC)) == ["blocked", "ended"]
        # Deciding is the traffic job's: the blocks stay as they were.
        assert await redis_client.client.hgetall(TRAFFIC_BLOCKED_KEY) == blocks

    async def test_a_traffic_decision_is_asked_for_every_connector_waiting_for_one_once_each(
        self, redis_client: RedisClient, hygiene: IndexHygieneJob
    ) -> None:
        """A connector both blocked and waiting is asked for once; the marks are the traffic job's to settle."""
        marks = {"waiting": "2026-10-20T09:15:02.123456", "both": "2026-10-20T09:15:03"}
        await redis_client.client.hset(TRAFFIC_PENDING_KEY, mapping=marks)
        await redis_client.client.hset(TRAFFIC_BLOCKED_KEY, "both", (utc_now() + timedelta(days=3)).isoformat())

        await hygiene.run(ALL)

        assert sorted(await queued_job_keys(redis_client, JobKind.TRAFFIC)) == ["both", "waiting"]
        assert await redis_client.client.hgetall(TRAFFIC_PENDING_KEY) == marks

    async def test_a_connector_whose_decision_was_lost_stops_waiting_once_the_decision_hygiene_asked_for_runs(
        self,
        redis_client: RedisClient,
        hygiene: IndexHygieneJob,
        sessions: async_sessionmaker[AsyncSession],
        rows: Rows,
    ) -> None:
        """Its run-now entry given up or trimmed away: the mark stays until hygiene's own request is decided."""
        connector = await rows.connector(await rows.project(), traffic_config={"limit_bytes": 1000, "action": "block"})
        await redis_client.client.hset(TRAFFIC_PENDING_KEY, connector.id, utc_now().isoformat())

        await hygiene.run(ALL)
        keys = await queued_job_keys(redis_client, JobKind.TRAFFIC)
        await TrafficJob(sessions, redis_client).run(JobBatch(ids=("1-0",), keys=tuple(keys)))

        assert keys == [connector.id]
        assert await redis_client.client.exists(TRAFFIC_PENDING_KEY) == 0
