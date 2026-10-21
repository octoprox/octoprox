# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The gateway runtime, composed as the node composes it for role ``gateway``, against the real Redis and no Postgres.

The real feed publisher stands in for the controller, under a lease the fixture takes, and requests go through the
gateway's real proxy port to an upstream proxy served by the test.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog

from api.core import utc_now
from api.core.config import Settings
from api.core.failure_log import build_failure
from api.core.job_stats import job_stats
from api.core.roles import Role
from api.core.signals import request_completed, request_failed
from api.core.stats import FailureKind
from api.core.tasks import TaskSet
from api.core.workers import LeaseName, WorkerName
from api.db import streams
from api.db.redis import FAILURE_LOG_KEY, LEASE_KEY, PROXY_WINDOWS, RedisClient
from api.feed.codec import FeedCodec
from api.feed.models import GeoDatabaseFeed
from api.feed.publisher import FeedPublisher
from api.feed.state import RuntimeStateReader
from api.geo import store as store_module
from api.geo.models import GeoSettings
from api.geo.runtime import GeoStack
from api.geo.store import BlobMissingError, BlobSource, DatabaseRetry, HttpBlobSource
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol
from api.models.tunnel import TunnelEndpointState
from api.providers.registry import ProviderRegistry, build_registry
from api.providers.store import ProviderStore
from api.roles import gateway as gateway_module
from api.roles.gateway import GatewayRuntime
from api.strategies.health_based import HEALTH_MIN_SAMPLES
from tests.feed.test_compiler import ACME
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb
from tests.routing.builder import FeedBuilder, healthy
from tests.waits import running, until

SECRET = "gateway-test-secret"
OWNER = "controller-1"


class _ConnectProxy:
    """An upstream HTTP proxy: answers CONNECT, records the target and echoes the tunnel's bytes."""

    def __init__(self) -> None:
        self.targets: list[str] = []
        self.server: asyncio.Server | None = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.targets.append((await reader.readline()).decode().split(" ")[1])
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        writer.close()

    @property
    def port(self) -> int:
        assert self.server is not None
        port: int = self.server.sockets[0].getsockname()[1]
        return port


@pytest.fixture
async def upstream() -> AsyncIterator[_ConnectProxy]:
    proxy = _ConnectProxy()
    proxy.server = await asyncio.start_server(proxy._serve, "127.0.0.1", 0)
    yield proxy
    proxy.server.close()
    await proxy.server.wait_closed()


@pytest.fixture
def registry() -> ProviderRegistry:
    return build_registry(include_plugins=False)


@pytest.fixture
def feed(upstream: _ConnectProxy, registry: ProviderRegistry) -> FeedBuilder:
    """One project with one static connector whose one proxy is the test's upstream."""
    return FeedBuilder(registry).add(
        Project(id="project-1", name="P", username="client", password="secret"),
        Credential(
            id="cred-1", name="Static", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1", config={}
        ),
        Connector(
            id="conn-1", name="Static", credential_id="cred-1",
            credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="project-1",
        ),
        healthy(Proxy(
            id="px-1", host="127.0.0.1", port=upstream.port, protocol=ProxyProtocol.HTTP, connector_id="conn-1"
        )),
    )


@pytest.fixture
async def publish(redis_client: RedisClient) -> Callable[..., Any]:
    """Publishes the feed's sections as the controller holding the lease does."""
    lease_key = LEASE_KEY.format(name=LeaseName.CONTROLLER)
    await redis_client.client.set(lease_key, OWNER)
    publisher = FeedPublisher(redis_client, FeedCodec(SECRET), lease_key=lease_key, owner_id=OWNER)

    async def publish(feed: FeedBuilder, *, changes: str, **global_fields: Any) -> None:
        sections = feed.sections()
        for name, value in global_fields.items():
            setattr(sections["global"], name, value)
        await publisher.publish(sections, changes=changes, compiled_at=utc_now())

    return publish


def _settings(test_redis_url: str, tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "role": Role.GATEWAY,
        "redis_url": test_redis_url,
        "host": "127.0.0.1",
        "proxy_port": 0,
        "internal_secret": SECRET,
        "internal_api_url": "http://api.internal.test:8000",
        "geo_cache_dir": str(tmp_path / "geo"),
        "tls_mitm_ca_cert_path": str(tmp_path / "ca" / "ca.crt"),
        "tls_mitm_ca_key_path": str(tmp_path / "ca" / "ca.key"),
    }
    return Settings(_env_file=None, **{**values, **overrides})  # type: ignore[call-arg]


class Gateways:
    """Builds gateway runtimes the way the Node does for role ``gateway``, and stops them after the test."""

    def __init__(self, redis_client: RedisClient, registry: ProviderRegistry, redis_url: str, tmp_path: Path) -> None:
        self.redis_client = redis_client
        self.registry = registry
        self.redis_url = redis_url
        self.tmp_path = tmp_path
        self.built: list[tuple[GatewayRuntime, GeoStack]] = []

    def build(
        self, *, blob_source: BlobSource | None = None, redis_client: RedisClient | None = None, **settings: Any
    ) -> GatewayRuntime:
        """A gateway with ``settings`` over the test's.

        IP database files come from the api unless ``blob_source``; the
        process's Redis client is the test's unless ``redis_client``.
        """
        config = _settings(self.redis_url, self.tmp_path, **settings)
        client = redis_client or self.redis_client
        geo = GeoStack.build(
            config,
            session_factory=None,
            redis_client=client,
            blob_source=blob_source or HttpBlobSource.from_settings(config),
        )
        runtime = GatewayRuntime(
            config, redis_client=client, provider_store=ProviderStore(self.registry, None), geo=geo
        )
        self.built.append((runtime, geo))
        return runtime

    async def stop(self) -> None:
        """Stop each runtime, then close its stack, as the Node does."""
        for runtime, geo in self.built:
            await runtime.stop()
            await geo.close()


@pytest.fixture
async def gateways(
    redis_client: RedisClient, registry: ProviderRegistry, test_redis_url: str, tmp_path: Path
) -> AsyncIterator[Gateways]:
    built = Gateways(redis_client, registry, test_redis_url, tmp_path)
    yield built
    await built.stop()


@pytest.fixture(autouse=True)
def _quick_retries(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The follower tries a failed update again a twentieth of a second later, not a second; each test counts anew."""
    monkeypatch.setattr(streams, "RETRY_FIRST_SECONDS", 0.05)
    job_stats.reset()
    yield
    job_stats.reset()


def _free_port() -> int:
    """A port nothing listens on: bound and let go at once, so the gateway can be given it before it binds."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


async def _connect(gateway: GatewayRuntime, target: str, payload: bytes, *, username: str = "client") -> bytes:
    """CONNECT through the gateway's proxy port and send ``payload``; everything the client got back."""
    token = base64.b64encode(f"{username}:secret".encode()).decode()
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway.proxy_server.port)
    writer.write(f"CONNECT {target} HTTP/1.1\r\nProxy-Authorization: Basic {token}\r\n\r\n".encode() + payload)
    await writer.drain()
    writer.write_eof()
    received = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return received


class TestServing:
    async def test_a_connect_is_routed_by_the_feed_metered_and_flushed_at_stop(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, upstream: _ConnectProxy, redis_client: RedisClient
    ) -> None:
        gateway = gateways.build()
        await publish(feed, changes="1700000000000-1")
        await gateway.start()
        await until(lambda: gateway.readiness() is None)

        received = await _connect(gateway, "example.com:443", b"ping")

        assert received == b"HTTP/1.1 200 Connection Established\r\n\r\nping"
        assert upstream.targets == ["example.com:443"]
        await until(lambda: gateway.metering.totals.request_count == 1)
        assert gateway.inflight.total == 0
        snapshot = gateway.snapshot()
        assert (snapshot.requests, snapshot.successes, snapshot.bytes_sent, snapshot.bytes_received) == (1, 1, 4, 4)
        assert snapshot.feed_changes == "1700000000000-1" and snapshot.listening
        assert snapshot.listener_port == gateway.proxy_server.port

        # Stopping hands what was counted to Redis, where a worker's metrics flush takes it.
        await gateway.stop()
        window = (await RuntimeStateReader(redis_client).windows(PROXY_WINDOWS, ["px-1"]))["px-1"]
        assert (window.request_count, window.success_count, window.bytes_sent, window.bytes_received) == (1, 1, 4, 4)

    async def test_a_change_published_reaches_the_router(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, upstream: _ConnectProxy
    ) -> None:
        gateway = gateways.build()
        await publish(feed, changes="1-0")
        await gateway.start()
        await until(lambda: gateway.readiness() is None)
        before = gateway.router.snapshot

        feed.add(healthy(Proxy(id="px-2", host="127.0.0.1", port=upstream.port, connector_id="conn-1")))
        await publish(feed, changes="2-0")
        # The publish's entry wakes the follower: no poll interval, and an hour to its own next read.
        await until(lambda: gateway.router.snapshot is not before, timeout=1.0)

        assert gateway.router.snapshot.proxy("px-2") is not None
        assert gateway.follower.applied_head is not None and gateway.follower.applied_head.changes == "2-0"
        assert gateway.snapshot().feed_changes == "2-0"
        # Only the connector changed: the projects' index was carried over.
        assert gateway.router.get_project("project-1") is before.project("project-1")
        # Read in full once, at start; the change came as an entry.
        stream = gateway.streams()["feed"]
        assert (stream.full_reads, stream.entries) == ({"start": 1}, 1)

    async def test_a_refused_request_is_counted_by_kind_and_logged(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, redis_client: RedisClient
    ) -> None:
        gateway = gateways.build()
        await publish(feed, changes="1-0")
        await gateway.start()
        await until(lambda: gateway.readiness() is None)

        received = await _connect(gateway, "example.com:443", b"", username="client-cc-jp")

        assert received.startswith(b"HTTP/1.1 502")
        assert gateway.snapshot().failures_by_kind == {"no_proxy_available": 1}
        await gateway.stop()
        stream = FAILURE_LOG_KEY.format(project_id="project-1", kind="no_proxy_available")
        assert await redis_client.client.xlen(stream) == 1

    async def test_a_disabled_failure_log_keeps_no_examples_but_the_kind_is_counted(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, redis_client: RedisClient
    ) -> None:
        gateway = gateways.build(failure_log_enabled=False)
        await publish(feed, changes="1-0")
        await gateway.start()
        await until(lambda: gateway.readiness() is None)

        received = await _connect(gateway, "example.com:443", b"", username="client-cc-jp")

        assert received.startswith(b"HTTP/1.1 502")
        assert gateway.snapshot().failures_by_kind == {"no_proxy_available": 1}
        # No publisher loop runs for it.
        assert WorkerName.FAILURE_LOG_PUBLISHER not in [task.get_name() for task in gateway.background_tasks]
        await gateway.stop()
        stream = FAILURE_LOG_KEY.format(project_id="project-1", kind="no_proxy_available")
        assert await redis_client.client.xlen(stream) == 0

    async def test_the_proxy_port_refuses_connections_until_a_feed_is_applied(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, upstream: _ConnectProxy
    ) -> None:
        """With no feed a gateway knows no project and would answer every client 407, so it does not listen yet.

        A refused connection is what makes a balancer pass the client on to
        a gateway that has a feed.
        """
        port = _free_port()
        gateway = gateways.build(proxy_port=port)
        await gateway.start()

        assert gateway.readiness() == "feed not applied yet"
        snapshot = gateway.snapshot()
        assert not snapshot.listening and snapshot.feed_digest is None and snapshot.tunnels == []
        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_connection("127.0.0.1", port)

        await publish(feed, changes="1-0")
        await until(lambda: gateway.readiness() is None)
        assert gateway.cert_manager.loaded and gateway.snapshot().listening and gateway.proxy_server.port == port
        received = await _connect(gateway, "example.com:443", b"ping")
        assert received == b"HTTP/1.1 200 Connection Established\r\n\r\nping"

    async def test_a_feed_applied_while_the_gateway_stops_binds_no_listener(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any
    ) -> None:
        """The follower may be applying the first feed when stop() begins: the port is never bound."""
        gateway = gateways.build()
        await gateway.start()
        await gateway.stop()
        await publish(feed, changes="1-0")

        with structlog.testing.capture_logs() as logs:
            # What the follower had in flight when stop() began.
            assert await gateway.follower.poll()

        assert gateway.router.snapshot.project("project-1") is not None
        assert not gateway.proxy_server.is_listening
        assert "Proxy server started" not in [entry["event"] for entry in logs]

    async def test_a_stop_that_begins_while_the_port_is_being_bound_closes_it(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stop() finds no listener to close while the bind is under way, so the apply closes what it bound."""
        gateway = gateways.build()
        with structlog.testing.capture_logs() as logs:
            await gateway.start()
            # The follower's own first read finds no feed; it is then told to stop, so the test applies the feed.
            await until(lambda: any(entry["event"].startswith("Routing feed head missing") for entry in logs))
        await gateway.follower.stop()
        bind = gateway.proxy_server.start

        async def bind_while_stopping() -> None:
            await gateway.stop()
            await bind()

        monkeypatch.setattr(gateway.proxy_server, "start", bind_while_stopping)
        await publish(feed, changes="1-0")

        assert await gateway.follower.poll()

        assert not gateway.proxy_server.is_listening


class TestApply:
    async def test_the_global_section_reaches_the_registry_and_the_attribution_settings(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, registry: ProviderRegistry
    ) -> None:
        gateway = gateways.build()
        await publish(
            feed, changes="1-0", descriptors=[ACME],
            geo_settings=GeoSettings(preflight_max_attempts=5), geo_settings_stored=True,
        )
        await gateway.start()
        await until(lambda: gateway.readiness() is None)

        assert registry.get("acme") is not None
        assert gateway.preflight.max_attempts == 5
        _, geo = gateways.built[-1]
        assert geo.settings_store.from_database

    async def test_a_quarantine_the_feed_carries_takes_over_the_local_one(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any
    ) -> None:
        gateway = gateways.build()
        await publish(feed, changes="1-0")
        await gateway.start()
        await until(lambda: gateway.readiness() is None)
        ends = utc_now() + timedelta(minutes=5)
        gateway.exclusions.quarantine("px-1", ends)
        assert gateway.snapshot().local_quarantines == 1

        feed.quarantined["px-1"] = ends
        await publish(feed, changes="2-0")
        await until(lambda: gateway.snapshot().local_quarantines == 0)
        assert gateway.router.snapshot.quarantined == {"px-1": ends}

    async def test_the_tunnel_endpoints_follow_the_global_section(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gateway_module, "WireGuardEndpoint", _RecordingEndpoint)
        gateway = gateways.build(wireguard_enabled=True, tunnel_transparent_port=0, tunnel_dns_port=0)
        await publish(feed, changes="1-0")
        await gateway.start()
        await until(lambda: gateway.readiness() is None)
        endpoint = gateway.wireguard
        assert isinstance(endpoint, _RecordingEndpoint)

        assert endpoint.started and endpoint.applied == [None]
        assert gateway.tunnel is not None and gateway.tunnel.transparent_server is not None
        assert [state.protocol for state in gateway.snapshot().tunnels] == ["wireguard"]
        assert WorkerName.TUNNEL_RECONCILER in [task.get_name() for task in gateway.background_tasks]
        await gateway.stop()
        assert endpoint.stopped


class TestOutcomes:
    async def test_the_router_reads_the_outcomes_the_gateways_metering_recorded(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, upstream: _ConnectProxy
    ) -> None:
        """``health_based`` routes on what this gateway's own metering saw through each proxy."""
        feed.projects["project-1"].routing_strategy = "health_based"
        feed.add(healthy(Proxy(id="px-2", host="127.0.0.1", port=upstream.port, connector_id="conn-1")))
        gateway = gateways.build()
        await publish(feed, changes="1-0")
        await gateway.start()
        await until(lambda: gateway.readiness() is None)

        for _ in range(HEALTH_MIN_SAMPLES):
            gateway.metering.record_completed("px-1", "project-1", True, 10.0, 0, 0)
            gateway.metering.record_completed("px-2", "project-1", False, 900.0, 0, 0)

        picks = set()
        for _ in range(20):
            selected = await gateway.router.select_proxy_for_project("project-1")
            assert selected is not None
            picks.add(selected.id)
        assert picks == {"px-1"}

    async def test_a_proxy_the_feed_drops_loses_its_outcomes(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, upstream: _ConnectProxy
    ) -> None:
        feed.add(healthy(Proxy(id="px-2", host="127.0.0.1", port=upstream.port, connector_id="conn-1")))
        gateway = gateways.build()
        await publish(feed, changes="1-0")
        await gateway.start()
        await until(lambda: gateway.readiness() is None)
        gateway.metering.record_completed("px-1", "project-1", True, 10.0, 0, 0)
        gateway.metering.record_completed("px-2", "project-1", True, 10.0, 0, 0)

        del feed.proxies["px-2"]
        await publish(feed, changes="2-0")
        await until(lambda: gateway.router.snapshot.proxy("px-2") is None)

        assert gateway.outcomes.of_proxy("px-1") == (1, 1, 10.0)
        assert gateway.outcomes.of_proxy("px-2") == (0, 0, 0.0)


class _HeldBlobs:
    """IP database files that arrive only once the test lets them through, as a slow api would send them.

    It knows each file by its checksum, any other missing as the api's answer would be, and records the fetches that
    completed, in order, and the most that waited at once.
    """

    def __init__(self, *files: bytes) -> None:
        self._files = {hashlib.sha256(data).hexdigest(): data for data in files}
        self.released = asyncio.Event()
        self.waiting = 0
        self.most_waiting = 0
        self.fetched: list[str] = []

    async def fetch(self, database_id: str, sha256: str, path: Path) -> None:
        if sha256 not in self._files:
            raise BlobMissingError("the api has no file at the requested checksum")
        self.waiting += 1
        self.most_waiting = max(self.most_waiting, self.waiting)
        try:
            await self.released.wait()
        finally:
            self.waiting -= 1
        path.write_bytes(self._files[sha256])
        self.fetched.append(database_id)


def _listed(database_id: str, data: bytes) -> GeoDatabaseFeed:
    """A stored IP database as the feed lists it, at the checksum of ``data``."""
    return GeoDatabaseFeed(
        id=database_id, name=database_id, vendor="maxmind", kind="country", format="mmdb", source="upload",
        enabled=True, priority=100, sha256=hashlib.sha256(data).hexdigest(), created_at=utc_now(),
    )


class TestIpDatabases:
    async def test_downloads_hold_back_neither_routing_nor_the_next_update(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, upstream: _ConnectProxy, tmp_path: Path
    ) -> None:
        """The store syncs one list at a time, in the background; a list it has not taken yet gives way to a newer."""
        records = {"81.2.69.0/24": MAXMIND_RECORD}
        first = write_mmdb(tmp_path / "first.mmdb", "GeoLite2-Country", records, description="first").read_bytes()
        second = write_mmdb(tmp_path / "second.mmdb", "GeoLite2-Country", records, description="second").read_bytes()
        blobs = _HeldBlobs(first, second)
        gateway = gateways.build(blob_source=blobs)
        await publish(feed, changes="1-0", geo_databases=[_listed("stored-a", first)])
        await gateway.start()

        # Applied and routing while its database is still on the way.
        await until(lambda: gateway.follower.applied_head is not None and blobs.waiting == 1)
        assert gateway.readiness() == "IP databases not loaded yet: stored-a"
        assert await _connect(gateway, "example.com:443", b"ping") == b"HTTP/1.1 200 Connection Established\r\n\r\nping"

        # Two more updates apply meanwhile; the second's list replaces the first's, which the store never took.
        feed.add(healthy(Proxy(id="px-2", host="127.0.0.1", port=upstream.port, connector_id="conn-1")))
        await publish(feed, changes="2-0", geo_databases=[_listed("stored-a", first), _listed("stored-b", second)])
        await until(lambda: gateway.snapshot().feed_changes == "2-0")
        assert gateway.router.snapshot.proxy("px-2") is not None
        await publish(feed, changes="3-0", geo_databases=[_listed("stored-a", first), _listed("stored-c", second)])
        await until(lambda: gateway.snapshot().feed_changes == "3-0")
        assert gateway.readiness() == "IP databases not loaded yet: stored-a, stored-c"

        blobs.released.set()
        await until(lambda: gateway.readiness() is None)
        assert blobs.fetched == ["stored-a", "stored-c"] and blobs.most_waiting == 1
        assert gateway.snapshot().geo_databases_loaded == ["stored-a", "stored-c"]

        # Loaded once, a gateway stays ready while a database added later cannot be had yet.
        unknown = second + b"\0"
        await publish(feed, changes="4-0", geo_databases=[_listed("stored-a", first), _listed("stored-d", unknown)])
        await until(lambda: gateway.snapshot().geo_databases_missing == ["stored-d"])
        assert gateway.readiness() is None

    async def test_a_sync_cut_by_a_deadline_is_a_failed_run_logged_by_its_type(
        self, gateways: Gateways, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare TimeoutError has no text, so the line names its type; the same list is synced again after the back-off."""
        gateway = gateways.build()
        sync = AsyncMock(side_effect=[TimeoutError, None])
        monkeypatch.setattr(gateway._geo.database_store, "sync_records", sync)
        loop = gateway.database_sync
        listed = [_listed("stored-a", b"a")]

        with structlog.testing.capture_logs() as logs:
            async with running(loop):
                loop.offer(listed)
                await until(lambda: sync.await_count == 2)

        assert [awaited.args for awaited in sync.await_args_list] == [(listed,), (listed,)]
        assert loop.missing() == []
        lines = [(log["event"], log.get("error")) for log in logs if log.get("loop") == WorkerName.GEO_DATABASE_SYNC]
        assert lines == [("Loop cycle failed", "TimeoutError"), ("Loop recovered", None)]
        stats = job_stats.get(WorkerName.GEO_DATABASE_SYNC)
        assert stats is not None and (stats.failures, stats.consecutive_failures) == (1, 0)

    async def test_a_list_offered_while_a_sync_fails_is_synced_in_its_place(
        self, gateways: Gateways, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gateway = gateways.build()
        failing, newer = [_listed("stored-a", b"a")], [_listed("stored-b", b"b")]
        fail = asyncio.Event()

        async def sync_records(listed: list[GeoDatabaseFeed]) -> None:
            if listed is failing:
                await fail.wait()
                raise TimeoutError

        sync = AsyncMock(side_effect=sync_records)
        monkeypatch.setattr(gateway._geo.database_store, "sync_records", sync)
        loop = gateway.database_sync

        async with running(loop):
            loop.offer(failing)
            await until(lambda: sync.await_count == 1)
            loop.offer(newer)
            fail.set()
            await until(lambda: sync.await_count == 2)

        assert [awaited.args for awaited in sync.await_args_list] == [(failing,), (newer,)]
        assert loop.missing() == []

    async def test_a_sync_the_cache_directory_fails_is_synced_again_once_it_can_be_made(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, tmp_path: Path
    ) -> None:
        """An OSError leaves the store no failure to retry, so only the list kept offered brings the database."""
        data = write_mmdb(tmp_path / "a.mmdb", "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()
        blobs = _HeldBlobs(data)
        blobs.released.set()
        blocker = tmp_path / "blocked"
        blocker.write_text("a file where the cache directory's parent should be")
        gateway = gateways.build(blob_source=blobs, geo_cache_dir=str(blocker / "geo"))
        await publish(feed, changes="1-0", geo_databases=[_listed("stored-a", data)])
        await gateway.start()

        await until(
            lambda: gateway.follower.applied_head is not None
            and (stats := job_stats.get(WorkerName.GEO_DATABASE_SYNC)) is not None
            and stats.failures >= 2
        )
        assert gateway.readiness() == "IP databases not loaded yet: stored-a"
        assert gateway.snapshot().geo_database_errors == {}

        # No feed moves: the sync after the next back-off makes the directory and brings the file.
        blocker.unlink()
        await until(lambda: gateway.readiness() is None)
        assert gateway.snapshot().geo_databases_loaded == ["stored-a"] and blobs.fetched == ["stored-a"]
        stats = job_stats.get(WorkerName.GEO_DATABASE_SYNC)
        assert stats is not None and stats.consecutive_failures == 0


class _RecordingEndpoint:
    """A WireGuard endpoint that records what the gateway asks of it and touches no interface."""

    def __init__(self, settings: Settings, plane: Any, redis_client: RedisClient) -> None:
        self.applied: list[Any] = []
        self.started = self.stopped = False
        self.tasks = TaskSet()

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def apply(self, feed: Any) -> None:
        self.applied.append(feed)

    async def reconcile(self) -> int:
        return 0

    def state(self) -> TunnelEndpointState:
        return TunnelEndpointState(protocol="wireguard", instance_id="gateway-1", state="starting")


class TestStart:
    @pytest.mark.parametrize(
        ("role", "warned"), [(Role.GATEWAY, True), (Role.ALL, False)]
    )
    async def test_a_gateway_left_at_the_default_api_url_says_so(
        self, gateways: Gateways, role: Role, warned: bool
    ) -> None:
        gateway = gateways.build(role=role, internal_api_url=Settings.model_fields["internal_api_url"].default)
        with structlog.testing.capture_logs() as logs:
            await gateway.start()

        events = [entry["event"] for entry in logs if entry["log_level"] == "warning"]
        assert ("Gateway reaches the api at the default internal_api_url" in events) is warned

    async def test_a_taken_proxy_port_keeps_the_gateway_unready_until_it_is_free(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any
    ) -> None:
        """The first feed binds the port: a taken one fails that apply, and the follower tries it again on its own."""
        taken = await asyncio.start_server(lambda _reader, _writer: None, "127.0.0.1", 0)
        port = taken.sockets[0].getsockname()[1]
        gateway = gateways.build(proxy_port=port)
        await publish(feed, changes="1-0")
        try:
            with structlog.testing.capture_logs() as logs:
                await gateway.start()
                await until(
                    lambda: any(
                        entry["event"] == "Loop cycle failed" and entry.get("loop") == WorkerName.FEED_FOLLOWER
                        for entry in logs
                    )
                )
            assert gateway.readiness() == "feed not applied yet"
            assert not gateway.proxy_server.is_listening
        finally:
            taken.close()
            await taken.wait_closed()

        await until(lambda: gateway.readiness() is None)
        assert gateway.proxy_server.port == port and gateway.snapshot().feed_changes == "1-0"

        await gateway.stop()
        assert gateway.tasks.tasks == []
        assert not gateway.proxy_server.is_listening
        # Nothing reaches a stopped gateway any more, not even from its own listener.
        failure = build_failure("project-1", FailureKind.NO_PROXY_AVAILABLE, 502, "no proxy")
        await request_failed.send_async(gateway.proxy_server, failure=failure)
        assert gateway.snapshot().failures_by_kind == {}

    async def test_a_failed_start_is_cleaned_up_by_stop(self, gateways: Gateways, tmp_path: Path) -> None:
        """A CA that cannot be loaded fails the start, and stop() after it leaves nothing running or listening."""
        (tmp_path / "ca").mkdir()
        (tmp_path / "ca" / "ca.crt").write_text("not a certificate")
        (tmp_path / "ca" / "ca.key").write_text("not a key")
        gateway = gateways.build()
        with pytest.raises(ValueError):
            await gateway.start()

        await gateway.stop()

        assert gateway.tasks.tasks == []
        assert not gateway.proxy_server.is_listening


class TestStop:
    async def test_the_listeners_close_before_the_loops_stop_and_the_tunnels_go_down_after(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No request is counted after its publisher has sent its last window, and no feed reaches a stopped endpoint."""
        monkeypatch.setattr(gateway_module, "WireGuardEndpoint", _RecordingEndpoint)
        gateway = gateways.build(wireguard_enabled=True, tunnel_transparent_port=0, tunnel_dns_port=0)
        await publish(feed, changes="1-0")
        await gateway.start()
        await until(lambda: gateway.readiness() is None)
        assert gateway.tunnel is not None and gateway.tunnel.transparent_server is not None
        assert gateway.wireguard is not None
        steps: list[str] = []

        def recorded(step: str, target: Any) -> None:
            stop = target.stop

            async def stop_recorded() -> None:
                # The first stop of each: the data plane closes its listener again as it stops.
                if step not in steps:
                    steps.append(step)
                await stop()

            monkeypatch.setattr(target, "stop", stop_recorded)

        recorded("proxy listener", gateway.proxy_server)
        recorded("tunnel listener", gateway.tunnel.transparent_server)
        for loop in gateway.loops():
            recorded(loop.name, loop)
        recorded("tunnel endpoint", gateway.wireguard)
        recorded("tunnel data plane", gateway.tunnel)

        await gateway.stop()

        assert steps[:2] == ["proxy listener", "tunnel listener"]
        assert sorted(steps[2:7]) == sorted(
            [
                WorkerName.GEO_DATABASE_SYNC,
                WorkerName.FEED_FOLLOWER,
                WorkerName.REQUEST_METERING,
                WorkerName.FAILURE_LOG_PUBLISHER,
                WorkerName.SIGHTING_PUBLISHER,
            ]
        )
        assert steps[7:] == ["tunnel endpoint", "tunnel data plane"]
        # Every loop and task has ended, the reconciler included.
        assert gateway.tasks.tasks == []

    async def test_its_loops_run_as_tasks_named_after_them_beside_its_own_tasks(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any
    ) -> None:
        gateway = gateways.build()
        await publish(feed, changes="1-0")
        await gateway.start()
        await until(lambda: gateway.readiness() is None)

        assert [task.get_name() for task in gateway.background_tasks] == [
            WorkerName.GEO_DATABASE_SYNC,
            WorkerName.FEED_FOLLOWER,
            WorkerName.REQUEST_METERING,
            WorkerName.FAILURE_LOG_PUBLISHER,
            WorkerName.SIGHTING_PUBLISHER,
        ]


class TestListeners:
    async def test_two_gateways_in_one_process_each_count_only_the_requests_they_carried(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any
    ) -> None:
        """Each hears its own listeners alone, so a request one carries is counted, and can trip a rate limit, there only."""
        carrying, other = gateways.build(), gateways.build()
        await publish(feed, changes="1-0")
        for gateway in (carrying, other):
            await gateway.start()
        await until(lambda: carrying.readiness() is None and other.readiness() is None)

        assert (await _connect(carrying, "example.com:443", b"ping")).endswith(b"ping")
        refused = await _connect(carrying, "example.com:443", b"", username="client-cc-jp")
        assert refused.startswith(b"HTTP/1.1 502")

        await until(lambda: carrying.metering.totals.request_count == 1)
        assert carrying.snapshot().failures_by_kind == {"no_proxy_available": 1}
        assert (other.snapshot().requests, other.snapshot().failures_by_kind) == (0, {})

    async def test_its_mitm_handler_and_its_tunnel_listener_are_heard_too(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gateway_module, "WireGuardEndpoint", _RecordingEndpoint)
        gateway = gateways.build(wireguard_enabled=True, tunnel_transparent_port=0, tunnel_dns_port=0)
        await publish(feed, changes="1-0")
        await gateway.start()
        await until(lambda: gateway.readiness() is None)
        tunnel = gateway.tunnel.transparent_server if gateway.tunnel is not None else None
        assert tunnel is not None
        failure = build_failure("project-1", FailureKind.NO_PROXY_AVAILABLE, 502, "no proxy")

        await request_failed.send_async(gateway.mitm_handler, failure=failure)
        await request_failed.send_async(tunnel, failure=failure)
        await request_completed.send_async(
            tunnel, proxy_id="px-1", project_id="project-1", success=True, latency_ms=1.0, bytes_sent=1,
            bytes_received=1, peer_id="device-1",
        )

        assert gateway.snapshot().failures_by_kind == {"no_proxy_available": 2}
        assert gateway.metering.totals.request_count == 1


class TestOneRedisClient:
    async def test_a_paused_redis_costs_a_request_its_deadline_and_the_counts_land_once_it_answers(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, redis_client: RedisClient, test_redis_url: str
    ) -> None:
        """Every call goes through the process's one client; the request path's run under its request deadline."""
        feed.projects["project-1"].routing_strategy = "sticky"
        client = RedisClient(test_redis_url, request_timeout=0.3)
        await client.connect()
        try:
            gateway = gateways.build(redis_client=client)
            assert not hasattr(gateway, "request_redis")
            await publish(feed, changes="1-0")
            await gateway.start()
            await until(lambda: gateway.readiness() is None)

            await redis_client.client.client_pause(1500, all=True)
            loop = asyncio.get_running_loop()
            started = loop.time()
            received = await asyncio.wait_for(
                _connect(gateway, "example.com:443", b"ping", username="client-sessid-s1"), 3
            )

            # The binding could be neither read nor written in time, so the session was placed by hashing.
            assert received == b"HTTP/1.1 200 Connection Established\r\n\r\nping"
            assert loop.time() - started < 1.0
            assert await redis_client.get_sticky_binding("project-1", "s1") is None
            # The counts waited in memory for Redis, and land once it answers.
            await gateway.stop()
            window = (await RuntimeStateReader(redis_client).windows(PROXY_WINDOWS, ["px-1"]))["px-1"]
            assert window.request_count == 1
        finally:
            await client.close()


class _FlakyBlobs:
    """IP database files the api cannot hand over until the test says it can, as an api still starting would."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.serving = False
        self.attempts = 0

    async def fetch(self, database_id: str, sha256: str, path: Path) -> None:
        self.attempts += 1
        if not self.serving:
            raise ConnectionError("the api is not answering yet")
        path.write_bytes(self._data)


class TestDatabaseRetry:
    async def test_a_gateway_started_before_the_api_turns_ready_once_the_retry_gets_the_file(
        self, gateways: Gateways, feed: FeedBuilder, publish: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The node runs the retry beside the gateway's runtime, over the same store."""
        monkeypatch.setattr(store_module, "GEO_BLOB_RETRY_SECONDS", 0.05)
        data = write_mmdb(tmp_path / "a.mmdb", "GeoLite2-Country", {"81.2.69.0/24": MAXMIND_RECORD}).read_bytes()
        blobs = _FlakyBlobs(data)
        gateway = gateways.build(blob_source=blobs)
        retry = DatabaseRetry(gateway._geo.database_store)
        async with running(retry):
            await publish(feed, changes="1-0", geo_databases=[_listed("stored-a", data)])
            await gateway.start()
            await until(lambda: gateway.follower.applied_head is not None and blobs.attempts >= 2)
            assert gateway.readiness() == "IP databases not loaded yet: stored-a"

            # No feed moves: the retry alone brings the file once the api serves it.
            blobs.serving = True
            await until(lambda: gateway.readiness() is None)
            assert gateway.snapshot().geo_databases_loaded == ["stored-a"]
