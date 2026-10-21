# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A cluster of Octoprox processes on the test's event loop, and what an admin and a client do with it.

Each process is the app ``create_app`` builds for its role with its lifespan entered on the test's loop, sharing only
Postgres and Redis with the others; the test reads both with its own connections and waits by polling for an outcome.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import httpx
import pytest

from api.core.leadership import STANDBY_POLL_MS
from api.core.roles import JobKind, Role
from api.core.system_stats import ClusterReader
from api.db.redis import CHANGES_STREAM_KEY, PROJECT_WINDOWS, StreamId
from api.db.repository import MetricsRepository, ProxyRepository
from api.db.streams import KIND_FIELD, REF_FIELD, FullRead
from api.feed.state import RuntimeStateReader
from api.models.proxy import ProxyStatus
from tests.e2e.fakes import ECHO_PATH, LOOPBACK, FakeTarget, FakeUpstream

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from api.core.stats import MetricDelta
    from api.db.redis import RedisClient
    from api.db.streams import ChangeKind
    from api.feed.state import HealthSoft
    from api.models.system import StreamState, WorkStream
    from api.roles.api import ApiRuntime
    from api.roles.controller import ControllerRuntime
    from api.roles.gateway import GatewayRuntime
    from api.roles.node import Node
    from api.roles.worker import WorkerRuntime

# Every process's loops, faster than in production so a test sees them act
# within seconds: the controller compiles at most every 100 ms and its
# producers publish each proxy's health check and the metrics flush every
# second (both settings are whole seconds), and a check gives up after 3 s,
# so a dead vendor fails its checks quickly. The gateways' metering keeps its
# fixed 5 s cadence, and the producers without a setting keep their periods
# unless a test patches the controller's constants.
FAST_LOOPS: dict[str, Any] = {
    "feed_compile_interval": 0.1,
    "health_check_interval": 1,
    "health_check_timeout": 3,
    "metrics_flush_interval": 1,
}
# The secret every process seals and opens the routing feed with: one cluster, one secret.
INTERNAL_SECRET = "e2e-internal-secret"
# Where a gateway would fetch IP database files from. No test stores one, so
# nothing is fetched; set so a gateway does not warn about the default.
INTERNAL_API_URL = "http://api.e2e.test:8000"
# One name for every process's Postgres sessions, so the whole cluster and
# the routes' ``get_db`` (which reads the module-global settings, those of
# the last app built) share one cached engine, which every process disposes
# as it stops: a name per process would leave ``get_db`` an engine of its
# own, never disposed, whenever a non-api app was built last.
DB_APPLICATION_NAME = "octoprox-e2e"
# INFO rather than the test settings' DEBUG: at DEBUG the engine echoes every
# statement of every loop of every process, which only slows a test down.
LOG_LEVEL = "INFO"

# How long a process may take to report ready once started: a gateway waits
# for the controller's first publish and its own first read of the feed.
READY_WITHIN_SECONDS = 15.0
# How long a new proxy may take to be routed by a gateway: the api's run-now
# health entry wakes a worker at once, its check moves the status, the
# controller compiles that change at once, and the gateway reads the feed as
# its entry arrives. Well under a second; the rest is room for a busy machine.
ROUTED_WITHIN_SECONDS = 15.0
# How long a request may take to reach Postgres totals: the gateway's
# metering writes its window to Redis every 5 s, and a worker runs the
# metrics flush the controller publishes every second.
FLUSHED_WITHIN_SECONDS = 20.0
# Room for a busy machine on top of a bound that a period, a poll or the claim
# timings set.
SLACK_SECONDS = 5.0
# How long a standby may take to lead once the leader stopped: a leader that
# stops releases the lease, so the standby takes it at its next poll, far
# inside the lease's TTL (``LEASE_TTL_MS``), which bounds a failover only after
# a leader that died without releasing it.
FAILOVER_WITHIN_SECONDS = STANDBY_POLL_MS / 1000 + SLACK_SECONDS
# How long the workers may take to have their groups back after a wipe: a
# read that finds its group gone creates it again at once, from the stream's
# first entry.
REGROUPED_WITHIN_SECONDS = 3.0
# How long one client request through a gateway may take, connect to last byte.
CLIENT_TIMEOUT_SECONDS = 10.0
# A follower's full reads when it read in full only as it started: whatever reached it since came by an entry.
STARTED_ONLY = {FullRead.START.value: 1}
# How often a wait checks its condition.
POLL_SECONDS = 0.05


@dataclass(frozen=True)
class ClaimTimings:
    """The group consumers' claim timings, in seconds, which a test shortens with the ``short_claims`` fixture."""

    # How long an entry idles in a worker's hands before another worker takes it.
    timeout: float
    # How often a worker with room looks for entries idle that long.
    check: float
    # How often a worker resets the idle time of the entries it holds.
    refresh: float


# Short enough that a dead worker's entries are taken within seconds; the
# refresh stays well inside the timeout, so a live worker keeps what it runs.
SHORT_CLAIMS = ClaimTimings(timeout=1.5, check=0.25, refresh=0.25)


def cluster_settings(target: FakeTarget) -> dict[str, Any]:
    """The settings overrides every process of a test's cluster starts with.

    The echo endpoint (``geo_lookup_url``) is the fake site's ``/ip``, so the attribution health checks feed stays on
    the test's machine.
    """
    return {
        **FAST_LOOPS,
        "host": LOOPBACK,
        "proxy_port": 0,
        "internal_secret": INTERNAL_SECRET,
        "internal_api_url": INTERNAL_API_URL,
        "geo_lookup_url": target.url(ECHO_PATH),
        "db_application_name": DB_APPLICATION_NAME,
        "log_level": LOG_LEVEL,
    }


# --- Waiting -------------------------------------------------------------------------


async def until(condition: Callable[[], bool], *, within: float, what: str) -> float:
    """Seconds until ``condition()`` held, checked every ``POLL_SECONDS``; fails the test after ``within``.

    ``what`` names the outcome waited for in the failure.
    """
    started = time.monotonic()
    try:
        async with asyncio.timeout(within):
            while not condition():
                await asyncio.sleep(POLL_SECONDS)
    except TimeoutError:
        pytest.fail(f"{what}: not within {within:g} s")
    return time.monotonic() - started


async def eventually[T](probe: Callable[[], Awaitable[T | None]], *, within: float, what: str) -> T:
    """The first truthy value ``probe()`` returns, asked every ``POLL_SECONDS``; fails the test after ``within``.

    ``what`` names the outcome waited for in the failure.
    """
    try:
        async with asyncio.timeout(within):
            while True:
                value = await probe()
                if value:
                    return value
                await asyncio.sleep(POLL_SECONDS)
    except TimeoutError:
        pytest.fail(f"{what}: not within {within:g} s")


async def throughout(probe: Callable[[], Awaitable[bool]], *, seconds: float, what: str) -> None:
    """Check every ``POLL_SECONDS`` for ``seconds`` that ``probe()`` holds; fails the test the first time it does not.

    ``what`` names what must hold in the failure.
    """
    started = time.monotonic()
    while (elapsed := time.monotonic() - started) < seconds:
        if not await probe():
            pytest.fail(f"{what}: broken after {elapsed:.2f} s")
        await asyncio.sleep(POLL_SECONDS)


# --- Processes -----------------------------------------------------------------------


class TaskPerRequestTransport(httpx.AsyncBaseTransport):
    """An app's ASGI transport that serves each request in a task of its own, as a server does.

    ``httpx.ASGITransport`` runs the app in the caller's task, where what a request binds for its log lines would
    outlive it; in a task of its own, as uvicorn serves it, that context ends with the request.
    """

    def __init__(self, app: FastAPI) -> None:
        self._transport = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """The app's response to ``request``, served in a task of its own."""
        serving = asyncio.create_task(self._transport.handle_async_request(request))
        try:
            return await serving
        except asyncio.CancelledError:
            # The caller gave up (a wait's deadline): so does the request.
            serving.cancel()
            raise

    async def aclose(self) -> None:
        """Close the underlying ASGI transport."""
        await self._transport.aclose()


class Process:
    """One Octoprox process of a cluster: its role's app, the node the app's lifespan runs, and a client on the app.

    Args:
        name: The process's name; its instance id is ``e2e-<name>``.
        app: The app ``create_app`` built for the process's role and settings.
        headers: Sent with every request of :attr:`http`: the admin's token.
    """

    def __init__(self, name: str, app: FastAPI, *, headers: Mapping[str, str]) -> None:
        self.name = name
        self.app = app
        # Requests run the app's whole ASGI stack (middleware, sign-in, routes) on the test's loop.
        self.http = httpx.AsyncClient(
            transport=TaskPerRequestTransport(app),
            base_url=f"http://{name}.e2e.test",
            headers=dict(headers),
        )
        self._lifespan: AsyncExitStack | None = None

    @property
    def running(self) -> bool:
        """Whether the process has started and not stopped."""
        return self._lifespan is not None

    @property
    def node(self) -> Node:
        """The node the app's lifespan built: the process's infrastructure and runtimes."""
        node = getattr(self.app.state, "node", None)
        assert node is not None, f"{self.name} has not started"
        return cast("Node", node)

    @property
    def api(self) -> ApiRuntime:
        """The process's api runtime: the services and the job queue the routes use."""
        runtime = self.node.api
        assert runtime is not None, f"{self.name} runs no api"
        return runtime

    @property
    def gateway(self) -> GatewayRuntime:
        """The process's gateway runtime: its router, proxy listener and metering."""
        runtime = self.node.gateway
        assert runtime is not None, f"{self.name} runs no gateway"
        return runtime

    @property
    def controller(self) -> ControllerRuntime:
        """The process's controller runtime: its lease, feed builder and job producers."""
        runtime = self.node.controller
        assert runtime is not None, f"{self.name} runs no controller"
        return runtime

    @property
    def worker(self) -> WorkerRuntime:
        """The process's worker runtime: a job runner per kind it takes, and the ingests."""
        runtime = self.node.worker
        assert runtime is not None, f"{self.name} runs no worker"
        return runtime

    def streams(self) -> dict[str, StreamState]:
        """Every stream the process follows from a position of its own, by name, as its heartbeat reports them."""
        return self.node.snapshot().streams

    def has_read(self, stream: str, entry: StreamId) -> bool:
        """Whether the process has read ``stream`` up to ``entry``, as its heartbeat reports it."""
        state = self.streams().get(stream)
        return state is not None and state.position is not None and StreamId.of(state.position) >= entry

    def has_compiled(self, entry: StreamId) -> bool:
        """Whether the controller's last compile read the changes stream up to ``entry``."""
        compiled = self.controller.builder.compiled
        return compiled is not None and compiled.changes is not None and StreamId.of(compiled.changes) >= entry

    def is_traffic_blocked(self, connector_id: str) -> bool:
        """Whether the feed the gateway applied holds the connector back for its traffic limit."""
        return self.gateway.router.is_traffic_blocked(connector_id)

    async def start(self) -> None:
        """Enter the app's lifespan: the node migrates, connects, and starts its role's runtimes."""
        lifespan = AsyncExitStack()
        await lifespan.enter_async_context(self.app.router.lifespan_context(self.app))
        self._lifespan = lifespan

    async def stop(self) -> None:
        """Leave the lifespan: the node stops its runtimes, flushing what they buffer. Idempotent."""
        lifespan, self._lifespan = self._lifespan, None
        if lifespan is not None:
            await lifespan.aclose()

    def cancel_loops(self) -> None:
        """Cancel the task of every loop the process runs without stopping any, as a process that dies leaves them.

        Nothing cycles after, so nothing it holds is refreshed; a batch a loop started runs on until it ends.
        """
        for task in self.node.background_tasks:
            task.cancel()

    async def is_ready(self) -> bool:
        """Whether the process answers its readiness probe with 200, as a load balancer asks it."""
        return (await self.http.get("/ready")).status_code == 200


class Cluster:
    """The processes of one test: built from the cluster's settings, and stopped, last started first, when it ends.

    Args:
        build_app: The ``build_app`` fixture: an app of a role from the test settings with overrides.
        settings: The overrides every process gets (:func:`cluster_settings`).
        headers: The admin's token, sent with every request to a process.
    """

    def __init__(
        self,
        build_app: Callable[..., FastAPI],
        *,
        settings: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> None:
        self._build_app = build_app
        self._settings = dict(settings)
        self._headers = dict(headers)
        self._processes: list[Process] = []
        # What :meth:`close` undoes, last first: every stop runs even when one before it raised.
        self._exits = AsyncExitStack()

    def process(self, role: Role, name: str, **overrides: Any) -> Process:
        """A process of ``role`` named ``name``, built and not started.

        Build every process of a test before starting any: each build clears the engine cache, so a process built after
        others started would leave the routes' ``get_db`` an engine no process disposes (``DB_APPLICATION_NAME``).
        """
        app = self._build_app(role, **{**self._settings, "instance_id": f"e2e-{name}", **overrides})
        process = Process(name, app, headers=self._headers)
        self._processes.append(process)
        self._exits.push_async_callback(process.http.aclose)
        return process

    async def start(self, *processes: Process) -> None:
        """Start ``processes`` one after another, in the order given."""
        for process in processes:
            await process.start()
            self._exits.push_async_callback(process.stop)

    async def ready(self) -> None:
        """Wait until every running process reports ready."""
        for process in self._processes:
            if process.running:
                await eventually(
                    process.is_ready, within=READY_WITHIN_SECONDS, what=f"{process.name} ready"
                )

    async def stop(self, process: Process) -> None:
        """Stop one process, as its container stopping would."""
        await process.stop()

    async def close(self) -> None:
        """Stop every running process, last started first, then close the admin clients."""
        await self._exits.aclose()


@dataclass(frozen=True)
class Roles:
    """A started layout: its processes, and which of them the admin writes to, clients connect to, leads and works."""

    # Where the admin writes.
    api: Process
    # Where clients connect.
    gateway: Process
    # The layout's one controller, so its leader.
    controller: Process
    # Where the jobs run.
    worker: Process
    # Every process of the layout, in start order.
    processes: tuple[Process, ...]


async def start_all_in_one(cluster: Cluster, **overrides: Any) -> Roles:
    """One process of role ``all``: every runtime in one process, as a single install runs."""
    process = cluster.process(Role.ALL, "all", **overrides)
    await cluster.start(process)
    await cluster.ready()
    return Roles(api=process, gateway=process, controller=process, worker=process, processes=(process,))


async def start_split(cluster: Cluster, **overrides: Any) -> Roles:
    """One process per role, api, controller, worker and gateway, as a cluster deploys them."""
    processes = tuple(
        cluster.process(role, role.value, **overrides)
        for role in (Role.API, Role.CONTROLLER, Role.WORKER, Role.GATEWAY)
    )
    await cluster.start(*processes)
    await cluster.ready()
    api, controller, worker, gateway = processes
    return Roles(api=api, gateway=gateway, controller=controller, worker=worker, processes=processes)


# --- What the admin does -------------------------------------------------------------


@dataclass
class Pool:
    """A project with one static connector and its first proxy, the fake vendor, as the admin created them."""

    project_id: str
    # The project's proxy credentials: what clients send the gateway.
    username: str
    password: str
    connector_id: str
    # The proxy :func:`create_pool` added first.
    proxy_id: str = field(init=False)

    async def add_proxy(self, api: Process, upstream: FakeUpstream) -> str:
        """Add a proxy at ``upstream`` to the connector through the api, with the credentials the vendor demands; its id."""
        proxy = await created(
            api,
            f"/api/v1/projects/{self.project_id}/proxies",
            {
                "connector_id": self.connector_id,
                "host": LOOPBACK,
                "port": upstream.port,
                "protocol": "http",
                "username": upstream.username,
                "password": upstream.password,
            },
        )
        return str(proxy["id"])


async def created(api: Process, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """POST ``body`` to the api's ``path`` and return what was created; fails the test unless it answers 201."""
    response = await api.http.post(path, json=dict(body))
    assert response.status_code == 201, f"POST {path}: {response.status_code} {response.text}"
    answer: dict[str, Any] = response.json()
    return answer


async def create_pool(api: Process, upstream: FakeUpstream, **connector: Any) -> Pool:
    """Create, through the api's routes, a project, a static credential and connector, and a proxy at ``upstream``.

    ``connector`` adds fields to the connector's body, such as its ``traffic_config`` or ``rate_limit_config``.
    """
    suffix = uuid4().hex[:8]
    username, password = f"e2e_{suffix}", f"client-{suffix}"
    project = await created(
        api,
        "/api/v1/projects",
        {
            "name": f"E2E {suffix}",
            "username": username,
            "password": password,
            "routing_strategy": "round_robin",
        },
    )
    base = f"/api/v1/projects/{project['id']}"
    credential = await created(
        api,
        f"{base}/credentials",
        {"name": "Static", "type": "static_proxy_provider", "config": {}},
    )
    created_connector = await created(
        api, f"{base}/connectors", {"name": "Static", "credential_id": credential["id"], **connector}
    )
    pool = Pool(project["id"], username, password, created_connector["id"])
    pool.proxy_id = await pool.add_proxy(api, upstream)
    return pool


# --- What a client does --------------------------------------------------------------


def proxy_status(gateway: Process, proxy_id: str) -> ProxyStatus | None:
    """The proxy's status in the feed the gateway applied; None while the gateway has no such proxy."""
    proxy = gateway.gateway.router.get_proxy(proxy_id)
    return proxy.status if proxy is not None else None


async def until_routed(gateway: Process, proxy_id: str) -> None:
    """Wait until the gateway has applied a feed in which the proxy is healthy, so it routes to it."""
    await until(
        lambda: proxy_status(gateway, proxy_id) == ProxyStatus.HEALTHY,
        within=ROUTED_WITHIN_SECONDS,
        what="the gateway routing to the new proxy",
    )


async def fetch(
    gateway: Process, pool: Pool, url: str, *, password: str | None = None
) -> httpx.Response:
    """GET ``url`` through the gateway's proxy port as the pool's project: plain HTTP, an absolute-form request.

    ``password`` replaces the project's own.
    """
    port = gateway.gateway.proxy_server.port
    proxy = f"http://{pool.username}:{password or pool.password}@{LOOPBACK}:{port}"
    async with httpx.AsyncClient(
        proxy=proxy, trust_env=False, timeout=CLIENT_TIMEOUT_SECONDS
    ) as client:
        return await client.get(url)


async def fetch_in_tunnel(
    gateway: Process, pool: Pool, target: FakeTarget, path: str
) -> tuple[bytes, bytes]:
    """CONNECT through the gateway to the site and GET ``path`` inside the tunnel, half-closing once it is sent.

    Returns the gateway's status line for the CONNECT and the body of the last response read: the site's, when the
    tunnel was opened.
    """
    token = base64.b64encode(f"{pool.username}:{pool.password}".encode()).decode()
    reader, writer = await asyncio.open_connection(LOOPBACK, gateway.gateway.proxy_server.port)
    try:
        writer.write(
            f"CONNECT {target.authority} HTTP/1.1\r\nHost: {target.authority}\r\n"
            f"Proxy-Authorization: Basic {token}\r\n\r\n"
            f"GET {path} HTTP/1.1\r\nHost: {target.authority}\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        writer.write_eof()
        async with asyncio.timeout(CLIENT_TIMEOUT_SECONDS):
            received = await reader.read()
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()
    status_line, _, rest = received.partition(b"\r\n")
    return status_line, rest.rpartition(b"\r\n\r\n")[2]


# --- What Postgres holds (read with the test's own connections) ------------------------


async def stored_status(
    sessions: async_sessionmaker[AsyncSession], proxy_id: str
) -> ProxyStatus | None:
    """The proxy's status column; None when the row is gone."""
    async with sessions() as session:
        proxy = await ProxyRepository(session).get_by_id(proxy_id)
    return proxy.status if proxy is not None else None


async def project_totals(
    sessions: async_sessionmaker[AsyncSession], project_id: str
) -> MetricDelta | None:
    """The project's all-time totals the workers' metrics flush keeps; None before its first flush."""
    async with sessions() as session:
        totals = await MetricsRepository(session).get_totals(PROJECT_WINDOWS, [project_id])
    return totals.get(project_id)


# --- What Redis holds ------------------------------------------------------------------

# The work streams every worker reads whatever job kinds it takes, by the System view's names.
INGEST_STREAMS = ("sightings", "observations")


async def change_entries(
    redis_client: RedisClient, kind: ChangeKind, ref: str, *, after: StreamId | None = None
) -> list[StreamId]:
    """The changes stream's entries of saves of ``kind`` about ``ref``, oldest first; only those after ``after`` if given."""
    entries = await redis_client.client.xrange(CHANGES_STREAM_KEY, min=f"({after}" if after else "-", max="+")
    return [
        StreamId.of(str(entry_id))
        for entry_id, fields in entries or []
        if fields and fields.get(KIND_FIELD) == kind and fields.get(REF_FIELD) == ref
    ]


async def change_entry(redis_client: RedisClient, kind: ChangeKind, ref: str) -> StreamId:
    """The newest changes stream entry of a save of ``kind`` about ``ref``; fails the test when there is none."""
    entries = await change_entries(redis_client, kind, ref)
    if not entries:
        pytest.fail(f"no {kind} change entry about {ref}")
    return entries[-1]


async def soft_health(redis_client: RedisClient, proxy_id: str) -> HealthSoft | None:
    """The proxy's soft health as its last check left it; None before its first check."""
    return (await RuntimeStateReader(redis_client).health([proxy_id])).get(proxy_id)


async def checked_after(redis_client: RedisClient, proxy_id: str, at: datetime) -> bool:
    """Whether the proxy's last check ran after ``at`` (naive UTC)."""
    soft = await soft_health(redis_client, proxy_id)
    return soft is not None and soft.checked_at > at


def _job_stream_name(kind: JobKind) -> str:
    return f"jobs.{kind}"


async def job_stream(redis_client: RedisClient, kind: JobKind) -> WorkStream:
    """``kind``'s job stream as the System view reads it: its length, what is pending, and the workers in its group."""
    return (await ClusterReader(redis_client).streams()).by_name[_job_stream_name(kind)]


async def drained(redis_client: RedisClient, *kinds: JobKind) -> bool:
    """Whether the job stream of each of ``kinds`` is empty: nothing queued and nothing running."""
    streams = (await ClusterReader(redis_client).streams()).by_name
    return all(streams[_job_stream_name(kind)].length == 0 for kind in kinds)


async def groups_back(redis_client: RedisClient, kinds: Iterable[JobKind]) -> bool:
    """Whether the job stream of each of ``kinds``, and each ingest stream, has the workers' group with a worker in it."""
    streams = (await ClusterReader(redis_client).streams()).by_name
    names = [*map(_job_stream_name, kinds), *INGEST_STREAMS]
    return all(name in streams and streams[name].consumers > 0 for name in names)
