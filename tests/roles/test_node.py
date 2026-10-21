# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The node: what each role builds, how it starts and stops its runtimes and its own loops, what it reports, and the api.

Nodes run on the test's event loop against the test containers, each on empty stores and stopped when its test ends;
the heartbeat tests take Redis away with ``CLIENT PAUSE``, or Postgres by standing in for its ``SELECT 1``.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import structlog
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.job_stats import job_stats
from api.core.loops import Loop, PeriodicLoop
from api.core.roles import JobKind, Role
from api.core.workers import WORKERS, LeaseName, WorkerName
from api.db.geo_repository import GeoDatabaseRepository
from api.db.openvpn_repository import OpenVpnSettingsRepository
from api.db.redis import (
    CHANGES_STREAM_KEY,
    INSTANCE_INDEX_KEY,
    INSTANCE_REGISTRY_KEY,
    INSTANCE_STATS_KEY,
    LEASE_KEY,
    OBSERVATION_STREAM_KEY,
    REPLY_TIMEOUT_SECONDS,
    RedisClient,
)
from api.db.repository import ProviderDescriptorRepository
from api.db.session import get_async_session_factory
from api.db.streams import ChangeKind
from api.db.wireguard_repository import WireGuardSettingsRepository
from api.feed.compiler import FeedCompiler
from api.geo.models import GeoDatabaseRecord, GeoDatabaseSource, IpObservation, ObservationSource
from api.geo.store import DatabaseRetry
from api.models.provider import ProviderRecord
from api.models.system import GatewaySnapshot, InstanceSnapshot, StreamState
from api.models.tunnel import TunnelEndpointState
from api.roles import Node
from api.roles.node import Heartbeat
from api.roles.runtime import RoleRuntime
from tests.conftest import queued_job_keys
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb
from tests.jobs.test_refresh import ACME
from tests.waits import until

# Short enough that a node's loops act within a test's patience.
FAST = {"feed_compile_interval": 0.1}
# The roles that use Postgres, and those unready while Redis does not answer.
POSTGRES_ROLES = (Role.API, Role.CONTROLLER, Role.WORKER, Role.ALL)


@pytest.fixture
async def nodes(
    test_settings: Settings, db_session: AsyncSession, redis_client: RedisClient
) -> AsyncIterator[Callable[..., Node]]:
    """Builds a node of a role from the test settings with overrides; every one is stopped when the test ends.

    It asks for ``db_session`` and ``redis_client`` for their emptying, before the first node starts and after the last
    has stopped.
    """
    built: list[Node] = []
    # A fresh engine: the cached one may hold connections of another test's event loop.
    get_async_session_factory.cache_clear()

    def build(role: Role, **overrides: Any) -> Node:
        node = Node(test_settings.model_copy(update={"role": role, **FAST, **overrides}))
        built.append(node)
        return node

    yield build
    for node in built:
        await node.stop()
    get_async_session_factory.cache_clear()


def task_names(node: Node) -> list[str]:
    return sorted(task.get_name() for task in node.background_tasks)


class Idle(Loop):
    """A loop that only waits, under a name no catalogue holds: no duty brings its cycle, and its stop ends it."""

    def __init__(self, name: Any) -> None:
        super().__init__(name=name)

    async def _cycle(self) -> bool:
        return False


class Noted(RoleRuntime):
    """A role runtime with one idle loop that notes its start and stop in ``notes``; when ``fails`` its start raises once the loop runs."""

    def __init__(self, name: str, notes: list[str], *, fails: bool = False) -> None:
        super().__init__()
        self.name = name
        self.notes = notes
        self.fails = fails
        self.loop = Idle(f"{name}_loop")

    def loops(self) -> Sequence[Loop]:
        return (self.loop,)

    def readiness(self) -> str | None:
        return None

    def snapshot(self) -> None:
        """Nothing to report, so the heartbeat's snapshot carries no section for it."""
        return None

    async def start(self) -> None:
        self.notes.append(f"start {self.name}")
        await super().start()
        if self.fails:
            raise RuntimeError(f"{self.name} cannot start")

    async def stop(self) -> None:
        self.notes.append(f"stop {self.name}")
        await super().stop()


class TestComposition:
    """What a node of each role builds."""

    async def test_each_role_builds_its_runtimes(self, nodes: Callable[..., Node]) -> None:
        expected = {
            Role.API: {"api"},
            Role.CONTROLLER: {"controller"},
            Role.WORKER: {"worker"},
            Role.GATEWAY: {"gateway"},
            Role.ALL: {"api", "controller", "worker", "gateway"},
            Role.ECHO: set(),
        }
        for role, runtimes in expected.items():
            node = nodes(role)
            built = {name for name in ("api", "controller", "worker", "gateway") if getattr(node, name) is not None}
            assert built == runtimes, role
            # Gateways and echo hold no database credentials; echo nothing but its files.
            assert (node.session_factory is not None) == (role not in (Role.GATEWAY, Role.ECHO)), role
            assert (node.redis_client is not None) == (role != Role.ECHO), role
            assert (node.echo_store is not None) == (role == Role.ECHO), role
            # A change log where writes are committed: never on a gateway, which writes no definition.
            assert (node.changes is not None) == (role in POSTGRES_ROLES), role

    async def test_one_registry_store_attribution_stack_and_change_log_shared_by_every_runtime(
        self, nodes: Callable[..., Node]
    ) -> None:
        node = nodes(Role.ALL)
        assert node.api is not None and node.worker is not None and node.provider_store is not None
        assert node.api.provider_registry is node.registry is node.provider_store.registry
        assert node.api.geo is node.geo
        assert node.api.changes is node.changes is not None
        # In role all the worker's definitions refresher is the process's: the node runs none of its own.
        assert node.refresher is node.worker.refresher

    async def test_every_role_with_ip_databases_retries_their_files_once_and_the_controller_none(
        self, nodes: Callable[..., Node]
    ) -> None:
        """The api's, a worker's and a gateway's store miss files the feed or Postgres lists; the controller opens none."""
        for role in (Role.API, Role.WORKER, Role.GATEWAY, Role.ALL, Role.CONTROLLER):
            node = nodes(role)
            assert node.geo is not None
            retries = [loop for loop in node._loops if isinstance(loop, DatabaseRetry)]
            assert len(retries) == (0 if role == Role.CONTROLLER else 1), role
            assert all(retry._store is node.geo.database_store for retry in retries), role

    async def test_a_process_has_one_redis_client(
        self, nodes: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every runtime of role all on it: the request path takes its deadline from it, nothing builds another."""
        built: list[RedisClient] = []
        init = RedisClient.__init__

        def counted(client: RedisClient, *args: Any, **kwargs: Any) -> None:
            built.append(client)
            init(client, *args, **kwargs)

        monkeypatch.setattr(RedisClient, "__init__", counted)

        node = nodes(Role.ALL)

        assert built == [node.redis_client]
        assert node.api is not None and node.api.redis_client is node.redis_client

    async def test_the_client_is_named_after_the_instance_and_bounded_by_the_settings(
        self, nodes: Callable[..., Node]
    ) -> None:
        first = nodes(Role.API, instance_id="api-7", redis_request_timeout=0.5)
        # The same instance id again, as Compose and a restarted pod give one.
        again = nodes(Role.API, instance_id="api-7")
        assert first.redis_client is not None and again.redis_client is not None

        # A token drawn at each boot: a restarted process never takes over its predecessor's batch marks.
        assert first.redis_client.writer_id.startswith("api-7:")
        assert again.redis_client.writer_id.startswith("api-7:")
        assert first.redis_client.writer_id != again.redis_client.writer_id
        assert first.redis_client.socket_timeout == REPLY_TIMEOUT_SECONDS
        async with first.redis_client.request_deadline() as deadline:
            remaining = (deadline.when() or 0.0) - asyncio.get_running_loop().time()
        assert 0.4 < remaining <= 0.5


class TestLifecycle:
    async def test_an_api_node_advertises_its_snapshot_and_withdraws_on_stop(
        self, nodes: Callable[..., Node], redis_client: RedisClient
    ) -> None:
        node = nodes(Role.API)
        await node.start()
        registry_key = INSTANCE_REGISTRY_KEY.format(instance_id=node.instance_id)
        stats_key = INSTANCE_STATS_KEY.format(instance_id=node.instance_id)
        await until(lambda: node.readiness() == [])
        await until(lambda: redis_client.client.exists(registry_key))

        assert await redis_client.client.get(registry_key) == "api"
        snapshot = InstanceSnapshot.model_validate_json(await redis_client.client.get(stats_key))
        assert snapshot.runtime.role == Role.API and snapshot.runtime.instance_id == node.instance_id
        assert snapshot.ready is True and snapshot.reasons == []
        assert snapshot.api is not None and snapshot.api.pool_size == 5
        # The definitions refresher follows the changes stream from its position at start.
        assert set(snapshot.streams) == {"changes"}
        assert snapshot.streams["changes"].position is not None
        assert snapshot.streams["changes"].full_reads == {"start": 1}
        assert (snapshot.gateway, snapshot.controller, snapshot.worker) == (None, None, None)
        # The api runtime runs no loop: the refresher, the observation recorder and the database retry are the
        # node's, each run once.
        assert sorted(task.name for task in snapshot.tasks) == [
            "definitions_refresher",
            "geo_database_retry",
            "geo_observation_publisher",
            "heartbeat",
            "loop_lag_monitor",
        ]

        await node.stop()

        assert await redis_client.client.exists(registry_key, stats_key) == 0
        assert node.instance_id not in await redis_client.client.smembers(INSTANCE_INDEX_KEY)
        assert node.readiness() == ["stopped"] and node.background_tasks == []
        await node.stop()  # a second stop does nothing

    async def test_an_all_node_runs_every_runtime_once(self, nodes: Callable[..., Node]) -> None:
        node = nodes(Role.ALL)
        await node.start()
        # Ready once the controller has published a feed and the gateway applied it.
        await until(lambda: node.readiness() == [])

        snapshot = node.snapshot()
        assert snapshot.api is not None and snapshot.gateway is not None
        assert snapshot.controller is not None and snapshot.controller.is_leader
        assert snapshot.worker is not None and set(snapshot.worker.jobs) == set(node.settings.worker_jobs)
        names = [task.name for task in snapshot.tasks]
        for loop in ("heartbeat", "loop_lag_monitor", "feed_builder", "feed_follower", "job_runner_health"):
            assert names.count(loop) == 1, loop
        # One refresher (the worker's), one observation recorder and one database retry (the node's) per process.
        assert names.count("definitions_refresher") == 1
        assert names.count("geo_observation_publisher") == 1
        assert names.count("geo_database_retry") == 1
        assert set(snapshot.streams) == {"changes", "runtime", "feed"}
        # Round trips through the heartbeat's JSON with every section.
        assert InstanceSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot

    async def test_in_role_all_the_changes_row_is_the_refreshers(
        self, nodes: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The controller follows the changes stream too, and reports its position in its own section."""
        node = nodes(Role.ALL)
        assert node.refresher is not None and node.controller is not None
        mine = StreamState(position="7-0", entries=7)
        monkeypatch.setattr(node.refresher, "streams", lambda: {"changes": mine})

        streams = node.snapshot().streams

        assert streams["changes"] == mine
        assert streams["runtime"] == node.controller.streams()["runtime"]

    async def test_runtimes_start_in_order_and_stop_in_reverse_one_whose_start_raised_included(
        self, nodes: Callable[..., Node]
    ) -> None:
        """The node drives them as role runtimes alone: the gateway, started last, fails, and every runtime is stopped."""
        node = nodes(Role.API)
        notes: list[str] = []
        runtimes = {name: Noted(name, notes) for name in ("api", "controller", "worker")}
        runtimes["gateway"] = Noted("gateway", notes, fails=True)
        for name, runtime in runtimes.items():
            setattr(node, name, runtime)

        with pytest.raises(RuntimeError, match="gateway cannot start"):
            await node.start()

        assert notes == [
            "start api",
            "start controller",
            "start worker",
            "start gateway",
            "stop gateway",
            "stop worker",
            "stop controller",
            "stop api",
        ]
        # The failed one's loop too: its stop ended the loop it had started.
        assert all(runtime.loop.stopped for runtime in runtimes.values())
        assert node.readiness() == ["stopped"] and node.background_tasks == []

    async def test_the_node_lists_every_runtime_s_loops_and_its_own(self, nodes: Callable[..., Node]) -> None:
        """The System page's rows: each runtime says which tasks it runs, so none is left out."""
        node = nodes(Role.API)
        notes: list[str] = []
        for name in ("controller", "worker"):
            setattr(node, name, Noted(name, notes))

        await node.start()

        own = ["heartbeat", "loop_lag_monitor", "geo_observation_publisher", "geo_database_retry", "definitions_refresher"]
        assert task_names(node) == sorted(["controller_loop", "worker_loop", *own])
        await node.stop()
        assert notes == ["start controller", "start worker", "stop worker", "stop controller"]

    async def test_stop_order_runtimes_then_own_loops_then_change_log_then_client(
        self, nodes: Callable[..., Node], redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A save just before shutdown: its entry is kept through a Redis stall, and the stop sends it.

        The observation recorder stops after the runtimes, whose stops may record, the heartbeat after the node's loops,
        and the change log after every loop.
        """
        # Far beyond the test, so only the stop can send it.
        monkeypatch.setattr("api.db.streams.RETRY_FIRST_SECONDS", 60.0)
        node = nodes(Role.API, redis_request_timeout=0.1)
        await node.start()
        assert node.changes is not None and node.api is not None and node.redis_client is not None
        assert node.geo is not None and node._heartbeat is not None
        stopped: list[str] = []
        for name, owner, method in (
            ("api", node.api, "stop"),
            ("observation recorder", node.geo.observation_recorder, "stop"),
            ("heartbeat", node._heartbeat, "stop"),
            ("change log", node.changes, "close"),
            ("client", node.redis_client, "close"),
        ):
            monkeypatch.setattr(owner, method, _noting(stopped, name, getattr(owner, method)))

        await redis_client.client.execute_command("CLIENT", "PAUSE", 300, "WRITE")
        await node.changes.record(ChangeKind.PROJECT, "p-1")
        assert node.changes.pending == {ChangeKind.PROJECT}
        await asyncio.sleep(0.4)
        await node.stop()

        entries = await redis_client.client.xrange(CHANGES_STREAM_KEY)
        assert [fields for _, fields in entries][-1] == {"kind": "project", "ref": "p-1"}
        assert list(dict.fromkeys(stopped)) == ["api", "observation recorder", "heartbeat", "change log", "client"]

    async def test_what_the_runtimes_record_as_they_stop_is_published(
        self, nodes: Callable[..., Node], redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The observation recorder is the node's own loop, stopped after the runtimes, and its stop sends what it holds."""
        node = nodes(Role.API)
        await node.start()
        assert node.api is not None and node.geo is not None
        recorder = node.geo.observation_recorder
        stop = node.api.stop

        async def recording_stop() -> None:
            recorder.record(IpObservation(source=ObservationSource.MANUAL, ip="81.2.69.160"))
            await stop()

        monkeypatch.setattr(node.api, "stop", recording_stop)

        await node.stop()

        entries = await redis_client.client.xrange(OBSERVATION_STREAM_KEY)
        assert [IpObservation.model_validate_json(fields["item"]).ip for _, fields in entries] == ["81.2.69.160"]

    async def test_a_start_that_fails_stops_everything_that_started(
        self, nodes: Callable[..., Node], redis_client: RedisClient, tmp_path: Path
    ) -> None:
        """The gateway, started last, cannot have its MITM CA: the api, controller and worker it follows are stopped.

        A gateway that cannot intercept does not start, whenever it binds its proxy port.
        """
        # The CA's directory would be a regular file, so the CA can be neither read nor written.
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        node = nodes(
            Role.ALL, tls_mitm_ca_cert_path=str(blocker / "ca.crt"), tls_mitm_ca_key_path=str(blocker / "ca.key")
        )
        with pytest.raises(OSError):
            await node.start()

        assert node.readiness() == ["stopped"]
        assert node.background_tasks == []
        assert node.controller is not None and node.worker is not None and node.gateway is not None
        assert node.controller.tasks.tasks == [] and node.worker.tasks.tasks == []
        assert not node.gateway.proxy_server.is_listening
        # The controller let its lease go, and the process never advertised itself.
        assert await redis_client.client.get(LEASE_KEY.format(name=LeaseName.CONTROLLER)) != node.instance_id
        assert not await redis_client.client.exists(INSTANCE_REGISTRY_KEY.format(instance_id=node.instance_id))

    async def test_a_node_starts_once(self, nodes: Callable[..., Node]) -> None:
        node = nodes(Role.ECHO)
        await node.start()
        with pytest.raises(RuntimeError):
            await node.start()

    async def test_echo_opens_its_files_and_nothing_else(self, nodes: Callable[..., Node]) -> None:
        node = nodes(Role.ECHO)
        assert node.readiness() == ["not started"]

        await node.start()

        assert node.readiness() == []
        assert task_names(node) == ["loop_lag_monitor"]
        snapshot = node.snapshot()
        assert snapshot.runtime.role == Role.ECHO and snapshot.ready
        assert (snapshot.api, snapshot.gateway, snapshot.controller, snapshot.worker) == (None, None, None, None)


def _noting(calls: list[str], name: str, method: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
    """``method``, noting ``name`` in ``calls`` when it is called."""

    async def noted() -> None:
        calls.append(name)
        await method()

    return noted


class TestDefinitionsAtStart:
    async def test_an_api_seeds_the_tunnel_settings_rows_at_start_and_announces_only_those_it_wrote(
        self, nodes: Callable[..., Node], redis_client: RedisClient, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A controller already running compiles them at once, rather than at its next reconcile."""
        first = nodes(Role.API, instance_id="api-first")
        await first.start()

        async with db_session_factory() as session:
            assert await WireGuardSettingsRepository(session).get() is not None
            assert await OpenVpnSettingsRepository(session).get() is not None
        entries = await redis_client.client.xrange(CHANGES_STREAM_KEY)
        assert sorted(fields["kind"] for _, fields in entries) == ["openvpn", "wireguard"]

        again = nodes(Role.API, instance_id="api-again")
        await again.start()

        # The rows were there: nothing was written, so nothing is announced.
        assert len(await redis_client.client.xrange(CHANGES_STREAM_KEY)) == len(entries)

    @pytest.mark.parametrize("applied", [True, False])
    async def test_a_start_that_applied_a_migration_announces_that_anything_may_have_changed(
        self, nodes: Callable[..., Node], redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch, applied: bool
    ) -> None:
        """Processes already running read everything again; a start that found the schema at head announces nothing."""
        monkeypatch.setattr("api.roles.node.run_migrations", lambda _url: applied)
        node = nodes(Role.WORKER)

        await node.start()

        kinds = [fields["kind"] for _, fields in await redis_client.client.xrange(CHANGES_STREAM_KEY)]
        assert kinds == (["all"] if applied else [])

    async def test_a_controller_reads_the_descriptors_and_opens_no_ip_database(
        self, nodes: Callable[..., Node], db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """It looks no IP up, and nothing there re-reads the list: a file it opened would stay mapped once replaced."""
        data = write_mmdb(tmp_path / "stored.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()
        record = GeoDatabaseRecord(name="stored", sha256=hashlib.sha256(data).hexdigest())
        async with db_session_factory() as session:
            await ProviderDescriptorRepository(session).create(ProviderRecord(id="acme", name="Acme", spec=ACME))
            await GeoDatabaseRepository(session).create(record, data)
            await session.commit()
        controller = nodes(Role.CONTROLLER, instance_id="node-controller")
        api = nodes(Role.API, instance_id="node-api")

        await controller.start()
        await api.start()

        assert controller.registry is not None and controller.registry.get("acme") is not None
        assert controller.geo is not None and controller.geo.database_store.loaded == []
        # The api attributes (Detect, /echo), so it opens what is stored.
        assert api.geo is not None and api.geo.database_store.is_loaded(record.id)

    @pytest.mark.parametrize(("proxy_public_url", "hinted"), [("", True), ("https://proxy.example.com", False)])
    async def test_an_api_names_the_proxy_address_setting_while_it_is_unset(
        self, nodes: Callable[..., Node], proxy_public_url: str, hinted: bool
    ) -> None:
        """Outside role all the proxy may be published at another host than the web UI: a hint, not a warning."""
        node = nodes(Role.API, proxy_public_url=proxy_public_url)

        with structlog.testing.capture_logs() as logs:
            await node.start()

        hints = [log for log in logs if log["event"] == "The Connect page names the web UI's host as the proxy's"]
        assert [(log["log_level"], log["setting"]) for log in hints] == (
            [("info", "OCTOPROX_PROXY_PUBLIC_URL")] if hinted else []
        )


class TestApiJobs:
    """What the api runtime asks the workers for."""

    async def test_a_manual_ip_database_refresh_that_stores_a_new_file_asks_for_every_proxy_to_be_judged_again(
        self,
        nodes: Callable[..., Node],
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        data = write_mmdb(tmp_path / "country.mmdb", "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()

        async def downloaded(record: GeoDatabaseRecord, guard: object) -> bytes:
            return data

        monkeypatch.setattr("api.geo.updater.download", downloaded)
        record = GeoDatabaseRecord(
            name="vendor", source=GeoDatabaseSource.URL, update_url="https://download.vendor.test/db.mmdb"
        )
        async with db_session_factory() as session:
            await GeoDatabaseRepository(session).create(record, None)
            await session.commit()
        node = nodes(Role.API)
        await node.start()
        assert node.api is not None

        await node.api.geo_updater.refresh(record)

        assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == ["all"]
        # The same file again changes nothing, so nothing more is asked for.
        await node.api.geo_updater.refresh(record)
        assert await queued_job_keys(redis_client, JobKind.REATTRIBUTE) == ["all"]


class TestApiPreview:
    async def test_the_api_compiles_previews_with_no_store(self, nodes: Callable[..., Node]) -> None:
        """Its compiler is the api's, with no whole-feed compile, so no preview can write this process's stores."""
        node = nodes(Role.API)
        assert node.api is not None

        compiler = node.api._compiler

        assert type(compiler) is FeedCompiler
        assert not hasattr(compiler, "compile")

    async def test_a_preview_while_redis_cannot_be_read_is_built_without_quarantines_and_blocks(
        self, nodes: Callable[..., Node], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Connect page and the traffic split show less, not an error."""
        node = nodes(Role.API)
        await node.start()
        assert node.api is not None

        async def unreachable() -> dict[str, datetime]:
            raise RedisConnectionError("Redis is down")

        monkeypatch.setattr(node.api.state_reader, "quarantined", unreachable)
        monkeypatch.setattr(node.api.state_reader, "blocked", unreachable)

        with structlog.testing.capture_logs() as logs:
            router = await node.api.preview_router("no-such-project")

        assert router.snapshot.project("no-such-project") is None
        assert sorted((log["event"], log["state"]) for log in logs if log["log_level"] == "warning") == [
            ("Runtime state unavailable, shown without it", "quarantines"),
            ("Runtime state unavailable, shown without it", "traffic blocks"),
        ]


class StandIn:
    """The heartbeat's ``SELECT 1``, answered as the test says: with ``error``, and only once ``released``."""

    def __init__(self) -> None:
        self.error: str | None = None
        self.calls = 0
        self.released = asyncio.Event()
        self.released.set()

    async def __call__(self, engine: AsyncEngine) -> str | None:
        self.calls += 1
        await self.released.wait()
        return self.error


@pytest.fixture
def fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """A beat every 50 ms, each giving Postgres 100 ms to answer, with the counters from none."""
    monkeypatch.setattr("api.roles.node.INSTANCE_HEARTBEAT_INTERVAL", 0.05)
    monkeypatch.setattr("api.roles.node.DEPENDENCY_CHECK_SECONDS", 0.1)
    job_stats.reset()


@pytest.fixture
def postgres(fast_heartbeat: None, monkeypatch: pytest.MonkeyPatch) -> StandIn:
    """The heartbeat's SELECT 1, answered the stand-in's way."""
    stand_in = StandIn()
    monkeypatch.setattr(Heartbeat, "_postgres_answers", staticmethod(stand_in))
    return stand_in


def heartbeats() -> int:
    """Heartbeat cycles this process has run so far."""
    stats = job_stats.get(WorkerName.HEARTBEAT)
    return stats.runs if stats is not None else 0


class TestHeartbeat:
    async def test_the_heartbeat_is_a_periodic_loop_whose_first_beat_comes_at_once(
        self, nodes: Callable[..., Node], redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A beat every ``INSTANCE_HEARTBEAT_INTERVAL`` from the start of the one before, the first at the start."""
        monkeypatch.setattr("api.roles.node.INSTANCE_HEARTBEAT_INTERVAL", 60.0)
        job_stats.reset()
        node = nodes(Role.API)
        heartbeat = node._heartbeat
        assert isinstance(heartbeat, PeriodicLoop)
        assert (heartbeat.name, heartbeat.interval) == (WorkerName.HEARTBEAT, 60.0)

        await node.start()

        # Long before the interval is up.
        await until(lambda: heartbeats() >= 1, timeout=5)
        assert await redis_client.client.exists(INSTANCE_REGISTRY_KEY.format(instance_id=node.instance_id))
        stats = job_stats.get(WorkerName.HEARTBEAT)
        assert stats is not None
        assert (stats.interval_seconds, stats.runs, stats.idle_runs, stats.failures) == (60.0, 1, 0, 0)
        # Its row in the snapshot: the task's state, its counters and its catalogue entry.
        (row,) = [task for task in node.snapshot().tasks if task.name == WorkerName.HEARTBEAT]
        assert (row.state, row.interval_seconds, row.runs) == ("running", 60.0, 1)
        assert row.description == WORKERS[WorkerName.HEARTBEAT].description


class TestReadiness:
    async def test_reasons_name_the_store_then_the_runtime(
        self, nodes: Callable[..., Node], postgres: StandIn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node = nodes(Role.API)
        assert node.readiness() == ["not started"]
        await node.start()
        assert node.refresher is not None and node.api is not None
        await until(lambda: node.readiness() == [])

        postgres.error = "connection refused"
        monkeypatch.setattr(node.api, "readiness", lambda: "api not started")
        monkeypatch.setattr(node.refresher, "readiness", lambda: "definitions not loaded yet")

        # A fixed reason, never the error's text: the probes answer without sign-in.
        await until(lambda: "Postgres unreachable" in node.readiness())
        assert node.readiness() == [
            "Postgres unreachable",
            "api: api not started",
            "definitions: definitions not loaded yet",
        ]
        assert node.snapshot().ready is False

    @pytest.mark.parametrize("role", [Role.API, Role.CONTROLLER, Role.WORKER])
    async def test_a_postgres_role_is_unready_while_postgres_does_not_answer(
        self, nodes: Callable[..., Node], postgres: StandIn, role: Role
    ) -> None:
        node = nodes(role)
        await node.start()

        with structlog.testing.capture_logs() as logs:
            postgres.error = "connection refused"
            await until(lambda: "Postgres unreachable" in node.readiness())
            beats = heartbeats()
            await until(lambda: heartbeats() >= beats + 3)
            postgres.error = None
            await until(lambda: "Postgres unreachable" not in node.readiness())

        # Once as the streak began, however many beats it lasted.
        assert [log["error"] for log in logs if log["event"] == "Postgres check failed"] == ["connection refused"]

    async def test_a_select_that_does_not_answer_counts_in_time_and_is_not_asked_again_meanwhile(
        self, nodes: Callable[..., Node], postgres: StandIn, redis_client: RedisClient
    ) -> None:
        """A server gone silent: the beats go on advertising the process instead of waiting with the probe."""
        node = nodes(Role.API)
        await node.start()

        postgres.released.clear()
        await until(lambda: node.readiness() == ["Postgres unreachable"])
        calls, beats = postgres.calls, heartbeats()
        await until(lambda: heartbeats() >= beats + 3)
        assert await redis_client.client.ttl(INSTANCE_REGISTRY_KEY.format(instance_id=node.instance_id)) > 0
        # One probe hangs; the beats wait on it rather than piling up more.
        assert postgres.calls == calls

        postgres.released.set()

        await until(lambda: node.readiness() == [])

    async def test_a_select_still_waiting_when_the_process_stops_is_cancelled(
        self, nodes: Callable[..., Node], postgres: StandIn
    ) -> None:
        """A probe on a silent server would hold its connection until the kernel gives up: the stop cancels it."""
        node = nodes(Role.API)
        await node.start()
        postgres.released.clear()
        await until(lambda: node.readiness() == ["Postgres unreachable"])
        assert node._heartbeat is not None
        probe = node._heartbeat._postgres_probe
        assert probe is not None and not probe.done()

        await node.stop()

        await asyncio.wait([probe], timeout=1)
        assert probe.cancelled()

    async def test_a_redis_that_does_not_answer_in_time_makes_a_worker_unready(
        self, nodes: Callable[..., Node], fast_heartbeat: None, redis_client: RedisClient
    ) -> None:
        """The heartbeat's write waits the request deadline, not the 30 s a reply may take."""
        node = nodes(Role.WORKER, redis_request_timeout=0.1)
        await node.start()
        await until(lambda: node.readiness() == [])

        with structlog.testing.capture_logs() as logs:
            await redis_client.client.execute_command("CLIENT", "PAUSE", 1000, "WRITE")
            await until(lambda: node.readiness() == ["Redis unreachable"], timeout=0.8)
            beats = heartbeats()
            # Still beating while the pause lasts: each write gives up at the deadline.
            await until(lambda: heartbeats() >= beats + 2, timeout=0.8)
            await until(lambda: node.readiness() == [])

        # Logged once; a deadline's TimeoutError has no text, so the line and the failed beats name its type.
        assert [log["error"] for log in logs if log["event"] == "Instance heartbeat failed"] == ["TimeoutError"]
        stats = job_stats.get(WorkerName.HEARTBEAT)
        assert stats is not None and stats.last_error == "TimeoutError"

    @pytest.mark.parametrize(
        ("role", "unready"), [(Role.API, False), (Role.GATEWAY, False), (Role.CONTROLLER, True), (Role.WORKER, True)]
    )
    async def test_only_a_controller_or_a_worker_cannot_work_without_redis(
        self,
        nodes: Callable[..., Node],
        fast_heartbeat: None,
        monkeypatch: pytest.MonkeyPatch,
        role: Role,
        unready: bool,
    ) -> None:
        """An api serves its reads and writes without Redis, and a gateway routes on the feed it holds.

        A refused write is a failed beat, filed with the beat's own answer, and no cycle failure: the beat logs it once.
        """
        node = nodes(role)
        await node.start()
        assert node.redis_client is not None

        async def refused(*_: Any) -> None:
            raise ConnectionError("Redis down")

        monkeypatch.setattr(node.redis_client, "advertise_instance", refused)
        with structlog.testing.capture_logs() as logs:
            await until(lambda: (stats := job_stats.get(WorkerName.HEARTBEAT)) is not None and stats.failures >= 2)

        assert ("Redis unreachable" in node.readiness()) is unready
        stats = job_stats.get(WorkerName.HEARTBEAT)
        assert stats is not None and stats.last_error == "Redis down"
        assert stats.consecutive_failures >= 2 and stats.idle_runs == 0
        events = [log["event"] for log in logs]
        assert "Loop cycle failed" not in events
        assert events.count("Instance heartbeat failed") == 1


class TestHeartbeatReader:
    """The api reads the other processes' heartbeats: the System page, and each gateway's tunnel endpoints."""

    async def test_tunnel_endpoints_come_from_the_gateways_heartbeats(
        self, nodes: Callable[..., Node], redis_client: RedisClient
    ) -> None:
        node = nodes(Role.API)
        await node.start()
        assert node.api is not None
        # Its own first beat checks Postgres before it writes.
        await until(lambda: redis_client.client.exists(INSTANCE_STATS_KEY.format(instance_id=node.instance_id)))
        endpoint = TunnelEndpointState(
            protocol="wireguard", instance_id="gateway-1", state="running", listen_port=51820, active_connections=3
        )
        await redis_client.advertise_instance("gateway-1", "gateway", _gateway_heartbeat("gateway-1", [endpoint]))
        await redis_client.advertise_instance("gateway-2", "gateway", "{not a snapshot")
        await redis_client.advertise_instance("gateway-3", "gateway", None)

        snapshots = await node.api.instance_snapshots()
        assert {"gateway-1", node.instance_id} <= set(snapshots)
        assert "gateway-2" not in snapshots and "gateway-3" not in snapshots
        assert await node.api.tunnel_endpoints() == [endpoint]
        status = await node.api.wireguard_service.status()
        assert [gateway.instance_id for gateway in status.gateways] == ["gateway-1"]
        assert (await node.api.openvpn_service.status()).gateways == []


def _gateway_heartbeat(instance_id: str, tunnels: list[TunnelEndpointState]) -> str:
    """What a gateway publishes on its heartbeat, with its tunnel endpoints."""
    gateway = GatewaySnapshot(
        listener_port=8080,
        listening=True,
        active_connections=0,
        retired_slots=0,
        local_quarantines=0,
        in_flight=0,
        requests=0,
        successes=0,
        failures=0,
        bytes_sent=0,
        bytes_received=0,
        failures_by_kind={},
        pending_metric_deltas=0,
        metric_flush_failures=0,
        rate_limit_errors=0,
        preflight_checks=0,
        preflight_rejections=0,
        geo_databases_loaded=[],
        geo_database_errors={},
        geo_databases_missing=[],
        observations_pending=0,
        observations_dropped=0,
        sightings_pending=0,
        sightings_dropped=0,
        failure_log_dropped=0,
        mitm_captures_dropped=0,
        tunnels=tunnels,
    )
    snapshot = InstanceSnapshot.model_validate(
        {
            "runtime": {"version": "6.0.0", "role": "gateway", "instance_id": instance_id, "pid": 1},
            "ready": True,
            "gateway": gateway.model_dump(mode="json"),
        }
    )
    return snapshot.model_dump_json()
