# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Shared pieces of the controller tests: the feed builder as the runtime builds it, over the real Postgres and Redis.

Definitions are written through the write services, each announcing itself on the changes stream, and a test reads back
what Postgres and Redis hold once the running builder has caught up (``caught_up``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.controller import producers
from api.controller.feed_builder import FeedBuilder
from api.core.config import Settings
from api.core.leadership import Lease
from api.core.workers import LeaseName
from api.db.redis import CHANGES_STREAM_KEY, RUNTIME_STREAM_KEY, RedisClient, StreamId
from api.db.streams import ChangeLog
from api.feed.codec import FeedCodec
from api.feed.compiler import FullFeedCompiler
from api.feed.follower import FeedFollower, FeedUpdate
from api.feed.publisher import FeedPublisher
from api.geo.settings import GeoSettingsStore, defaults_from_config
from api.jobs.queue import JobQueue
from api.models.connector import Connector, ConnectorCreate
from api.models.credential import CredentialCreate
from api.models.project import Project, ProjectCreate
from api.models.proxy import Proxy, ProxyStatus
from api.providers.registry import ProviderRegistry, build_registry
from api.providers.store import ProviderStore
from api.routing.router import Router
from api.routing.snapshot import RoutingSnapshot
from api.services.connectors import ConnectorService
from api.services.credentials import CredentialService
from api.services.projects import ProjectService
from api.services.proxies import ProxyService
from tests.conftest import Clock
from tests.services.conftest import Provider
from tests.waits import running, until

# The secret the feed is sealed with, shared by the controller and the test gateways.
SECRET = "controller-test-secret"
# The controller's instance id: the value the lease holds while it leads.
OWNER = "controller-1"
# The least time between two compiles in most tests: short, so a test never
# waits on it, but not zero, so a compile right after another still waits.
COMPILE_INTERVAL = 0.01
# An epoch second the tests' clocks start at: the start of an hour, so a period's seconds line up with the clock's.
START = 1_760_000_400


class ObservedBuilder(FeedBuilder):
    """The feed builder, noting how far it had read each stream when its last act that returned began."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Stream name -> the position read when the last act that returned began; empty before the first.
        self.acted: dict[str, str] = {}

    async def _act(self) -> bool:
        read = {name: state.position or "0-0" for name, state in self.streams().items()}
        worked = await super()._act()
        self.acted = read
        return worked


async def caught_up(builder: FeedBuilder, redis_client: RedisClient, timeout: float = 5.0) -> None:
    """Wait until the running builder has acted on both streams as far as they go now, and has published its compile of them.

    An act that began past the heads read the runtime state the entries asked for and published what changed; the last
    compile covers the changes head, was made after the last full read and is published too.
    """
    assert isinstance(builder, ObservedBuilder)
    heads = {
        "changes": await redis_client.stream_head(CHANGES_STREAM_KEY) or "0-0",
        "runtime": await redis_client.stream_head(RUNTIME_STREAM_KEY) or "0-0",
    }

    def done() -> bool:
        acted = all(
            name in builder.acted and StreamId.of(builder.acted[name]) >= StreamId.of(head)
            for name, head in heads.items()
        )
        compiled = builder.compiled
        if not acted or compiled is None:
            return False
        full_reads = [at for state in builder.streams().values() if (at := state.last_full_read_at) is not None]
        return (
            StreamId.of(compiled.changes or "0-0") >= StreamId.of(heads["changes"])
            and all(compiled.compiled_at >= at for at in full_reads)
            and builder.publish_error is None
            and builder.published_at is not None
            and builder.published_at >= compiled.compiled_at
        )

    await until(done, timeout)


async def healthy_proxy(services: Services, connector: Connector, host: str) -> Proxy:
    """A proxy a job stored as working, as a sync's vendor session slot is."""
    proxy = Proxy(host=host, port=8080, connector_id=connector.id, status=ProxyStatus.HEALTHY)
    await services.proxies.add(proxy)
    return proxy


@pytest.fixture
def registry() -> ProviderRegistry:
    return build_registry(include_plugins=False)


@pytest.fixture
def sessions(
    db_session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> async_sessionmaker[AsyncSession]:
    """What every component opens its transactions with; ``db_session`` is here for its cleanup."""
    return db_session_factory


@pytest.fixture
def jobs(redis_client: RedisClient) -> JobQueue:
    return JobQueue(redis_client)


@pytest.fixture
async def lease(redis_client: RedisClient) -> AsyncIterator[Lease]:
    """The controller lease, held by this test's controller for the whole test."""
    held = Lease(redis_client, name=LeaseName.CONTROLLER, owner_id=OWNER)
    held.keep()
    await until(lambda: held.is_held)
    yield held
    await held.release()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """The producers' wall clock, from ``START``: what ``time.time()`` reads in their module."""
    clock = Clock(START)
    monkeypatch.setattr(producers, "time", clock)
    return clock


@dataclass
class Services:
    """The write services, as the api and the workers use them."""

    projects: ProjectService
    credentials: CredentialService
    connectors: ConnectorService
    proxies: ProxyService

    async def project(self, name: str = "Acme") -> Project:
        return await self.projects.create(ProjectCreate(name=name, username=name.lower(), password="pw"))

    async def connect(self, project: Project, name: str, provider: Provider, **fields: object) -> Connector:
        """A connector of ``provider`` in the project, on a credential of its own."""
        credential = await self.credentials.create(
            project.id, CredentialCreate(name=name, type=provider.type, config=provider.credential)
        )
        data = ConnectorCreate(name=name, credential_id=credential.id, config=provider.connector, **fields)
        return await self.connectors.create(project.id, data)


@pytest.fixture
def services(
    sessions: async_sessionmaker[AsyncSession],
    jobs: JobQueue,
    redis_client: RedisClient,
    registry: ProviderRegistry,
    change_log: ChangeLog,
) -> Services:
    return Services(
        projects=ProjectService(sessions, jobs, redis_client, changes=change_log),
        credentials=CredentialService(sessions, registry, changes=change_log),
        connectors=ConnectorService(sessions, jobs, redis_client, registry, changes=change_log),
        proxies=ProxyService(sessions, jobs, redis_client, registry, changes=change_log),
    )


# Builds a feed builder around a lease: make_builder(lease, compile_interval=..., reconcile_interval=...).
MakeBuilder = Callable[..., FeedBuilder]


@pytest.fixture
def make_builder(
    sessions: async_sessionmaker[AsyncSession],
    redis_client: RedisClient,
    registry: ProviderRegistry,
    jobs: JobQueue,
    test_settings: Settings,
) -> MakeBuilder:
    def make(
        lease: Lease, *, compile_interval: float = COMPILE_INTERVAL, reconcile_interval: float = 3600.0
    ) -> FeedBuilder:
        compiler = FullFeedCompiler(
            sessions,
            registry,
            descriptor_sink=ProviderStore(registry, sessions),
            geo_settings_sink=GeoSettingsStore(test_settings, sessions),
            geo_defaults=defaults_from_config(test_settings),
            default_strategy=test_settings.default_strategy,
        )
        publisher = FeedPublisher(redis_client, FeedCodec(SECRET), lease_key=lease.key, owner_id=OWNER)
        return ObservedBuilder(
            compiler,
            publisher,
            lease,
            jobs,
            redis_client,
            registry=registry,
            compile_interval=compile_interval,
            reconcile_interval=reconcile_interval,
        )

    return make


@pytest.fixture
async def builder(make_builder: MakeBuilder, lease: Lease) -> AsyncIterator[FeedBuilder]:
    """The leading controller's feed builder, running for the whole test."""
    built = make_builder(lease)
    async with running(built):
        yield built


class Gateway:
    """A gateway's feed follower and router, applying each update as the gateway composition does."""

    def __init__(self, redis_client: RedisClient) -> None:
        # A registry of its own: a gateway builds its providers from the feed.
        self._registry = build_registry(include_plugins=False)
        self.router = Router(RoutingSnapshot.from_sections({}, registry=self._registry))
        self.follower = FeedFollower(redis_client, FeedCodec(SECRET), self._apply, reconcile_interval=3600.0)
        self.updates: list[FeedUpdate] = []

    async def _apply(self, update: FeedUpdate) -> None:
        self.updates.append(update)
        previous = self.router.snapshot
        self.router.swap(RoutingSnapshot.from_sections(update.sections, registry=self._registry, previous=previous))


@pytest.fixture
def gateway(redis_client: RedisClient) -> Gateway:
    return Gateway(redis_client)
