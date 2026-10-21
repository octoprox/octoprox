# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Shared pieces of the worker job tests: the real Postgres and Redis test containers, services and attribution stack.

Upstream proxies are :class:`FakeUpstream`, which answers every request itself, and what a job did is read back from
Postgres, Redis, the job streams and the changes stream; the one IP database file places ``81.2.69.0/24`` in London.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio.client import Pipeline, Redis
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.roles import JobKind
from api.db.redis import CHANGES_STREAM_KEY, OBSERVATION_STREAM_KEY, RedisClient
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.db.streams import ChangeLog, StreamEntry
from api.geo.attributor import ProxyAttributor
from api.geo.models import ExitJudgement, IpObservation
from api.geo.observations import ObservationIngest
from api.geo.runtime import GeoStack
from api.jobs.attribution import Attribution
from api.jobs.job import JobBatch
from api.jobs.queue import JobQueue
from api.models.connector import Connector
from api.models.credential import Credential
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol
from api.providers.registry import ProviderRegistry, build_registry
from api.services.connectors import ConnectorService
from api.services.proxies import ProxyService
from tests.conftest import queued_job_keys
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb

# An exit the test database places in London, and one it does not know.
LONDON_IP = "81.2.69.160"
UNKNOWN_IP = "198.51.100.7"
# The echo endpoint checks and lookups request through the proxies. Plain
# HTTP, so the fake upstream answers it as a forward proxy would.
ECHO_URL = "http://echo.test/ip"


class FakeUpstream:
    """A local HTTP proxy that answers every request itself, and records what it was asked.

    ``status`` and ``body`` are the answer (the body as JSON); ``delay``
    holds the answer back, for timeouts.
    """

    def __init__(self) -> None:
        self.status = 200
        self.body: Any = {}
        self.delay = 0.0
        # Each request's line and Proxy-Authorization header, in arrival order.
        self.requests: list[tuple[str, str | None]] = []
        self._server: asyncio.Server | None = None

    def answer(self, body: Any, status: int = 200) -> None:
        self.body = body
        self.status = status

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def port(self) -> int:
        assert self._server is not None
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head: list[str] = []
        while (line := (await reader.readline()).decode().rstrip("\r\n")) != "":
            head.append(line)
        if not head:
            writer.close()
            return
        headers = dict(line.split(":", 1) for line in head[1:] if ":" in line)
        auth = next((value.strip() for name, value in headers.items() if name.lower() == "proxy-authorization"), None)
        self.requests.append((head[0], auth))
        if self.delay:
            await asyncio.sleep(self.delay)
        payload = json.dumps(self.body).encode()
        writer.write(
            f"HTTP/1.1 {self.status} X\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n".encode()
            + payload
        )
        # A client that gave up waiting (a timeout test) has hung up by now.
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()


@pytest.fixture
async def upstream() -> AsyncIterator[FakeUpstream]:
    server = FakeUpstream()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def registry() -> ProviderRegistry:
    return build_registry(include_plugins=False)


@pytest.fixture
def sessions(
    db_session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> async_sessionmaker[AsyncSession]:
    """What jobs and services open their transactions with; ``db_session`` is here for its cleanup."""
    return db_session_factory


@pytest.fixture
def jobs(redis_client: RedisClient) -> JobQueue:
    return JobQueue(redis_client)


@pytest.fixture
def proxy_service(
    sessions: async_sessionmaker[AsyncSession],
    jobs: JobQueue,
    redis_client: RedisClient,
    registry: ProviderRegistry,
    change_log: ChangeLog,
) -> ProxyService:
    return ProxyService(sessions, jobs, redis_client, registry, changes=change_log)


@pytest.fixture
def connector_service(
    sessions: async_sessionmaker[AsyncSession],
    jobs: JobQueue,
    redis_client: RedisClient,
    registry: ProviderRegistry,
    change_log: ChangeLog,
) -> ConnectorService:
    return ConnectorService(sessions, jobs, redis_client, registry, changes=change_log)


def geo_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Settings whose attribution stack reads the London database and echoes through ``ECHO_URL``."""
    database = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
    values: dict[str, Any] = {
        "instance_id": "worker-1",
        "geo_cache_dir": str(tmp_path / "cache"),
        "geo_databases": [{"path": str(database)}],
        "geo_lookup_url": ECHO_URL,
        "geo_lookup_ip_path": "ip",
        "geo_lookup_country_path": "country",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
async def geo(
    tmp_path: Path, sessions: async_sessionmaker[AsyncSession], redis_client: RedisClient
) -> AsyncIterator[GeoStack]:
    """The worker's attribution stack, read from Postgres as a worker starts it."""
    stack = GeoStack.build(geo_settings(tmp_path), session_factory=sessions, redis_client=redis_client, blob_source=None)
    await stack.geo_service.resync()
    yield stack
    stack.database_store.close_all()


@pytest.fixture
def attribution(
    geo: GeoStack,
    sessions: async_sessionmaker[AsyncSession],
    proxy_service: ProxyService,
    redis_client: RedisClient,
    change_log: ChangeLog,
) -> Attribution:
    return Attribution(ProxyAttributor(geo.geo_service), sessions, proxy_service, redis_client, changes=change_log)


class Rows:
    """Writes the rows a test needs through the repositories, committed, and reads them back."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._made = 0

    async def project(self, **fields: Any) -> Project:
        self._made += 1
        project = Project(name=f"p{self._made}", username=f"u{self._made}", password="pw", **fields)
        async with self._sessions() as session:
            await ProjectRepository(session).create(project)
            await session.commit()
        return project

    async def connector(
        self,
        project: Project,
        type: str = "static_proxy_provider",
        *,
        credential: dict[str, Any] | None = None,
        **fields: Any,
    ) -> Connector:
        """A connector of ``project`` on a credential of its own, of ``type``."""
        self._made += 1
        made = Credential(name=f"c{self._made}", type=type, project_id=project.id, config=credential or {})
        connector = Connector(
            name=f"c{self._made}", credential_id=made.id, credential_type=type, project_id=project.id, **fields
        )
        async with self._sessions() as session:
            await CredentialRepository(session).create(made)
            await ConnectorRepository(session).create(connector)
            await session.commit()
        return connector

    async def proxy(self, connector: Connector, **fields: Any) -> Proxy:
        fields.setdefault("host", "127.0.0.1")
        fields.setdefault("port", 9)
        fields.setdefault("protocol", ProxyProtocol.HTTP)
        proxy = Proxy(connector_id=connector.id, **fields)
        async with self._sessions() as session:
            await ProxyRepository(session).create(proxy)
            await session.commit()
        return proxy

    async def get(self, proxy_id: str) -> Proxy | None:
        async with self._sessions() as session:
            return await ProxyRepository(session).get_by_id(proxy_id)

    async def of(self, connector: Connector) -> list[Proxy]:
        """The connector's proxies as stored, in creation order."""
        async with self._sessions() as session:
            return await ProxyRepository(session).get_by_connector(connector.id)

    async def connector_as_stored(self, connector_id: str) -> Connector | None:
        async with self._sessions() as session:
            return await ConnectorRepository(session).get_by_id(connector_id)


@pytest.fixture
def rows(sessions: async_sessionmaker[AsyncSession]) -> Rows:
    return Rows(sessions)


def batch_of(*keys: str) -> JobBatch:
    """A batch of one entry per key, as a runner hands a job the entries it read."""
    return JobBatch.of([StreamEntry(f"{number}-0", {"key": key}) for number, key in enumerate(keys, 1)])


async def waiting(redis_client: RedisClient, kind: str) -> set[str]:
    """The keys of ``kind`` with a run asked for and not taken yet: those on its job stream."""
    return set(await queued_job_keys(redis_client, JobKind(kind)))


async def announced(redis_client: RedisClient) -> list[tuple[str, str]]:
    """Every entry the writes appended to the changes stream so far, as (kind, ref), oldest first."""
    entries = await redis_client.client.xrange(CHANGES_STREAM_KEY)
    return [(fields["kind"], fields["ref"]) for _, fields in entries]


async def recorded(geo: GeoStack, redis_client: RedisClient) -> list[IpObservation | ExitJudgement]:
    """What the process recorded since the last call: its recorder's buffer, flushed to the observations stream and read back."""
    await geo.observation_recorder.flush()
    entries = await redis_client.client.xrange(OBSERVATION_STREAM_KEY)
    await redis_client.client.delete(OBSERVATION_STREAM_KEY)
    return [ObservationIngest.parse(fields["item"]) for _entry_id, fields in entries]


@contextmanager
def counted_selects(engine: AsyncEngine) -> Iterator[list[str]]:
    """The SELECT statements the engine runs while the block runs."""
    statements: list[str] = []

    def before(conn: object, cursor: object, statement: str, *args: object) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)


@contextmanager
def counted_round_trips() -> Iterator[list[str]]:
    """The Redis round trips every client makes while the block runs: each command's name, or "pipeline".

    Counted where the client sends them, so the handshake of a connection the
    pool opens meanwhile is not one.
    """
    sent: list[str] = []
    command, pipeline = Redis.execute_command, Pipeline.execute

    async def counting_command(self: Redis, *args: Any, **options: Any) -> Any:
        sent.append(str(args[0]))
        return await command(self, *args, **options)

    async def counting_pipeline(self: Pipeline, raise_on_error: bool = True) -> list[Any]:
        sent.append("pipeline")
        return await pipeline(self, raise_on_error)

    Redis.execute_command = counting_command  # type: ignore[method-assign]
    Pipeline.execute = counting_pipeline  # type: ignore[method-assign]
    try:
        yield sent
    finally:
        Redis.execute_command = command  # type: ignore[method-assign]
        Pipeline.execute = pipeline  # type: ignore[method-assign]
