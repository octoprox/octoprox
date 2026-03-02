# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Pytest configuration and fixtures for Octoprox tests.

This module provides shared fixtures using testcontainers for PostgreSQL and Redis,
along with FastAPI test client fixtures for integration testing.
"""
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from api.core.config import Settings
from api.db.redis import RedisClient

# =============================================================================
# Session-scoped containers (shared across all tests for performance)
# =============================================================================

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


# =============================================================================
# Test Settings
# =============================================================================


@pytest.fixture
def test_settings(
    test_redis_url: str,
    postgres_container: PostgresContainer,
) -> Settings:
    """Create test settings pointing to testcontainers."""
    return Settings(
        db_host=postgres_container.get_container_host_ip(),
        db_port=int(postgres_container.get_exposed_port(5432)),
        db_user=postgres_container.username,
        db_password=postgres_container.password,
        db_name=postgres_container.dbname,
        redis_url=test_redis_url,
        auth_username="testadmin",
        auth_password="testpassword123",
        jwt_secret="test-jwt-secret-key",
        jwt_expiry_hours=1,
        log_level="DEBUG",
        health_check_interval=3600,  # Disable health checks in tests
        metrics_flush_interval=3600,  # Disable metrics flushing in tests
        proxy_port=0,  # Use port 0 to let OS assign a free port
    )


# =============================================================================
# Database fixtures
# =============================================================================


@pytest.fixture
async def db_engine(
    test_settings: Settings,
    async_client: TestClient,
) -> AsyncGenerator[Any, None]:
    """Create an async database engine for tests.

    Depends on async_client to ensure migrations have run via the app's lifespan.
    """
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
    """Create a database session with automatic cleanup after each test."""
    async with db_session_factory() as session:
        yield session
        # Rollback any uncommitted changes
        await session.rollback()

    # Clean up all tables after each test
    async with db_engine.begin() as conn:
        # Use CASCADE to handle foreign key constraints
        await conn.execute(text("TRUNCATE TABLE proxy_metrics CASCADE"))
        await conn.execute(text("TRUNCATE TABLE proxies CASCADE"))
        await conn.execute(text("TRUNCATE TABLE connectors CASCADE"))
        await conn.execute(text("TRUNCATE TABLE credentials CASCADE"))
        await conn.execute(text("TRUNCATE TABLE projects CASCADE"))
        await conn.execute(text("TRUNCATE TABLE users CASCADE"))
        await conn.commit()


# =============================================================================
# Redis fixtures
# =============================================================================


@pytest.fixture
async def redis_client(test_redis_url: str) -> AsyncGenerator[RedisClient, None]:
    """Create a Redis client connected to the test container."""
    client = RedisClient(test_redis_url)
    await client.connect()
    yield client
    # Clean up all keys after each test
    await client.client.flushdb()
    await client.close()


# =============================================================================
# Application fixtures
# =============================================================================


@pytest.fixture
def mock_proxy_manager() -> MagicMock:
    """Create a mock ProxyManager for isolated route testing."""
    manager = MagicMock()
    manager.projects = []
    manager.proxies = []
    manager.healthy_proxies = []
    manager.credentials = []
    manager.connectors = []
    manager.get_project = MagicMock(return_value=None)
    manager.get_proxy = MagicMock(return_value=None)
    manager.get_credential = MagicMock(return_value=None)
    manager.get_connector = MagicMock(return_value=None)
    manager.add_project = AsyncMock()
    manager.update_project = AsyncMock()
    manager.remove_project = AsyncMock(return_value=True)
    manager.add_credential = AsyncMock()
    manager.update_credential = AsyncMock()
    manager.remove_credential = AsyncMock(return_value=True)
    manager.add_connector = AsyncMock()
    manager.update_connector = AsyncMock()
    manager.remove_connector = AsyncMock(return_value=True)
    manager.add_proxy = AsyncMock()
    manager.update_proxy = AsyncMock()
    manager.remove_proxy = AsyncMock(return_value=True)
    return manager


@pytest.fixture
def app_with_lifespan(test_settings: Settings) -> Any:
    """Create a FastAPI app with full lifespan."""
    import api.core.auth
    import api.core.config
    import api.core.proxy_server
    import api.db.session
    import api.main
    import api.routes.auth
    from api.db.session import get_async_session_factory
    from api.main import create_app

    # Clear the session factory cache to avoid event loop issues between tests
    get_async_session_factory.cache_clear()

    # Patch settings in all modules that import it at module load time
    api.core.config.settings = test_settings
    api.main.settings = test_settings
    api.core.proxy_server.settings = test_settings
    api.routes.auth.settings = test_settings
    api.core.auth.settings = test_settings
    api.db.session.settings = test_settings

    app = create_app()

    yield app

    # Cleanup
    get_async_session_factory.cache_clear()


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
