# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The install-wide lines of Needs attention, read off heartbeats written as live processes write them, the streams'
entries stamped on Redis's clock, and the IP database rows in Postgres."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.attention import install
from api.attention.install import InstallAttentionReader
from api.core import utc_now
from api.core.roles import JobKind
from api.core.system_stats import ClusterReader, ClusterReading
from api.core.workers import LeaseName
from api.db.geo_repository import GeoDatabaseRepository
from api.db.redis import (
    CHANGES_STREAM_KEY,
    EXIT_REPORT_STREAM_KEY,
    FEED_HEAD_KEY,
    FEED_STREAM_KEY,
    JOB_STREAM_KEY,
    LEASE_KEY,
    OBSERVATION_STREAM_KEY,
    WORKER_GROUP,
    RedisClient,
)
from api.feed.models import FeedHead
from api.geo.models import GeoDatabaseRecord, GeoDatabaseSource
from api.models.attention import InstallAttentionLine, Severity
from api.models.system import (
    ControllerSnapshot,
    InstanceSnapshot,
    JobKindStats,
    WorkerSnapshot,
)
from tests.routes.gateway_heartbeats import gateway_heartbeat

VERSION = "6.9.0"
DIGEST = "digest-1"
HYGIENE = JOB_STREAM_KEY.format(kind=JobKind.INDEX_HYGIENE)


def snapshot(
    instance_id: str,
    role: str = "all",
    *,
    uptime: float = 600.0,
    version: str = VERSION,
    reasons: Sequence[str] = (),
    gateway: dict[str, Any] | None = None,
    serves: bool = True,
    controller: ControllerSnapshot | None = None,
    worker: WorkerSnapshot | None = None,
) -> InstanceSnapshot:
    """A process's heartbeat: a gateway section when it ``serves`` (its fields over an idle one's), and the others given."""
    base = InstanceSnapshot.model_validate_json(gateway_heartbeat(instance_id, **({"feed_digest": DIGEST} | (gateway or {}))))
    return InstanceSnapshot.model_validate({
        "runtime": {"version": version, "role": role, "instance_id": instance_id, "pid": 1, "uptime_seconds": uptime},
        "ready": not reasons,
        "reasons": list(reasons),
        "gateway": base.gateway.model_dump() if serves and base.gateway is not None else None,
        "controller": controller.model_dump() if controller is not None else None,
        "worker": worker.model_dump() if worker is not None else None,
    })


def taking(*kinds: JobKind, failing: dict[JobKind, int] | None = None) -> WorkerSnapshot:
    """A worker taking ``kinds`` (every kind when none), with runs failed in a row as given."""
    failing = failing or {}
    return WorkerSnapshot(
        jobs={kind: JobKindStats(consecutive_failures=failing.get(kind, 0)) for kind in kinds or tuple(JobKind)}
    )


def _ms(at: datetime) -> int:
    """A naive UTC time as a stream entry's milliseconds."""
    return round(at.replace(tzinfo=UTC).timestamp() * 1000)


class Cluster:
    """Heartbeats, the lease and the streams in the test Redis, and the reader over them."""

    def __init__(self, redis_client: RedisClient, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.redis = redis_client
        self.client = redis_client.client
        self.sessions = sessions

    async def now_ms(self) -> int:
        seconds, microseconds = await self.client.time()
        return int(seconds) * 1000 + int(microseconds) // 1000

    async def publish(self, instance_id: str, role: str, beat: InstanceSnapshot | str) -> None:
        """One heartbeat, as a live process writes it; a string is written as it is (one this release cannot read)."""
        payload = beat if isinstance(beat, str) else beat.model_dump_json()
        await self.redis.advertise_instance(instance_id, role, payload)

    async def lead(self, instance_id: str | None) -> None:
        key = LEASE_KEY.format(name=LeaseName.CONTROLLER)
        if instance_id is None:
            await self.client.delete(key)
        else:
            await self.client.set(key, instance_id)

    async def add(self, key: str, ago: float, **fields: str) -> str:
        """An entry ``ago`` seconds old on Redis's clock."""
        entry_id = f"{await self.now_ms() - int(ago * 1000)}-0"
        await self.client.xadd(key, fields or {"key": "all"}, id=entry_id)
        return entry_id

    async def head(self, digest: str = DIGEST) -> None:
        head = FeedHead(schema_version=1, digest=digest, changes=None, compiled_at=utc_now(), sections=1)
        await self.client.set(FEED_HEAD_KEY, head.model_dump_json())

    async def healthy(self, *, changes: str | None = None) -> str:
        """One role-all process serving, leading with every change compiled and taking every kind; the scheduling
        appended its minutely job just now. The change it compiled up to."""
        compiled = changes or await self.add(CHANGES_STREAM_KEY, 120, kind="proxy")
        await self.add(HYGIENE, 5)
        await self.head()
        await self.lead("node-1")
        await self.publish(
            "node-1",
            "all",
            snapshot(
                "node-1",
                controller=ControllerSnapshot(is_leader=True, lease_terms=1, changes=compiled),
                worker=taking(),
            ),
        )
        return compiled

    async def lines(self, *, geo_updater_interval: float = 3600.0) -> dict[str, InstallAttentionLine]:
        reader = InstallAttentionReader(
            self.redis, self.sessions, ClusterReader(self.redis), geo_updater_interval=geo_updater_interval
        )
        answer = await reader.read()
        return {line.key: line for line in answer.lines}

    async def database(self, **fields: Any) -> GeoDatabaseRecord:
        record = GeoDatabaseRecord(name=fields.pop("name", "City"), **fields)
        async with self.sessions() as session:
            await GeoDatabaseRepository(session).create(record, None)
            await session.commit()
        return record


@pytest.fixture
async def cluster(
    redis_client: RedisClient, db_session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> AsyncIterator[Cluster]:
    yield Cluster(redis_client, db_session_factory)


async def test_a_healthy_install_has_no_line(cluster: Cluster) -> None:
    await cluster.healthy()

    assert await cluster.lines() == {}


class TestGateways:
    async def test_no_gateway_serving_is_a_problem(self, cluster: Cluster) -> None:
        await cluster.healthy()
        await cluster.publish("gw-1", "gateway", snapshot("gw-1", "gateway", gateway={"listening": False}))
        await cluster.publish("gw-2", "gateway", snapshot("gw-2", "gateway", reasons=["gateway: proxy listener not bound"]))
        await cluster.publish("node-1", "all", snapshot("node-1", reasons=["stopping"], worker=taking()))

        line = (await cluster.lines())["no_gateway_serving"]

        assert (line.severity, line.admin_page, line.at) == (Severity.PROBLEM, "system", None)
        assert line.text == "No gateway is accepting proxy connections right now, so requests through Octoprox fail."

    async def test_no_gateway_process_at_all(self, cluster: Cluster) -> None:
        await cluster.healthy()
        await cluster.publish("node-1", "controller", snapshot("node-1", "controller", serves=False))

        assert "no_gateway_serving" in await cluster.lines()

    async def test_a_gateway_whose_heartbeat_cannot_be_read_says_nothing_is_known(self, cluster: Cluster) -> None:
        """A newer release's heartbeat during an upgrade: it may well serve."""
        await cluster.healthy()
        await cluster.publish("node-1", "all", snapshot("node-1", serves=False, worker=taking()))
        await cluster.publish("gw-new", "gateway", '{"runtime": "from a newer release"}')

        assert "no_gateway_serving" not in await cluster.lines()

    async def test_gateways_starting_have_two_minutes(self, cluster: Cluster) -> None:
        await cluster.healthy()
        await cluster.publish("node-1", "all", snapshot("node-1", serves=False, worker=taking(), uptime=600))
        await cluster.publish("gw-1", "gateway", snapshot("gw-1", "gateway", reasons=["starting"], uptime=30))

        assert "no_gateway_serving" not in await cluster.lines()

        await cluster.publish("gw-1", "gateway", snapshot("gw-1", "gateway", reasons=["starting"], uptime=120))
        assert "no_gateway_serving" in await cluster.lines()

    async def test_a_process_without_its_database_still_serves_and_says_the_database_is_away(
        self, cluster: Cluster
    ) -> None:
        compiled = await cluster.healthy()
        await cluster.publish(
            "node-1",
            "all",
            snapshot(
                "node-1",
                reasons=["Postgres unreachable", "controller: Postgres unreachable"],
                controller=ControllerSnapshot(
                    is_leader=True, lease_terms=1, changes=compiled, compile_error="connection refused"
                ),
                worker=taking(failing={JobKind.HEALTH: 9}),
            ),
        )
        await cluster.add(CHANGES_STREAM_KEY, 70, kind="proxy")

        lines = await cluster.lines()

        # The database line says it all: the changes and the background work wait on it.
        assert list(lines) == ["store_unreachable:database"]
        assert lines["store_unreachable:database"].text == (
            "Octoprox cannot reach its database. Proxy traffic keeps flowing, but changes cannot be saved and proxy "
            "health and charts are not updating."
        )


class TestChanges:
    async def test_no_leader(self, cluster: Cluster) -> None:
        await cluster.healthy()
        await cluster.lead(None)

        line = (await cluster.lines())["changes_not_applied"]

        assert (line.severity, line.at) == (Severity.WARNING, None)
        assert line.text == "Recent changes have not taken effect yet. Requests still follow the earlier setup."

    async def test_a_leader_failing_to_publish(self, cluster: Cluster) -> None:
        compiled = await cluster.healthy()
        await cluster.publish(
            "node-1",
            "all",
            snapshot(
                "node-1",
                controller=ControllerSnapshot(is_leader=True, lease_terms=1, changes=compiled, publish_error="OOM"),
                worker=taking(),
            ),
        )

        assert (await cluster.lines())["changes_not_applied"].at is None

    @pytest.mark.parametrize(("ago", "shown"), [(50, False), (65, True)])
    async def test_changes_the_leader_has_not_compiled_for_a_minute(
        self, cluster: Cluster, ago: float, shown: bool
    ) -> None:
        await cluster.healthy()
        waiting = await cluster.add(CHANGES_STREAM_KEY, ago, kind="connector")
        await cluster.add(CHANGES_STREAM_KEY, 1, kind="connector")

        lines = await cluster.lines()

        assert ("changes_not_applied" in lines) is shown
        if shown:
            line = lines["changes_not_applied"]
            assert line.text == "Changes since {time} have not taken effect yet. Requests still follow the earlier setup."
            # The oldest change waiting, as naive UTC.
            assert line.at is not None and _ms(line.at) == int(waiting.split("-")[0])

    async def test_a_gateway_of_the_leader_s_release_behind_on_the_feed(self, cluster: Cluster) -> None:
        await cluster.healthy()
        await cluster.add(FEED_STREAM_KEY, 400, digest="digest-0")
        await cluster.add(FEED_STREAM_KEY, 300, digest=DIGEST)
        missed = await cluster.add(FEED_STREAM_KEY, 90, digest="digest-2")
        await cluster.add(FEED_STREAM_KEY, 10, digest="digest-3")
        await cluster.head("digest-3")
        # The leader's own gateway applied the head; another of its release did not, one of an older release neither.
        await cluster.publish(
            "node-1",
            "all",
            snapshot(
                "node-1",
                gateway={"feed_digest": "digest-3"},
                controller=ControllerSnapshot(is_leader=True, lease_terms=1),
                worker=taking(),
            ),
        )
        await cluster.publish("gw-1", "gateway", snapshot("gw-1", "gateway"))
        await cluster.publish("gw-old", "gateway", snapshot("gw-old", "gateway", version="6.8.0", gateway={"feed_digest": "digest-0"}))

        line = (await cluster.lines())["changes_not_applied"]

        # The first publish the gateway missed: the one after the last carrying its digest.
        assert line.at is not None and _ms(line.at) == int(missed.split("-")[0])

    async def test_a_gateway_whose_digest_the_stream_no_longer_keeps_missed_its_oldest_entry(
        self, cluster: Cluster
    ) -> None:
        await cluster.healthy()
        await cluster.add(FEED_STREAM_KEY, 200, digest="digest-8")
        await cluster.add(FEED_STREAM_KEY, 5, digest="digest-9")
        await cluster.head("digest-9")
        await cluster.publish(
            "node-1",
            "all",
            snapshot(
                "node-1",
                gateway={"feed_digest": "digest-9"},
                controller=ControllerSnapshot(is_leader=True, lease_terms=1),
                worker=taking(),
            ),
        )
        await cluster.publish("gw-1", "gateway", snapshot("gw-1", "gateway", gateway={"feed_digest": "digest-1"}))

        line = (await cluster.lines())["changes_not_applied"]

        assert line.at is not None and abs((utc_now() - line.at).total_seconds() - 200) < 5


class TestBackgroundWork:
    async def test_work_nobody_takes(self, cluster: Cluster) -> None:
        await cluster.healthy()
        await cluster.publish(
            "node-1", "all",
            snapshot("node-1", controller=ControllerSnapshot(is_leader=True, lease_terms=1),
                     worker=taking(*(kind for kind in JobKind if kind not in (JobKind.HEALTH, JobKind.PROVISION)))),
        )
        await cluster.add(JOB_STREAM_KEY.format(kind=JobKind.HEALTH), 95)
        # Queued a moment ago: not behind yet.
        await cluster.add(JOB_STREAM_KEY.format(kind=JobKind.PROVISION), 30)

        line = (await cluster.lines())["background_behind"]

        assert line.text == "Background work is behind: proxy health checks is not up to date."

    async def test_a_group_that_took_work_lately_is_not_behind_yet(self, cluster: Cluster) -> None:
        """The workers left a moment ago: the newest entry the group took counts, not the oldest one still there."""
        await cluster.healthy()
        await cluster.publish(
            "node-1", "all",
            snapshot("node-1", controller=ControllerSnapshot(is_leader=True, lease_terms=1), worker=taking(JobKind.SYNC)),
        )
        stream = JOB_STREAM_KEY.format(kind=JobKind.HEALTH)
        await cluster.add(stream, 100)
        await cluster.add(stream, 2)
        await cluster.client.xgroup_create(stream, WORKER_GROUP, id="0")
        await cluster.client.xreadgroup(WORKER_GROUP, "worker-1", {stream: ">"}, count=2)
        await cluster.add(stream, 1)

        assert "background_behind" not in await cluster.lines()

    @pytest.mark.parametrize(
        ("kind", "seconds", "area"),
        [
            (JobKind.HEALTH, 180, "proxy health checks"),
            (JobKind.SYNC, 900, "vendor proxy lists"),
            (JobKind.SCALE, 900, "cloud scaling"),
            (JobKind.PROVISION, 120, "new countries"),
            (JobKind.TRAFFIC, 180, "traffic limits"),
            (JobKind.METRICS_FLUSH, 300, "charts"),
            (JobKind.REATTRIBUTE, 600, "location checks"),
            (JobKind.ENRICH, 600, "location checks"),
        ],
    )
    async def test_work_waiting_too_long(self, cluster: Cluster, kind: JobKind, seconds: int, area: str) -> None:
        await cluster.healthy()
        stream = JOB_STREAM_KEY.format(kind=kind)
        await cluster.add(stream, seconds - 5)
        assert "background_behind" not in await cluster.lines()

        await cluster.client.delete(stream)
        await cluster.add(stream, seconds + 5)
        assert (await cluster.lines())["background_behind"].text == f"Background work is behind: {area} is not up to date."

    @pytest.mark.parametrize(
        ("key", "area"), [(OBSERVATION_STREAM_KEY, "location checks"), (EXIT_REPORT_STREAM_KEY, "exit reports")]
    )
    async def test_ingest_waiting_too_long_or_with_no_worker(self, cluster: Cluster, key: str, area: str) -> None:
        await cluster.healthy()
        await cluster.add(key, 700, item="{}")
        assert area in (await cluster.lines())["background_behind"].text

        await cluster.client.delete(key)
        await cluster.add(key, 100, item="{}")
        await cluster.publish("node-1", "all", snapshot("node-1", controller=ControllerSnapshot(is_leader=True, lease_terms=1)))
        assert area in (await cluster.lines())["background_behind"].text

    async def test_runs_failing_in_a_row_but_a_vendor_refusing_a_sync(self, cluster: Cluster) -> None:
        await cluster.healthy()
        await cluster.publish(
            "node-1", "all",
            snapshot(
                "node-1",
                controller=ControllerSnapshot(is_leader=True, lease_terms=1),
                worker=taking(failing={JobKind.SYNC: 5, JobKind.SCALE: 3, JobKind.TRAFFIC: 2}),
            ),
        )

        line = (await cluster.lines())["background_behind"]

        assert line.text == "Background work is behind: cloud scaling is not up to date."

    async def test_scheduling_silent_for_three_minutes_stops_the_scheduled_work(self, cluster: Cluster) -> None:
        await cluster.healthy()
        await cluster.client.delete(HYGIENE)
        await cluster.add(HYGIENE, 170)
        assert "background_behind" not in await cluster.lines()

        await cluster.client.delete(HYGIENE)
        await cluster.add(HYGIENE, 185)
        line = (await cluster.lines())["background_behind"]
        assert line.text == (
            "Background work is behind: proxy health checks, vendor proxy lists, cloud scaling, traffic limits and "
            "charts are not up to date."
        )

    async def test_no_scheduling_stream_without_a_leader(self, cluster: Cluster) -> None:
        await cluster.healthy()
        await cluster.client.delete(HYGIENE)
        assert "background_behind" not in await cluster.lines()

        await cluster.lead(None)
        assert "proxy health checks" in (await cluster.lines())["background_behind"].text


class TestIpDatabases:
    async def test_a_database_a_gateway_could_not_load(self, cluster: Cluster) -> None:
        await cluster.healthy()
        settled = await cluster.database(updated_at=utc_now() - timedelta(minutes=10))
        fresh = await cluster.database(name="Fresh", updated_at=utc_now() - timedelta(minutes=1))
        off = await cluster.database(name="Off", enabled=False, updated_at=utc_now() - timedelta(hours=1))
        broken = {"geo_database_errors": {fresh.id: "bad file", off.id: "bad file"}, "geo_databases_missing": []}

        await cluster.publish("node-1", "all", snapshot("node-1", gateway=broken, worker=taking(),
                                                         controller=ControllerSnapshot(is_leader=True, lease_terms=1)))
        assert "ip_databases" not in await cluster.lines()

        broken["geo_databases_missing"] = [settled.id]
        await cluster.publish("node-1", "all", snapshot("node-1", gateway=broken, worker=taking(),
                                                         controller=ControllerSnapshot(is_leader=True, lease_terms=1)))
        line = (await cluster.lines())["ip_databases"]
        assert (line.text, line.admin_page) == (
            "An IP database could not be loaded, so location checks may be less accurate.", "geo"
        )

        # A gateway up under five minutes may still be fetching it.
        await cluster.publish("node-1", "all", snapshot("node-1", gateway=broken, uptime=200, worker=taking(),
                                                         controller=ControllerSnapshot(is_leader=True, lease_terms=1)))
        assert "ip_databases" not in await cluster.lines()

    async def test_a_database_of_the_config_file_counts_by_the_gateway_s_uptime(self, cluster: Cluster) -> None:
        await cluster.healthy()
        await cluster.publish("node-1", "all", snapshot(
            "node-1", gateway={"geo_database_errors": {"config-city": "no such file"}}, worker=taking(),
            controller=ControllerSnapshot(is_leader=True, lease_terms=1),
        ))

        assert "ip_databases" in await cluster.lines()

    async def test_an_update_failing_or_late(self, cluster: Cluster) -> None:
        await cluster.healthy()
        now = utc_now()
        scheduled = {"source": GeoDatabaseSource.URL, "update_url": "https://example.test/db", "update_interval_hours": 24}
        await cluster.database(name="Fine", last_update_at=now - timedelta(hours=20), **scheduled)
        await cluster.database(name="Paused", update_paused=True, last_update_error="401", **scheduled)
        assert "ip_databases" not in await cluster.lines()

        failing = await cluster.database(name="Failing", last_update_at=now - timedelta(hours=2),
                                         last_update_error="401 Unauthorized", **scheduled)
        line = (await cluster.lines())["ip_databases"]
        assert line.text == "An IP database could not be updated, so location data may be getting older."

        async with cluster.sessions() as session:
            await GeoDatabaseRepository(session).delete(failing.id)
            await session.commit()
        # Due 2 hours ago, looked at every hour: a look was missed.
        await cluster.database(name="Late", last_update_at=now - timedelta(hours=26), **scheduled)
        assert "ip_databases" in await cluster.lines()
        assert "ip_databases" not in await cluster.lines(geo_updater_interval=4 * 3600)

    async def test_both_at_once(self, cluster: Cluster) -> None:
        await cluster.healthy()
        await cluster.database(name="Failing", source=GeoDatabaseSource.URL, update_url="https://example.test/db",
                               update_interval_hours=24, last_update_error="401")
        await cluster.publish("node-1", "all", snapshot(
            "node-1", gateway={"geo_databases_missing": ["config-city"]}, worker=taking(),
            controller=ControllerSnapshot(is_leader=True, lease_terms=1),
        ))

        assert (await cluster.lines())["ip_databases"].text == (
            "IP databases could not be loaded or updated, so location checks may be less accurate."
        )


class TestRedisAway:
    async def test_a_reading_that_failed_is_that_line_alone(
        self, cluster: Cluster, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await cluster.healthy()

        async def refused(self: ClusterReader) -> ClusterReading:
            return ClusterReading(error="Connection refused")

        monkeypatch.setattr(ClusterReader, "read", refused)

        lines = await cluster.lines()

        assert list(lines) == ["store_unreachable:live_data"]
        assert lines["store_unreachable:live_data"].text == (
            "Octoprox cannot reach its live data store. Requests still go through but may be slower, and live numbers "
            "may be missing."
        )

    async def test_a_reading_that_takes_too_long(self, cluster: Cluster, monkeypatch: pytest.MonkeyPatch) -> None:
        await cluster.healthy()
        slow = ClusterReader.streams

        async def stalled(self: ClusterReader) -> Any:
            await asyncio.sleep(1)
            return await slow(self)

        monkeypatch.setattr(ClusterReader, "streams", stalled)
        monkeypatch.setattr(install, "INSTALL_READ_TIMEOUT_SECONDS", 0.05)

        assert list(await cluster.lines()) == ["store_unreachable:live_data"]
