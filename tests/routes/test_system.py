# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The admin System view: the cluster as Postgres and Redis describe it, and the trend history.

The app is a role ``api`` process, and the cluster's other processes are played by writing what they would write to
Redis (heartbeats, the lease, the feed head, change entries, work stream entries) through its own client.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from redis.asyncio.client import Pipeline
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from api import __version__
from api.core import system_stats, utc_now
from api.core.config import Settings
from api.core.roles import JobKind, Role
from api.core.system_stats import ClusterReader, ClusterReading, SystemReader
from api.core.workers import LeaseName
from api.db.models import SystemMetricsModel
from api.db.redis import (
    CHANGES_STREAM_KEY,
    EARLIER_RELEASES_KEY_GROUP,
    FEED_HEAD_KEY,
    INSTANCE_TTL_SECONDS,
    JOB_STREAM_KEY,
    LEASE_KEY,
    OBSERVATION_STREAM_KEY,
    SIGHTING_STREAM_KEY,
    WORK_STREAMS,
    WORKER_GROUP,
    RedisClient,
)
from api.feed.models import FEED_SCHEMA, FeedHead
from api.models.proxy import ProxyCreate, ProxyStatus
from api.models.system import (
    ControllerSnapshot,
    GatewaySnapshot,
    InstanceInfo,
    InstanceRuntime,
    InstanceSnapshot,
    JobKindStats,
    StreamState,
    WorkerSnapshot,
    WorkerTask,
    WorkStream,
)
from api.services.proxies import StatusMove
from tests.conftest import api_runtime, on_app_loop

ENDPOINT = "/api/v1/system/stats"
TOPOLOGY = "/api/v1/system/topology"
HISTORY = "/api/v1/system/stats/history"

# Positions on the changes stream (entry IDs: milliseconds since the epoch,
# then a sequence): what a compile read through, and an earlier one.
COMPILED_THROUGH = "1792281600000-0"
EARLIER = "1792281300000-0"


@pytest.mark.parametrize("endpoint", [ENDPOINT, TOPOLOGY])
class TestSystemStatsAccessControl:
    def test_unauthenticated_is_rejected(self, async_client: TestClient, endpoint: str) -> None:
        assert async_client.get(endpoint).status_code == 401

    def test_viewer_is_forbidden(self, viewer_client: TestClient, endpoint: str) -> None:
        assert viewer_client.get(endpoint).status_code == 403

    def test_editor_is_forbidden(self, editor_client: TestClient, endpoint: str) -> None:
        assert editor_client.get(endpoint).status_code == 403

    def test_admin_is_allowed(self, authenticated_client: TestClient, endpoint: str) -> None:
        assert authenticated_client.get(endpoint).status_code == 200


# --- Playing the rest of the cluster ------------------------------------------------------------


class Cluster:
    """Writes what other processes put in Redis, through the app's client on its loop; ``clear`` takes it back."""

    def __init__(self, client: TestClient) -> None:
        self._client = client
        self._redis = api_runtime(client).redis_client
        self._instances: list[str] = []
        self._keys: set[str] = set()

    def heartbeat(self, instance_id: str, role: str, snapshot: InstanceSnapshot | str | None) -> None:
        """One heartbeat of a process: its role, and its snapshot (a model, raw text, or none)."""
        payload = snapshot.model_dump_json() if isinstance(snapshot, InstanceSnapshot) else snapshot
        on_app_loop(self._client, self._redis.advertise_instance, instance_id, role, payload)
        self._instances.append(instance_id)

    def lease(self, holder: str | None) -> None:
        """The controller lease, held by ``holder``, or by nobody."""
        key = LEASE_KEY.format(name=LeaseName.CONTROLLER)
        self._keys.add(key)
        if holder is None:
            on_app_loop(self._client, self._redis.client.delete, key)
        else:
            on_app_loop(self._client, lambda: self._redis.client.set(key, holder, px=60_000))

    def head(self, head: FeedHead) -> None:
        """The routing feed's head, as the active controller publishes it."""
        self._keys.add(FEED_HEAD_KEY)
        on_app_loop(self._client, self._redis.client.set, FEED_HEAD_KEY, head.model_dump_json())

    def change(self) -> str:
        """One entry on the changes stream, as a process appends it after a commit; its ID."""
        self._keys.add(CHANGES_STREAM_KEY)
        return str(on_app_loop(self._client, lambda: self._redis.client.xadd(CHANGES_STREAM_KEY, {"kind": "proxy"})))

    def no_changes_stream(self) -> None:
        """No changes stream at all, as after Redis started empty and before any process announced a change."""
        self._keys.add(CHANGES_STREAM_KEY)
        on_app_loop(self._client, self._redis.client.delete, CHANGES_STREAM_KEY)

    def key(self, key: str) -> None:
        """Any key, holding a string, as some other writer left it."""
        self._keys.add(key)
        on_app_loop(self._client, self._redis.client.set, key, "1")

    def job_stream(self, kind: JobKind, entries: int, *, delivered: int) -> None:
        """``kind``'s job stream holding ``entries`` entries, the first ``delivered`` of them taken by a worker of the group."""
        key = JOB_STREAM_KEY.format(kind=kind)
        client = self._redis.client
        self._keys.add(key)
        on_app_loop(self._client, client.delete, key)
        for n in range(entries):
            on_app_loop(self._client, client.xadd, key, {"key": f"key-{n}"})
        on_app_loop(self._client, client.xgroup_create, key, WORKER_GROUP, "0")
        on_app_loop(self._client, client.xreadgroup, WORKER_GROUP, "worker-1", {key: ">"}, delivered)

    def clear(self) -> None:
        for instance_id in self._instances:
            on_app_loop(self._client, self._redis.withdraw_instance, instance_id)
        if self._keys:
            on_app_loop(self._client, self._redis.client.delete, *self._keys)


@pytest.fixture
def cluster(authenticated_client: TestClient) -> Iterator[Cluster]:
    played = Cluster(authenticated_client)
    try:
        yield played
    finally:
        played.clear()


def stats_when(
    client: TestClient,
    condition: Callable[[dict[str, Any]], bool],
    timeout: float = 10.0,
    endpoint: str = ENDPOINT,
) -> dict[str, Any]:
    """The System view once ``condition`` holds for it: the app's own heartbeat runs on its own loop."""
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(endpoint)
        assert response.status_code == 200
        data: dict[str, Any] = response.json()
        if condition(data):
            return data
        assert time.monotonic() < deadline, "the System view never showed it"
        time.sleep(0.1)


def instance_of(data: dict[str, Any], role: str, instance_id: str) -> dict[str, Any] | None:
    return next((i for i in data["instances"].get(role, []) if i["instance_id"] == instance_id), None)


def key_groups(data: dict[str, Any]) -> set[str]:
    """The groups the Redis keyspace breakdown names."""
    return {group["label"] for group in data["redis"]["groups"]}


def lists(role: str, instance_id: str) -> Callable[[dict[str, Any]], bool]:
    return lambda data: instance_of(data, role, instance_id) is not None


def runtime(instance_id: str, role: Role) -> InstanceRuntime:
    return InstanceRuntime(version=__version__, role=role, instance_id=instance_id, pid=4242, uptime_seconds=60.0)


def gateway_section(*, digest: str | None, changes: str | None = None, age: float | None = None) -> GatewaySnapshot:
    """A gateway's heartbeat section, with the feed it applied (digest None: none yet)."""
    return GatewaySnapshot(
        listener_port=8080,
        listening=True,
        active_connections=3,
        feed_digest=digest,
        feed_changes=changes,
        feed_applied_at=None if digest is None else utc_now(),
        feed_age_seconds=age,
        retired_slots=0,
        local_quarantines=1,
        in_flight=2,
        requests=10,
        successes=9,
        failures=1,
        bytes_sent=100,
        bytes_received=1000,
        failures_by_kind={"upstream_timeout": 1},
        pending_metric_deltas=0,
        metric_flush_failures=0,
        rate_limit_errors=0,
        preflight_checks=4,
        preflight_rejections=1,
        geo_databases_loaded=["geo-1"],
        geo_database_errors={},
        geo_databases_missing=[],
        observations_pending=0,
        observations_dropped=0,
        sightings_pending=0,
        sightings_dropped=0,
        failure_log_dropped=0,
        mitm_captures_dropped=0,
        tunnels=[],
    )


def gateway(instance_id: str, **feed: Any) -> InstanceSnapshot:
    return InstanceSnapshot(
        runtime=runtime(instance_id, Role.GATEWAY),
        ready=feed.get("digest") is not None,
        reasons=[] if feed.get("digest") is not None else ["gateway: feed not applied yet"],
        tasks=[WorkerTask(name="feed_follower", state="running", runs=5)],
        gateway=gateway_section(**feed),
    )


def controller(
    instance_id: str, *, leading: bool, changes: str | None = None, compiled_at: datetime | None = None
) -> InstanceSnapshot:
    return InstanceSnapshot(
        runtime=runtime(instance_id, Role.CONTROLLER),
        ready=True,
        controller=ControllerSnapshot(
            is_leader=leading,
            lease_terms=1 if leading else 0,
            changes=changes,
            compiled_at=compiled_at,
            compiles=3 if leading else 0,
        ),
    )


def worker(instance_id: str) -> InstanceSnapshot:
    return InstanceSnapshot(
        runtime=runtime(instance_id, Role.WORKER),
        ready=True,
        tasks=[WorkerTask(name="job_runner_health", state="running", runs=12, idle_runs=2)],
        streams={
            "changes": StreamState(
                position=COMPILED_THROUGH, lag_seconds=0.4, entries=3, full_reads={"start": 1, "gap": 1}
            ),
        },
        worker=WorkerSnapshot(
            jobs={JobKind.HEALTH: JobKindStats(runs=12, failures=1, duration_seconds=3.5, in_flight=4)},
            sightings_consumed=9,
        ),
    )


def head(digest: str, changes: str | None = COMPILED_THROUGH) -> FeedHead:
    return FeedHead(schema_version=FEED_SCHEMA, digest=digest, changes=changes, compiled_at=utc_now(), sections=3)


# --- The cluster -----------------------------------------------------------------------------


class TestCluster:
    """Every process from its heartbeat, grouped by role; nothing about the process that answered."""

    def test_nothing_describes_the_process_that_answered(self, authenticated_client: TestClient) -> None:
        data = authenticated_client.get(ENDPOINT).json()

        assert set(data) == {
            "generated_at",
            "instances",
            "controller",
            "feed",
            "streams",
            "inventory",
            "projects",
            "database",
            "redis",
        }
        # Each api process's own pool is in its snapshot, not in the database section.
        assert "pool_size" not in data["database"] and "pool_checked_out" not in data["database"]

    def test_the_api_lists_itself_from_its_heartbeat(self, authenticated_client: TestClient) -> None:
        node = authenticated_client.app.state.node  # type: ignore[attr-defined]
        data = stats_when(authenticated_client, lists("api", node.instance_id))

        me = instance_of(data, "api", node.instance_id)
        assert me is not None and me["role"] == "api"
        snapshot = me["snapshot"]
        assert snapshot["runtime"]["role"] == "api" and snapshot["runtime"]["instance_id"] == node.instance_id
        assert snapshot["runtime"]["pid"] > 0 and snapshot["runtime"]["version"] == __version__
        assert snapshot["ready"] is True and snapshot["reasons"] == []
        assert snapshot["api"]["pool_size"] == 5
        assert (snapshot["gateway"], snapshot["controller"], snapshot["worker"]) == (None, None, None)
        # Published on a 5 s heartbeat with a 10 s TTL, so never older than the TTL.
        assert 0 <= me["age_seconds"] <= INSTANCE_TTL_SECONDS
        # The loops an api process runs, every one catalogued (described) and alive.
        tasks = {task["name"]: task for task in snapshot["tasks"]}
        assert set(tasks) == {
            "heartbeat",
            "loop_lag_monitor",
            "geo_observation_publisher",
            "geo_database_retry",
            "definitions_refresher",
        }
        assert all(task["description"] and task["state"] == "running" for task in tasks.values())
        assert all(0 <= task["idle_runs"] <= task["runs"] for task in tasks.values())
        assert tasks["heartbeat"]["interval_seconds"] == 5

    def test_every_role_is_listed_with_its_section(self, authenticated_client: TestClient, cluster: Cluster) -> None:
        cluster.heartbeat("gw-b", "gateway", gateway("gw-b", digest="d1", changes=COMPILED_THROUGH, age=2.0))
        cluster.heartbeat("gw-a", "gateway", gateway("gw-a", digest=None))
        cluster.heartbeat("ctl-1", "controller", controller("ctl-1", leading=True, changes=COMPILED_THROUGH))
        cluster.heartbeat("wrk-1", "worker", worker("wrk-1"))

        data = stats_when(authenticated_client, lists("worker", "wrk-1"))

        instances = data["instances"]
        # Roles in the Role order, each group sorted by instance id.
        order = [role.value for role in Role]
        listed = [role for role in instances if role in order]
        assert {"api", "gateway", "controller", "worker"} <= set(listed)
        assert listed == sorted(listed, key=order.index)
        assert [i["instance_id"] for i in instances["gateway"] if i["instance_id"] in ("gw-a", "gw-b")] == [
            "gw-a",
            "gw-b",
        ]
        gw = instance_of(data, "gateway", "gw-b")
        assert gw is not None and gw["snapshot"]["gateway"]["requests"] == 10
        assert gw["snapshot"]["gateway"]["failures_by_kind"] == {"upstream_timeout": 1}
        assert gw["snapshot"]["gateway"]["feed_changes"] == COMPILED_THROUGH
        assert gw["snapshot"]["tasks"][0]["name"] == "feed_follower"
        unready = instance_of(data, "gateway", "gw-a")
        assert unready is not None and unready["snapshot"]["reasons"] == ["gateway: feed not applied yet"]
        ctl = instance_of(data, "controller", "ctl-1")
        assert ctl is not None and ctl["snapshot"]["controller"]["is_leader"] is True
        assert ctl["snapshot"]["controller"]["changes"] == COMPILED_THROUGH
        wrk = instance_of(data, "worker", "wrk-1")
        assert wrk is not None
        assert wrk["snapshot"]["worker"]["jobs"]["health"] == {
            "runs": 12,
            "failures": 1,
            "duration_seconds": 3.5,
            "in_flight": 4,
        }
        # Each stream the worker follows, as its heartbeat reported it.
        assert wrk["snapshot"]["streams"] == {
            "changes": {
                "position": COMPILED_THROUGH,
                "lag_seconds": 0.4,
                "entries": 3,
                "full_reads": {"start": 1, "gap": 1},
                "last_full_read_at": None,
            }
        }
        for group in instances.values():
            for instance in group:
                assert 0 <= instance["age_seconds"] <= INSTANCE_TTL_SECONDS

    def test_a_process_without_a_readable_snapshot_is_still_listed(
        self, authenticated_client: TestClient, cluster: Cluster
    ) -> None:
        """One that could not build a snapshot, or one this release cannot read: listed, and the page still renders."""
        cluster.heartbeat("wrk-quiet", "worker", None)
        cluster.heartbeat("gw-other", "gateway", "{not a snapshot at all")
        cluster.heartbeat("odd-1", "sidecar", None)

        data = stats_when(authenticated_client, lists("sidecar", "odd-1"))

        for role, instance_id in (("worker", "wrk-quiet"), ("gateway", "gw-other"), ("sidecar", "odd-1")):
            instance = instance_of(data, role, instance_id)
            assert instance is not None, instance_id
            assert instance["snapshot"] is None and instance["age_seconds"] is None
        # A role this release does not know comes after the known ones.
        assert list(data["instances"])[-1] == "sidecar"
        # Without a snapshot there is no feed to compare.
        assert "gw-other" not in {g["instance_id"] for g in data["feed"]["gateways"]}

    def test_a_process_that_withdrew_is_gone(self, authenticated_client: TestClient, cluster: Cluster) -> None:
        cluster.heartbeat("wrk-leaving", "worker", worker("wrk-leaving"))
        stats_when(authenticated_client, lists("worker", "wrk-leaving"))

        on_app_loop(
            authenticated_client, api_runtime(authenticated_client).redis_client.withdraw_instance, "wrk-leaving"
        )

        assert instance_of(authenticated_client.get(ENDPOINT).json(), "worker", "wrk-leaving") is None


class TestControllerAndFeed:
    def test_the_controller_is_the_lease_holder_with_its_last_compile(
        self, authenticated_client: TestClient, cluster: Cluster
    ) -> None:
        compiled_at = utc_now() - timedelta(seconds=30)
        newest = cluster.change()
        leader = controller("ctl-1", leading=True, changes=COMPILED_THROUGH, compiled_at=compiled_at)
        cluster.heartbeat("ctl-1", "controller", leader)
        # A standby's own position, from when it last led, is not the controller's.
        cluster.heartbeat("ctl-2", "controller", controller("ctl-2", leading=False, changes=EARLIER))
        cluster.lease("ctl-1")

        data = stats_when(authenticated_client, lists("controller", "ctl-2"))

        status = data["controller"]
        assert status["leader"] == "ctl-1"
        # How far the leader's last compile read the changes stream, against the stream's newest entry now.
        assert (status["changes"], status["changes_head"]) == (COMPILED_THROUGH, newest)
        assert datetime.fromisoformat(status["compiled_at"]) == compiled_at
        assert status["error"] is None
        # Nothing reads a revision any more: no field of the section carries one.
        assert [key for key, value in status.items() if "revision" in key and value is not None] == []
        controllers = [i["instance_id"] for i in data["instances"]["controller"]]
        assert [i for i in controllers if i in ("ctl-1", "ctl-2")] == ["ctl-1", "ctl-2"]

    def test_no_leader_while_nobody_holds_the_lease(self, authenticated_client: TestClient, cluster: Cluster) -> None:
        newest = cluster.change()
        cluster.heartbeat("ctl-2", "controller", controller("ctl-2", leading=False, changes=EARLIER))
        cluster.lease(None)

        status = stats_when(authenticated_client, lists("controller", "ctl-2"))["controller"]

        assert (status["leader"], status["changes"], status["compiled_at"]) == (None, None, None)
        # The stream's head is read from Redis, whoever leads.
        assert status["changes_head"] == newest

    def test_a_reading_before_any_change_was_announced(
        self, authenticated_client: TestClient, cluster: Cluster
    ) -> None:
        """No changes stream yet (Redis started empty, nothing saved since): no head, and the rest of the reading stands."""
        cluster.no_changes_stream()
        cluster.heartbeat("ctl-1", "controller", controller("ctl-1", leading=True, changes="0-0"))
        cluster.lease("ctl-1")

        data = authenticated_client.get(ENDPOINT).json()

        status = data["controller"]
        for section in ("controller", "feed", "streams"):
            assert data[section]["error"] is None, section
        assert (status["leader"], status["changes"], status["changes_head"]) == ("ctl-1", "0-0", None)
        assert instance_of(data, "controller", "ctl-1") is not None

    def test_each_gateway_is_in_sync_or_behind_the_head(
        self, authenticated_client: TestClient, cluster: Cluster
    ) -> None:
        cluster.head(head("digest-now", changes=COMPILED_THROUGH))
        cluster.heartbeat("gw-1", "gateway", gateway("gw-1", digest="digest-now", changes=COMPILED_THROUGH, age=2.0))
        cluster.heartbeat("gw-2", "gateway", gateway("gw-2", digest="digest-old", changes=EARLIER, age=300.0))
        cluster.heartbeat("gw-3", "gateway", gateway("gw-3", digest=None))

        feed = stats_when(authenticated_client, lists("gateway", "gw-3"))["feed"]

        assert (feed["digest"], feed["changes"], feed["sections"]) == ("digest-now", COMPILED_THROUGH, 3)
        assert feed["compiled_at"] is not None and feed["error"] is None
        gateways = {g["instance_id"]: g for g in feed["gateways"]}
        assert {"gw-1", "gw-2", "gw-3"} <= set(gateways)
        gw1, gw2, gw3 = gateways["gw-1"], gateways["gw-2"], gateways["gw-3"]
        assert (gw1["in_sync"], gw1["digest"], gw1["changes"]) == (True, "digest-now", COMPILED_THROUGH)
        assert (gw2["in_sync"], gw2["digest"], gw2["changes"]) == (False, "digest-old", EARLIER)
        assert (gw3["in_sync"], gw3["digest"], gw3["changes"], gw3["age_seconds"]) == (False, None, None, None)
        # The age the gateway reported, carried forward by how old its heartbeat is.
        assert 2.0 <= gw1["age_seconds"] <= 2.0 + INSTANCE_TTL_SECONDS
        assert 300.0 <= gw2["age_seconds"] <= 300.0 + INSTANCE_TTL_SECONDS

    def test_without_a_head_no_gateway_is_in_sync(self, authenticated_client: TestClient, cluster: Cluster) -> None:
        on_app_loop(authenticated_client, api_runtime(authenticated_client).redis_client.client.delete, FEED_HEAD_KEY)
        cluster.heartbeat("gw-1", "gateway", gateway("gw-1", digest="digest-now", changes=COMPILED_THROUGH, age=2.0))

        feed = stats_when(authenticated_client, lists("gateway", "gw-1"))["feed"]

        assert (feed["digest"], feed["changes"], feed["compiled_at"], feed["sections"]) == (None, None, None, None)
        assert all(g["in_sync"] is False for g in feed["gateways"])

    def test_an_unreadable_head_is_reported(self, authenticated_client: TestClient, cluster: Cluster) -> None:
        cluster.head(head("digest-now"))
        on_app_loop(
            authenticated_client,
            api_runtime(authenticated_client).redis_client.client.set,
            FEED_HEAD_KEY,
            '{"schema_version": 99}',
        )

        feed = authenticated_client.get(ENDPOINT).json()["feed"]

        assert feed["digest"] is None
        assert feed["error"] == "The routing feed head in Redis cannot be read by this release"


class TestTopology:
    """The cluster sections of the System view alone, which the topology view polls on the heartbeats' cadence."""

    def test_the_cluster_sections_alone_read_as_the_view_reads_them(
        self, authenticated_client: TestClient, cluster: Cluster
    ) -> None:
        cluster.head(head("digest-now"))
        cluster.heartbeat("gw-1", "gateway", gateway("gw-1", digest="digest-now", changes=COMPILED_THROUGH, age=2.0))
        cluster.heartbeat("ctl-1", "controller", controller("ctl-1", leading=True, changes=COMPILED_THROUGH))
        cluster.lease("ctl-1")
        cluster.job_stream(JobKind.PROVISION, 5, delivered=2)

        data = stats_when(authenticated_client, lists("gateway", "gw-1"), endpoint=TOPOLOGY)
        stats = authenticated_client.get(ENDPOINT).json()

        # No inventory, project, database or Redis section: nothing that reads Postgres or scans the keyspace.
        assert set(data) == {"generated_at", "instances", "controller", "feed", "streams"}
        assert data["controller"]["leader"] == stats["controller"]["leader"] == "ctl-1"
        assert data["controller"]["changes"] == COMPILED_THROUGH
        gw = instance_of(data, "gateway", "gw-1")
        assert gw is not None and gw["snapshot"]["gateway"]["requests"] == 10
        assert [g["in_sync"] for g in data["feed"]["gateways"] if g["instance_id"] == "gw-1"] == [True]
        assert list(data["streams"]["by_name"]) == list(stats["streams"]["by_name"])
        provision = data["streams"]["by_name"]["jobs.provision"]
        assert (provision["length"], provision["pending"], provision["queued"]) == (5, 2, 3)

    async def test_an_unreachable_redis_leaves_each_section_with_its_error(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        unreachable = RedisClient(f"redis://127.0.0.1:{port}/0", socket_timeout=0.5)
        await unreachable.connect()
        try:
            topology = await ClusterReader(unreachable).topology()
        finally:
            await unreachable.close()

        assert topology.instances == {}
        for section in (topology.controller, topology.feed, topology.streams):
            assert section.error, section


class TestClusterReading:
    """The sections one reading makes, where the page's own tests leave a rule open."""

    def test_roles_this_release_does_not_know_come_last_by_name(self) -> None:
        reading = ClusterReading(
            instances=[
                InstanceInfo(instance_id="a-1", role="sidecar"),
                InstanceInfo(instance_id="z-2", role="agent"),
                InstanceInfo(instance_id="wrk-2", role="worker"),
                InstanceInfo(instance_id="z-1", role="agent"),
                InstanceInfo(instance_id="wrk-1", role="worker"),
                InstanceInfo(instance_id="api-1", role="api"),
            ]
        )

        groups = reading.by_role()

        # By role name, not by the ids in them: the sidecar's id sorts first.
        assert list(groups) == ["api", "worker", "agent", "sidecar"]
        assert [[instance.instance_id for instance in group] for group in groups.values()] == [
            ["api-1"], ["wrk-1", "wrk-2"], ["z-1", "z-2"], ["a-1"],
        ]

    @pytest.mark.parametrize(
        "snapshot",
        [None, InstanceSnapshot(runtime=runtime("ctl-1", Role.CONTROLLER), reasons=["controller: starting"])],
        ids=["no_snapshot", "no_controller_section"],
    )
    def test_a_leader_whose_heartbeat_reports_no_compile_has_no_position(
        self, snapshot: InstanceSnapshot | None
    ) -> None:
        reading = ClusterReading(
            instances=[InstanceInfo(instance_id="ctl-1", role="controller", snapshot=snapshot)],
            leader="ctl-1",
            changes_head=COMPILED_THROUGH,
        )

        status = reading.controller_status()

        assert (status.leader, status.changes, status.compiled_at, status.changes_head) == (
            "ctl-1", None, None, COMPILED_THROUGH,
        )

    def test_a_gateways_feed_age_is_carried_forward_by_its_heartbeats_own_age(self) -> None:
        def listed(instance_id: str, snapshot: InstanceSnapshot, age: float | None) -> InstanceInfo:
            return InstanceInfo(instance_id=instance_id, role="gateway", snapshot=snapshot, age_seconds=age)

        reading = ClusterReading(
            instances=[
                listed("gw-1", gateway("gw-1", digest="d1", age=2.04), 3.4),
                # A heartbeat key with no TTL has no age: the gateway's own stands.
                listed("gw-2", gateway("gw-2", digest="d0", age=2.0), None),
                # Nothing applied yet: no age to carry.
                listed("gw-3", gateway("gw-3", digest=None), 1.0),
                InstanceInfo(instance_id="wrk-1", role="worker", snapshot=worker("wrk-1"), age_seconds=1.0),
            ],
            head=head("d1"),
        )

        feed = reading.feed_status()

        assert [(g.instance_id, g.in_sync, g.age_seconds) for g in feed.gateways] == [
            ("gw-1", True, 5.4),
            ("gw-2", False, 2.0),
            ("gw-3", False, None),
        ]


class TestWorkStreams:
    """Each work stream and the workers' group on it: the backlog, the work in hand, and the workers taking it."""

    def test_the_page_shows_every_work_stream(self, authenticated_client: TestClient, cluster: Cluster) -> None:
        cluster.job_stream(JobKind.PROVISION, 5, delivered=2)

        streams = authenticated_client.get(ENDPOINT).json()["streams"]

        assert streams["error"] is None
        assert list(streams["by_name"]) == [*(f"jobs.{kind}" for kind in JobKind), "sightings", "observations"]
        provision = streams["by_name"]["jobs.provision"]
        oldest = provision.pop("oldest_seconds")
        assert provision == {"length": 5, "pending": 2, "queued": 3, "consumers": 1}
        assert 0 <= oldest < 10

    async def test_queued_and_running_entries_and_a_stream_no_worker_joined(self, redis_client: RedisClient) -> None:
        client = redis_client.client
        health = JOB_STREAM_KEY.format(kind=JobKind.HEALTH)
        for n in range(7):
            await client.xadd(health, {"key": f"proxy-{n}"})
        await client.xgroup_create(health, WORKER_GROUP, id="0")
        # Two workers joined: one took three entries, the other none yet.
        await client.xreadgroup(WORKER_GROUP, "worker-1", {health: ">"}, count=3)
        await client.xgroup_createconsumer(health, WORKER_GROUP, "worker-2")
        # A group of another name on the stream is not the workers'.
        await client.xgroup_create(health, "elsewhere", id="0")
        await client.xreadgroup("elsewhere", "reader", {health: ">"}, count=7)
        # Sightings appended before any worker created the group.
        for n in range(3):
            await client.xadd(SIGHTING_STREAM_KEY, {"item": f"sighting-{n}"})

        streams = await ClusterReader(redis_client).streams()

        assert streams.error is None
        reading = streams.by_name["jobs.health"]
        assert (reading.length, reading.pending, reading.queued, reading.consumers) == (7, 3, 4, 2)
        reading = streams.by_name["sightings"]
        assert (reading.length, reading.pending, reading.queued, reading.consumers) == (3, 0, 3, 0)

    async def test_a_missing_stream_reads_as_zeros(self, redis_client: RedisClient) -> None:
        """Before any producer appended to it, or after a wipe; every work stream is listed all the same."""
        streams = await ClusterReader(redis_client).streams()

        assert streams.error is None
        assert list(streams.by_name) == list(WORK_STREAMS)
        assert set(WORK_STREAMS) == {f"jobs.{kind}" for kind in JobKind} | {"sightings", "observations"}
        assert set(WORK_STREAMS.values()) == {JOB_STREAM_KEY.format(kind=kind) for kind in JobKind} | {
            SIGHTING_STREAM_KEY,
            OBSERVATION_STREAM_KEY,
        }
        assert all(reading == WorkStream() for reading in streams.by_name.values())

    async def test_the_oldest_entry_s_age_on_redis_s_clock(
        self, redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = redis_client.client
        seconds, microseconds = await client.time()
        now_ms = seconds * 1000 + microseconds // 1000
        # Redis stamped the entries, so this host's clock, an hour behind here, changes nothing.
        host_time, host_now = time.time, utc_now
        monkeypatch.setattr(time, "time", lambda: host_time() - 3600)
        monkeypatch.setattr(system_stats, "utc_now", lambda: host_now() - timedelta(hours=1))
        first = await client.xadd(OBSERVATION_STREAM_KEY, {"item": "{}"}, id=f"{now_ms - 90_000}-0")
        await client.xadd(OBSERVATION_STREAM_KEY, {"item": "{}"}, id=f"{now_ms - 30_000}-0")
        await client.xadd(OBSERVATION_STREAM_KEY, {"item": "{}"})

        async def oldest() -> float | None:
            return (await ClusterReader(redis_client).streams()).by_name["observations"].oldest_seconds

        age = await oldest()
        assert age is not None and 90 <= age < 100
        # A handled entry is deleted: the age is that of the oldest entry left.
        await client.xdel(OBSERVATION_STREAM_KEY, first)
        age = await oldest()
        assert age is not None and 30 <= age < 40
        # Every entry handled: no oldest entry.
        await client.xtrim(OBSERVATION_STREAM_KEY, maxlen=0, approximate=False)
        assert await oldest() is None
        # An entry stamped ahead of Redis's clock, which stepped back since, reads as just appended.
        await client.xadd(OBSERVATION_STREAM_KEY, {"item": "{}"}, id=f"{now_ms + 60_000}-0")
        assert await oldest() == 0

    async def test_queued_is_never_negative_when_the_cap_trimmed_pending_entries(
        self, redis_client: RedisClient
    ) -> None:
        client = redis_client.client
        sync = JOB_STREAM_KEY.format(kind=JobKind.SYNC)
        for n in range(5):
            await client.xadd(sync, {"key": f"connector-{n}"})
        await client.xgroup_create(sync, WORKER_GROUP, id="0")
        await client.xreadgroup(WORKER_GROUP, "worker-1", {sync: ">"}, count=5)
        # The cap trims the stream, and the trimmed entries stay pending.
        await client.xtrim(sync, maxlen=2, approximate=False)

        reading = (await ClusterReader(redis_client).streams()).by_name["jobs.sync"]

        assert (reading.length, reading.pending, reading.queued) == (2, 5, 0)
        assert reading.model_dump(mode="json")["queued"] == 0

    async def test_a_key_that_is_not_a_stream_fails_the_section(self, redis_client: RedisClient) -> None:
        """Only a missing stream or group reads as zeros: anything else Redis refuses leaves the section with why."""
        await redis_client.client.set(JOB_STREAM_KEY.format(kind=JobKind.ENRICH), "not a stream")

        streams = await ClusterReader(redis_client).streams()

        assert streams.by_name == {}
        assert streams.error is not None and "WRONGTYPE" in streams.error

    async def test_groups_redis_refuses_to_show_fail_the_section(
        self, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """Rather than streams that seem to have no worker: a user without XINFO reads the lengths, not the groups."""
        await redis_client.client.xadd(JOB_STREAM_KEY.format(kind=JobKind.HEALTH), {"key": "proxy-1"})
        await redis_client.client.acl_setuser(
            "no-xinfo", enabled=True, passwords=["+secret"], keys=["*"], channels=["*"], commands=["+@all", "-xinfo"]
        )
        try:
            limited = RedisClient(test_redis_url.replace("redis://", "redis://no-xinfo:secret@", 1))
            await limited.connect()
            try:
                streams = await ClusterReader(limited).streams()
            finally:
                await limited.close()
        finally:
            await redis_client.client.acl_deluser("no-xinfo")

        assert streams.by_name == {}
        assert streams.error is not None and "xinfo|groups" in streams.error


# --- The stores ------------------------------------------------------------------------------


class TestStores:
    def test_inventory_and_project_usage_follow_created_entities(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
        sample_credential_data: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Creating one of each entity moves every matching counter; tables are shared, so the asserts are deltas."""
        before = authenticated_client.get(ENDPOINT).json()["inventory"]

        project = authenticated_client.post("/api/v1/projects", json=sample_project_data).json()
        credential = authenticated_client.post(
            f"/api/v1/projects/{project['id']}/credentials", json=sample_credential_data
        ).json()
        connector = authenticated_client.post(
            f"/api/v1/projects/{project['id']}/connectors",
            json={**sample_connector_data, "credential_id": credential["id"]},
        ).json()
        proxies = api_runtime(authenticated_client).proxy_service
        created = [
            on_app_loop(
                authenticated_client,
                proxies.create,
                project["id"],
                ProxyCreate(host=f"proxy-{n}.example.com", port=8080, connector_id=connector["id"]),
            )
            for n in range(3)
        ]
        # The health job's compare-and-set, as a worker would write it.
        moves = {
            created[0].id: StatusMove(ProxyStatus.HEALTHY, ProxyStatus.UNKNOWN),
            created[1].id: StatusMove(ProxyStatus.UNHEALTHY, ProxyStatus.UNKNOWN),
        }
        on_app_loop(authenticated_client, proxies.set_statuses_if, moves)

        data = authenticated_client.get(ENDPOINT).json()
        after = data["inventory"]

        assert after["projects"] == before["projects"] + 1
        assert after["credentials"] == before["credentials"] + 1
        assert after["connectors"] == before["connectors"] + 1
        assert after["connectors_enabled"] == before["connectors_enabled"] + 1
        assert after["proxies"] == before["proxies"] + 3
        # The seeded admin is always present and roles add up to the total.
        assert after["users"] >= 1
        assert sum(after["users_by_role"].values()) == after["users"]
        # Statuses from the proxies.status column: every proxy has one.
        assert sum(after["proxies_by_status"].values()) == after["proxies"]
        for status, moved in (("unknown", 1), ("healthy", 1), ("unhealthy", 1)):
            assert after["proxies_by_status"].get(status, 0) == before["proxies_by_status"].get(status, 0) + moved
        # Provider types from the api's registry, plus the stored custom ones.
        assert after["providers_builtin"] > 0
        assert after["providers_total"] == after["providers_builtin"] + after["providers_custom"]

        usage = {p["id"]: p for p in data["projects"]}
        assert usage[project["id"]] == {
            "id": project["id"],
            "name": project["name"],
            "credentials": 1,
            "connectors": 1,
            "proxies": 3,
        }

    def test_database_reports_sizes_for_known_tables(self, authenticated_client: TestClient) -> None:
        database = authenticated_client.get(ENDPOINT).json()["database"]

        assert database["error"] is None
        assert database["size_bytes"] > 0
        assert database["backends"] is not None and database["backends"] >= 1
        names = {t["name"] for t in database["tables"]}
        assert {"projects", "proxies", "proxy_metrics", "metric_totals"} <= names
        for table in database["tables"]:
            assert table["total_bytes"] >= table["table_bytes"]
            # Unknown until autovacuum analyses the table, never a bogus zero.
            assert table["row_estimate"] is None or table["row_estimate"] >= 0
            # Bloat indicators: a count that is never negative, and a vacuum
            # time that is simply absent on a table no vacuum has reached.
            assert table["dead_rows"] >= 0
            assert table["last_vacuum_at"] is None or isinstance(table["last_vacuum_at"], str)

    def test_redis_reports_memory_and_keyspace(self, authenticated_client: TestClient, cluster: Cluster) -> None:
        # What a 5.x process left behind: its keys carry no prefix.
        cluster.key("proxy:health:5x-proxy")

        # Once the app's first heartbeat is in: the keyspace walk runs beside
        # the heartbeat read, so a reading can list the app and still miss its keys.
        redis = stats_when(authenticated_client, lambda data: "Instance snapshots" in key_groups(data))["redis"]

        assert redis["error"] is None
        assert redis["version"]
        assert redis["used_memory_bytes"] > 0
        assert redis["truncated"] is False
        assert redis["total_keys"] >= 1
        # Every key prefix Octoprox writes is named in the breakdown, and an earlier release's keys apart.
        groups = {group["label"]: group["keys"] for group in redis["groups"]}
        assert "Instance heartbeats" in groups
        assert groups[EARLIER_RELEASES_KEY_GROUP] == 1


async def test_an_unreachable_redis_costs_only_its_own_sections(
    db_session: AsyncSession, test_settings: Settings
) -> None:
    """The Postgres sections render; every Redis-backed one says why it is empty."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    unreachable = RedisClient(f"redis://127.0.0.1:{port}/0", socket_timeout=0.5)
    await unreachable.connect()
    try:
        stats = await SystemReader(db_session, unreachable).read()
    finally:
        await unreachable.close()

    assert stats.instances == {}
    for section in (stats.controller, stats.feed, stats.streams, stats.redis):
        assert section.error, section
    assert stats.controller.leader is None and stats.feed.gateways == [] and stats.streams.by_name == {}
    assert stats.controller.changes_head is None
    # Postgres answered.
    assert stats.database.error is None and stats.database.size_bytes > 0
    assert stats.inventory.providers_builtin == 0  # no registry given


async def test_a_redis_read_cut_by_a_deadline_is_named_by_its_type() -> None:
    """A bare TimeoutError has no text: the cluster and work stream sections, and their logs, name it by its type."""
    pipe = MagicMock(spec=Pipeline)
    pipe.execute = AsyncMock(side_effect=TimeoutError)
    redis = MagicMock(spec=RedisClient)
    redis.client.pipeline.return_value = pipe
    reader = ClusterReader(redis)

    with patch("api.core.system_stats.logger") as log:
        reading = await reader.read()
        streams = await reader.streams()

    assert (reading.error, streams.error) == ("TimeoutError", "TimeoutError")
    assert log.warning.call_args_list == [
        call("Cluster state unavailable", error="TimeoutError"),
        call("Work streams unavailable", error="TimeoutError"),
    ]


def test_a_process_whose_first_import_is_the_models_reads_any_snapshot() -> None:
    """The role sections are defined with the snapshot: no import of the runtimes is needed to read a heartbeat."""
    snapshot = InstanceSnapshot(
        runtime=runtime("all-1", Role.ALL),
        ready=True,
        tasks=[WorkerTask(name="heartbeat", state="running", runs=1)],
        streams=worker("all-1").streams,
        gateway=gateway_section(digest="d1", changes=COMPILED_THROUGH, age=1.0),
        controller=controller("all-1", leading=True, changes=COMPILED_THROUGH).controller,
        worker=worker("all-1").worker,
    )
    script = (
        "import sys\n"
        "from api.models.system import InstanceSnapshot\n"
        "snapshot = InstanceSnapshot.model_validate_json(sys.stdin.read())\n"
        "assert not [m for m in sys.modules if m == 'api.roles' or m.startswith('api.roles.')], 'imported api.roles'\n"
        "print(snapshot.model_dump_json())\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        input=snapshot.model_dump_json(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert InstanceSnapshot.model_validate_json(result.stdout) == snapshot


# --- Trend history -----------------------------------------------------------------------------


class TestSystemHistoryAccessControl:
    def test_unauthenticated_is_rejected(self, async_client: TestClient) -> None:
        assert async_client.get(HISTORY).status_code == 401

    def test_viewer_is_forbidden(self, viewer_client: TestClient) -> None:
        assert viewer_client.get(HISTORY).status_code == 403

    def test_editor_is_forbidden(self, editor_client: TestClient) -> None:
        assert editor_client.get(HISTORY).status_code == 403


class TestSystemHistory:
    def test_empty_before_the_first_snapshot(self, authenticated_client: TestClient, test_settings: Settings) -> None:
        """A fresh install must render as "collecting", not as an error."""
        _clear_snapshots(test_settings)

        resp = authenticated_client.get(HISTORY)

        assert resp.status_code == 200
        data = resp.json()
        assert data["snapshots"] == []
        assert data["table_growth"] == []
        assert data["range"] == "24h"
        assert data["bucket_seconds"] is None

    def test_rejects_an_unknown_range(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get(HISTORY, params={"range": "5y"}).status_code == 422

    def test_short_ranges_are_raw_and_long_ranges_bucketed(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get(HISTORY, params={"range": "1h"}).json()["bucket_seconds"] is None
        assert authenticated_client.get(HISTORY, params={"range": "7d"}).json()["bucket_seconds"] == 3600
        assert authenticated_client.get(HISTORY, params={"range": "90d"}).json()["bucket_seconds"] == 86400

    def test_the_interval_is_the_configured_one(
        self, authenticated_client: TestClient, test_settings: Settings
    ) -> None:
        test_settings.system_metrics_interval = 120

        assert authenticated_client.get(HISTORY).json()["interval_seconds"] == 120

    def test_returns_stored_snapshots_oldest_first(
        self, authenticated_client: TestClient, test_settings: Settings
    ) -> None:
        _clear_snapshots(test_settings)
        now = utc_now()
        _seed_snapshots(
            test_settings,
            [
                SystemMetricsModel(
                    timestamp=now - timedelta(minutes=minutes),
                    database_size_bytes=size,
                    proxies_total=minutes,
                    table_sizes={"proxies": size},
                    proxy_status_counts={},
                )
                for minutes, size in ((30, 100), (10, 300), (20, 200))
            ],
        )

        data = authenticated_client.get(HISTORY, params={"range": "1h"}).json()

        assert [s["database_size_bytes"] for s in data["snapshots"]] == [100, 200, 300]
        assert data["interval_seconds"] >= 0

    def test_table_growth_is_the_delta_across_the_window(
        self, authenticated_client: TestClient, test_settings: Settings
    ) -> None:
        _clear_snapshots(test_settings)
        now = utc_now()
        _seed_snapshots(
            test_settings,
            [
                SystemMetricsModel(
                    timestamp=now - timedelta(minutes=40),
                    table_sizes={"proxies": 100, "users": 50},
                    proxy_status_counts={},
                ),
                SystemMetricsModel(
                    timestamp=now - timedelta(minutes=1),
                    table_sizes={"proxies": 400, "users": 50},
                    proxy_status_counts={},
                ),
            ],
        )

        rows = authenticated_client.get(HISTORY, params={"range": "1h"}).json()["table_growth"]
        growth = {t["name"]: t for t in rows}

        assert growth["proxies"]["delta_bytes"] == 300
        assert growth["proxies"]["first_bytes"] == 100
        assert growth["proxies"]["last_bytes"] == 400
        # A table that did not move still appears, with a zero delta.
        assert growth["users"]["delta_bytes"] == 0
        # Biggest mover first.
        assert rows[0]["name"] == "proxies"


def _seed_snapshots(settings: Settings, rows: list[SystemMetricsModel]) -> None:
    """Insert snapshot rows over a synchronous connection.

    These tests drive the app through ``TestClient``, which runs it on its own
    event loop; writing with the async session factory here would bind the
    connection to a different loop and blow up mid-request.
    """
    engine = create_engine(settings.database_url_sync)
    try:
        with Session(engine) as session:
            session.add_all(rows)
            session.commit()
    finally:
        engine.dispose()


def _clear_snapshots(settings: Settings) -> None:
    """Drop every stored snapshot: route tests share their tables, and these assert on the whole series."""
    engine = create_engine(settings.database_url_sync)
    try:
        with Session(engine) as session:
            session.query(SystemMetricsModel).delete()
            session.commit()
    finally:
        engine.dispose()
