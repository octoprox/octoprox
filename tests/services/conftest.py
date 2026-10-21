# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Shared pieces of the write service and read model tests, on the real test containers and the built-in providers.

The services commit, announce each commit on the changes stream (``change_log``) and ask for their runs on the job
streams (``jobs``), and a test reads back what Postgres and the streams hold.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.db.redis import CHANGES_STREAM_KEY, RUNTIME_STREAM_KEY, RedisClient
from api.db.streams import ChangeKind, ChangeLog, ref_of
from api.jobs.queue import JobQueue
from api.models.connector import Connector, ConnectorCreate
from api.models.credential import Credential, CredentialCreate
from api.models.project import Project, ProjectCreate
from api.providers.registry import ProviderRegistry, build_registry
from api.services.connectors import ConnectorService
from api.services.credentials import CredentialService
from api.services.projects import ProjectService
from api.services.proxies import ProxyService


@dataclass(frozen=True)
class Provider:
    """A credential type with a credential config and a connector config its provider accepts."""

    type: str
    credential: dict[str, Any]
    connector: dict[str, Any]


_OXYLABS_CREDENTIAL = {"proxy_type": "residential", "username": "alice", "password": "s3cret"}
AWS_CREDENTIAL = {"access_key": "AKIAIOSFODNN7EXAMPLE", "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}

# Proxies added by hand: nothing to sync or scale.
STATIC = Provider("static_proxy_provider", {}, {})
# A vendor session pool of two slots per country, listing no country ("All countries"): a provider that syncs.
OXYLABS_POOL = Provider("oxylabs", _OXYLABS_CREDENTIAL, {"num_proxies": 2, "session_duration_minutes": 10})
# One gateway row rendering a vendor session per request.
OXYLABS_DYNAMIC = Provider("oxylabs", _OXYLABS_CREDENTIAL, {"session_mode": "dynamic", "session_duration_minutes": 10})
# Cloud instances the scale job creates and terminates.
AWS = Provider(
    "aws",
    AWS_CREDENTIAL,
    {
        "instance_name": "octo",
        "region": "us-east-1",
        "instance_type": "t3.micro",
        "key_pair_name": "octo",
        "security_group": "octo",
        "min_proxies": 1,
        "max_proxies": 4,
    },
)

# Adds a connector named ``name`` to a project, on a credential of its own.
Connect = Callable[..., Awaitable[Connector]]


@pytest.fixture
def registry() -> ProviderRegistry:
    return build_registry(include_plugins=False)


@pytest.fixture
def sessions(
    db_session_factory: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> async_sessionmaker[AsyncSession]:
    """What the services open their transactions with; ``db_session`` is here for its cleanup."""
    return db_session_factory


@pytest.fixture
def jobs(redis_client: RedisClient) -> JobQueue:
    return JobQueue(redis_client)


@pytest.fixture
def project_service(
    sessions: async_sessionmaker[AsyncSession],
    jobs: JobQueue,
    redis_client: RedisClient,
    change_log: ChangeLog,
) -> ProjectService:
    return ProjectService(sessions, jobs, redis_client, changes=change_log)


@pytest.fixture
def credential_service(
    sessions: async_sessionmaker[AsyncSession], registry: ProviderRegistry, change_log: ChangeLog
) -> CredentialService:
    return CredentialService(sessions, registry, changes=change_log)


@pytest.fixture
def connector_service(
    sessions: async_sessionmaker[AsyncSession],
    jobs: JobQueue,
    redis_client: RedisClient,
    registry: ProviderRegistry,
    change_log: ChangeLog,
) -> ConnectorService:
    return ConnectorService(sessions, jobs, redis_client, registry, changes=change_log)


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
def connect(credential_service: CredentialService, connector_service: ConnectorService) -> Connect:
    async def connect(project: Project, name: str, provider: Provider, **fields: Any) -> Connector:
        credential = await credential_service.create(
            project.id, CredentialCreate(name=name, type=provider.type, config=provider.credential)
        )
        return await connector_service.create(
            project.id, ConnectorCreate(name=name, credential_id=credential.id, config=provider.connector, **fields)
        )

    return connect


@pytest.fixture
async def project(project_service: ProjectService) -> Project:
    return await project_service.create(ProjectCreate(name="Acme", username="acme", password="pw"))


@pytest.fixture
async def other_project(project_service: ProjectService) -> Project:
    return await project_service.create(ProjectCreate(name="Other", username="other", password="pw"))


@pytest.fixture
async def static_credential(credential_service: CredentialService, project: Project) -> Credential:
    return await credential_service.create(project.id, CredentialCreate(name="Static", type=STATIC.type))


@pytest.fixture
async def static_connector(
    connector_service: ConnectorService, project: Project, static_credential: Credential
) -> Connector:
    return await connector_service.create(project.id, ConnectorCreate(name="ISP", credential_id=static_credential.id))


@pytest.fixture
async def pool(connect: Connect, project: Project) -> Connector:
    return await connect(project, "Pool", OXYLABS_POOL)


@pytest.fixture
async def cloud(connect: Connect, project: Project) -> Connector:
    return await connect(project, "AWS", AWS)


async def announced(redis_client: RedisClient, stream: str = CHANGES_STREAM_KEY) -> list[tuple[str, str]]:
    """Every entry of ``stream`` (the changes stream by default) as (kind, ref), oldest first."""
    return [(fields["kind"], fields["ref"]) for _, fields in await redis_client.client.xrange(stream)]


async def runtime_entries(redis_client: RedisClient) -> list[tuple[str, str]]:
    """Every entry of the runtime stream as (kind, ref), oldest first: the quarantines tripped and lifted."""
    return await announced(redis_client, RUNTIME_STREAM_KEY)


async def forget_announcements(redis_client: RedisClient) -> None:
    """Empty the changes stream, so a test sees only what its own writes announce, not its fixtures'."""
    await redis_client.client.delete(CHANGES_STREAM_KEY)


# The pipelines a test's Redis client executed, each as (whether it was a
# MULTI transaction, the commands it carried in order).
Executed = list[tuple[bool, list[str]]]


@pytest.fixture
def pipelines(redis_client: RedisClient, monkeypatch: pytest.MonkeyPatch) -> Executed:
    """Every pipeline the client executes from here on, as it was sent; each still runs."""
    executed: Executed = []
    open_pipeline = redis_client.client.pipeline

    def pipeline(transaction: bool = True, shard_hint: str | None = None) -> Any:
        pipe = open_pipeline(transaction=transaction, shard_hint=shard_hint)
        execute = pipe.execute

        async def sent(raise_on_error: bool = True) -> list[Any]:
            executed.append((pipe.is_transaction, [str(args[0]) for args, _ in pipe.command_stack]))
            return await execute(raise_on_error)

        pipe.execute = sent  # type: ignore[method-assign]
        return pipe

    monkeypatch.setattr(redis_client.client, "pipeline", pipeline)
    return executed


class Witness(ChangeLog):
    """A change log that appends nothing and looks, as each entry is recorded, at what a consumer woken by it would find.

    ``seen`` keeps (kind, ref, what ``look`` read of the row then), so a test tells an entry recorded after its commit
    (the write is there) from one recorded before it (the row as it was).
    """

    def __init__(self, redis_client: RedisClient, look: Callable[[str], Awaitable[object]]) -> None:
        super().__init__(redis_client)
        self._look = look
        self.seen: list[tuple[ChangeKind, str, object]] = []

    async def record(self, kind: ChangeKind, *ids: str) -> None:
        ref = ref_of(ids)
        self.seen.append((kind, ref, await self._look(ref)))
