# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures: Postgres and Redis testcontainers, the app of a role whose node a ``TestClient`` runs, and app helpers.

A test that asks for ``db_session`` or ``redis_client`` starts on empty tables or an empty Redis and leaves them empty,
but the route tests' api apps (``app_with_lifespan``) leave Postgres as their node wrote it, seeds included.
"""
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from api.core.config import Settings
from api.core.roles import JobKind, Role
from api.db.migrations import MIGRATIONS_DIR, run_migrations
from api.db.redis import JOB_STREAM_KEY, RedisClient
from api.db.streams import ChangeLog

if TYPE_CHECKING:
    from api.roles.api import ApiRuntime

# Every table a test may write, emptied by ``db_session`` before and after its
# test. CASCADE also empties the tables that reference these (project_metrics).
# One statement, not one per table: one per table took about 170 ms, the single
# statement about 75 ms.
_TRUNCATE_TABLES = text(
    "TRUNCATE TABLE "
    "metric_totals, metric_flushes, proxy_metrics, connector_metrics, host_metrics, failure_metrics, system_metrics, "
    "proxies, connectors, credentials, projects, api_tokens, auth_identities, auth_providers, users, "
    "provider_audit_log, provider_descriptors, ip_observations, connector_exit_ips, geo_database_blobs, "
    "geo_databases, geo_settings, tunnel_peer_metrics, wireguard_peers, wireguard_settings, "
    "openvpn_peers, openvpn_settings "
    "CASCADE"
)

# =============================================================================
# Session-scoped containers (shared across all tests for performance)
# =============================================================================

@pytest.fixture(scope="session", autouse=True)
def _detach_log_handlers_at_exit() -> Generator[None, None, None]:
    """Detach the app's log handlers when the session ends, so no record logged at exit prints "--- Logging error ---".

    They write to pytest's capture, closed by then, and urllib3 logs testcontainers' removal of its reaper at exit.
    """
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer, None, None]:
    """Start a PostgreSQL container for the test session."""
    with PostgresContainer("postgres:16-alpine") as postgres:
        yield postgres


@pytest.fixture(scope="session")
def redis_container() -> Generator[RedisContainer, None, None]:
    """Start a Redis container for the test session."""
    with RedisContainer("redis:7-alpine") as redis:
        yield redis


@pytest.fixture(scope="session")
def test_redis_url(redis_container: RedisContainer) -> str:
    """Get the Redis URL for the test Redis container."""
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


def _database_settings(postgres_container: PostgresContainer) -> dict[str, Any]:
    """The settings that point at the test Postgres container."""
    return {
        "db_host": postgres_container.get_container_host_ip(),
        "db_port": int(postgres_container.get_exposed_port(5432)),
        "db_user": postgres_container.username,
        "db_password": postgres_container.password,
        "db_name": postgres_container.dbname,
    }


@pytest.fixture(scope="session")
def migrated_database(postgres_container: PostgresContainer) -> str:
    """The test database brought to head once per session, without starting an app; its sync URL.

    Migration tests that go down a revision come back up to head before they end.
    """
    url = Settings(**_database_settings(postgres_container)).database_url_sync
    run_migrations(url)
    return url


# The newest revision of the migrations, which a database at head records.
HEAD = ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()


def tables(url: str) -> set[str]:
    """The tables of the database at ``url``, a sync URL."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return set(conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def test_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Where test apps cache IP database files and keep the MITM CA, instead of data/ in the working tree."""
    return tmp_path_factory.mktemp("data")


# =============================================================================
# Test Settings
# =============================================================================


@pytest.fixture
def test_settings(
    test_redis_url: str,
    postgres_container: PostgresContainer,
    test_data_dir: Path,
) -> Settings:
    """Create test settings pointing to testcontainers."""
    return Settings(
        **_database_settings(postgres_container),
        redis_url=test_redis_url,
        auth_username="testadmin",
        auth_password="testpassword123",
        jwt_secret="test-jwt-secret-key",
        jwt_expiry_hours=1,
        log_level="DEBUG",
        health_check_interval=3600,  # Disable health checks in tests
        metrics_flush_interval=3600,  # Disable metrics flushing in tests
        system_metrics_interval=0,  # No background system snapshots in tests
        update_check=False,  # No requests to octoprox.com from tests
        proxy_port=0,  # Use port 0 to let OS assign a free port
        geo_lookup_enabled=False,  # No requests through proxies in tests
        geo_cache_dir=str(test_data_dir / "geo"),
        tls_mitm_ca_cert_path=str(test_data_dir / "ca" / "octoprox-ca.crt"),
        tls_mitm_ca_key_path=str(test_data_dir / "ca" / "octoprox-ca.key"),
    )


# =============================================================================
# Database fixtures
# =============================================================================


@pytest.fixture
async def db_engine(
    test_settings: Settings,
    migrated_database: str,
) -> AsyncGenerator[Any, None]:
    """Create an async database engine for tests, on the migrated schema (no app is started)."""
    engine = create_async_engine(
        test_settings.database_url,
        echo=False,
        pool_pre_ping=True,
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session_factory(
    db_engine: Any,
) -> async_sessionmaker[AsyncSession]:
    """Create a session factory for tests."""
    return async_sessionmaker(
        db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )



@pytest.fixture
async def db_session(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_engine: Any,
) -> AsyncGenerator[AsyncSession, None]:
    """A database session on empty tables, emptied again after the test.

    Emptied first too, because the app fixtures leave the rows their nodes
    wrote (see the module docstring), and a test must not depend on what
    ran before it.
    """
    async with db_engine.begin() as conn:
        await conn.execute(_TRUNCATE_TABLES)
    async with db_session_factory() as session:
        yield session
        # Rollback any uncommitted changes
        await session.rollback()

    # Clean up all tables after each test.
    async with db_engine.begin() as conn:
        await conn.execute(_TRUNCATE_TABLES)


# =============================================================================
# Redis fixtures
# =============================================================================


@pytest.fixture
async def redis_client(test_redis_url: str) -> AsyncGenerator[RedisClient, None]:
    """A Redis client connected to the test container, on an empty Redis that is flushed again after the test."""
    client = RedisClient(test_redis_url)
    await client.connect()
    await client.client.flushdb()
    yield client
    # Clean up all keys after each test
    await client.client.flushdb()
    await client.close()


async def queued_job_keys(redis_client: RedisClient, kind: JobKind) -> list[str]:
    """The ``key`` of each entry on ``kind``'s job stream, oldest first: the runs asked for and not completed yet."""
    entries = await redis_client.client.xrange(JOB_STREAM_KEY.format(kind=kind))
    return [fields.get("key", "") for _entry_id, fields in entries]


class Clock:
    """A stand-in for a module's ``time``, set by a test: ``time()`` reads ``now``, the epoch seconds the test moves."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture
async def change_log(redis_client: RedisClient) -> AsyncGenerator[ChangeLog, None]:
    """A change log on the test Redis, closed after the test so no retry outlives it; it writes ``CHANGES_STREAM_KEY``."""
    log = ChangeLog(redis_client)
    yield log
    await log.close()


# =============================================================================
# Application fixtures
# =============================================================================


def _use_settings(settings: Settings) -> None:
    """Point the modules that read the module-global ``settings`` at ``settings``.

    ``create_app`` passes its settings to everything it builds, but a few
    modules still read the global: the auth dependency (the JWT secret),
    ``get_db`` (whose engine must be the node's, one per process), and the
    auth and sign-in routes.
    """
    import api.core.auth
    import api.core.config
    import api.db.session
    import api.routes.auth
    import api.routes.sso

    api.core.config.settings = settings
    api.core.auth.settings = settings
    api.db.session.settings = settings
    api.routes.auth.settings = settings
    api.routes.sso.settings = settings


@pytest.fixture
def app_role() -> Role:
    """The role the app fixtures boot: ``api``, as route tests run (no worker; they assert the jobs a write asks for).

    A test module overrides this fixture to boot another role.
    """
    return Role.API


@pytest.fixture
def app_with_lifespan(
    test_settings: Settings, app_role: Role, redis_client: RedisClient
) -> Generator[FastAPI, None, None]:
    """The app of a process of ``app_role`` (``api`` by default) on the test containers; a client's lifespan runs its node.

    Its settings are ``test_settings`` itself, so a test that changes one changes what the app reads, and Redis is
    flushed once the node has stopped.
    """
    from api.db.session import get_async_session_factory
    from api.main import create_app

    # A fresh engine per app: each TestClient runs the app on its own event loop.
    get_async_session_factory.cache_clear()
    test_settings.role = app_role
    _use_settings(test_settings)
    yield create_app(test_settings)
    get_async_session_factory.cache_clear()


@pytest.fixture
def build_app(
    test_settings: Settings, db_session: AsyncSession, redis_client: RedisClient
) -> Generator[Callable[..., FastAPI], None, None]:
    """Builds the app of any role from a copy of the test settings with overrides, to run under ``with TestClient(app)``.

    For example ``build_app(Role.GATEWAY, proxy_port=0)``; Postgres and Redis are emptied before the test's first node
    starts and once its nodes have stopped, so a test asks for this, not also for ``app_with_lifespan``.
    """
    from api.db.session import get_async_session_factory
    from api.main import create_app

    def build(role: Role, **overrides: Any) -> FastAPI:
        settings = test_settings.model_copy(update={"role": role, **overrides})
        get_async_session_factory.cache_clear()
        _use_settings(settings)
        return create_app(settings)

    yield build
    get_async_session_factory.cache_clear()


def on_app_loop[T](client: TestClient, call: Callable[..., Awaitable[T]], *args: Any) -> T:
    """``call(*args)`` awaited on the event loop the client's app runs on, where the node's clients live."""
    assert client.portal is not None, "use the TestClient as a context manager"
    return client.portal.call(call, *args)


def api_runtime(client: TestClient) -> "ApiRuntime":
    """The api runtime of the app the client serves."""
    runtime = client.app.state.node.api  # type: ignore[attr-defined]
    assert runtime is not None
    return cast("ApiRuntime", runtime)


def queued_jobs(client: TestClient, kind: JobKind) -> set[str]:
    """The keys of ``kind`` with a run asked for and not taken yet in the app's Redis: those on its job stream."""
    return set(on_app_loop(client, queued_job_keys, api_runtime(client).redis_client, kind))


@pytest.fixture
def async_client(app_with_lifespan: Any) -> Generator[TestClient, None, None]:
    """Create a test client (unauthenticated) for testing endpoints."""
    with TestClient(app_with_lifespan) as client:
        yield client


@pytest.fixture
def auth_headers(test_settings: Settings) -> dict[str, str]:
    """Create admin authentication headers with a valid JWT token."""
    from api.routes.auth import create_jwt

    token = create_jwt(
        payload={
            "sub": test_settings.auth_username,
            "user_id": "test-admin-id",
            "role": "admin",
        },
        secret=test_settings.jwt_secret,
        expiry_hours=test_settings.jwt_expiry_hours,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def editor_auth_headers(test_settings: Settings) -> dict[str, str]:
    """Create editor authentication headers with a valid JWT token."""
    from api.routes.auth import create_jwt

    token = create_jwt(
        payload={
            "sub": "testeditor",
            "user_id": "test-editor-id",
            "role": "editor",
        },
        secret=test_settings.jwt_secret,
        expiry_hours=test_settings.jwt_expiry_hours,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def viewer_auth_headers(test_settings: Settings) -> dict[str, str]:
    """Create viewer authentication headers with a valid JWT token."""
    from api.routes.auth import create_jwt

    token = create_jwt(
        payload={
            "sub": "testviewer",
            "user_id": "test-viewer-id",
            "role": "viewer",
        },
        secret=test_settings.jwt_secret,
        expiry_hours=test_settings.jwt_expiry_hours,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def authenticated_client(
    app_with_lifespan: Any,
    auth_headers: dict[str, str],
) -> Generator[TestClient, None, None]:
    """Create an authenticated test client (admin role)."""
    with TestClient(app_with_lifespan, headers=auth_headers) as client:
        yield client


@pytest.fixture
def editor_client(
    app_with_lifespan: Any,
    editor_auth_headers: dict[str, str],
) -> Generator[TestClient, None, None]:
    """Create an authenticated test client with editor role."""
    with TestClient(app_with_lifespan, headers=editor_auth_headers) as client:
        yield client


@pytest.fixture
def viewer_client(
    app_with_lifespan: Any,
    viewer_auth_headers: dict[str, str],
) -> Generator[TestClient, None, None]:
    """Create an authenticated test client with viewer role."""
    with TestClient(app_with_lifespan, headers=viewer_auth_headers) as client:
        yield client


# =============================================================================
# Created entity fixtures (for tests that need pre-existing data)
# These fixtures use authenticated_client to work with auth-enabled tests
# =============================================================================


@pytest.fixture
def created_project(
    authenticated_client: TestClient,
    sample_project_data: dict[str, Any],
) -> dict[str, Any]:
    """Create a project and return its data."""
    response = authenticated_client.post("/api/v1/projects", json=sample_project_data)
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def created_credential(
    authenticated_client: TestClient,
    created_project: dict[str, Any],
    sample_credential_data: dict[str, Any],
) -> dict[str, Any]:
    """Create a credential and return its data."""
    project_id = created_project["id"]
    response = authenticated_client.post(
        f"/api/v1/projects/{project_id}/credentials",
        json=sample_credential_data,
    )
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def created_connector(
    authenticated_client: TestClient,
    created_project: dict[str, Any],
    created_credential: dict[str, Any],
    sample_connector_data: dict[str, Any],
) -> dict[str, Any]:
    """Create a connector and return its data."""
    project_id = created_project["id"]
    connector_data = sample_connector_data.copy()
    connector_data["credential_id"] = created_credential["id"]
    response = authenticated_client.post(
        f"/api/v1/projects/{project_id}/connectors",
        json=connector_data,
    )
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def created_proxy(
    authenticated_client: TestClient,
    created_project: dict[str, Any],
    created_connector: dict[str, Any],
    sample_proxy_data: dict[str, Any],
) -> dict[str, Any]:
    """Create a proxy and return its data."""
    project_id = created_project["id"]
    proxy_data = sample_proxy_data.copy()
    proxy_data["connector_id"] = created_connector["id"]
    response = authenticated_client.post(
        f"/api/v1/projects/{project_id}/proxies",
        json=proxy_data,
    )
    assert response.status_code == 201
    return response.json()


# =============================================================================
# Sample data fixtures
# =============================================================================

@pytest.fixture
def sample_proxy_data() -> dict[str, Any]:
    """Sample proxy data for testing."""
    return {
        "host": "proxy.example.com",
        "port": 8080,
        "protocol": "http",
    }


@pytest.fixture
def sample_project_data() -> dict[str, Any]:
    """Sample project data for API requests."""
    import uuid

    unique_id = uuid.uuid4().hex[:8]
    return {
        "name": f"API Test Project {unique_id}",
        "description": "Created via API",
        "username": f"apiuser_{unique_id}",
        "password": "apipass123",
        "routing_strategy": "round_robin",
    }


@pytest.fixture
def sample_credential_data() -> dict[str, Any]:
    """Sample credential data for API requests."""
    return {
        "name": "API Test Credential",
        "type": "static_proxy_provider",
        "config": {},
    }


@pytest.fixture
def sample_connector_data() -> dict[str, Any]:
    """Sample connector data for API requests."""
    return {
        "name": "API Test Connector",
        "credential_id": "placeholder",  # Will be set in tests
        "config": {},
        "enabled": True,
    }
