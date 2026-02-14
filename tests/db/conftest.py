"""Database-specific test fixtures."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    MetricsRepository,
    ProjectRepository,
    ProxyRepository,
)


@pytest.fixture
def project_repo(db_session: AsyncSession) -> ProjectRepository:
    """Create a ProjectRepository instance."""
    return ProjectRepository(db_session)


@pytest.fixture
def credential_repo(db_session: AsyncSession) -> CredentialRepository:
    """Create a CredentialRepository instance."""
    return CredentialRepository(db_session)


@pytest.fixture
def connector_repo(db_session: AsyncSession) -> ConnectorRepository:
    """Create a ConnectorRepository instance."""
    return ConnectorRepository(db_session)


@pytest.fixture
def proxy_repo(db_session: AsyncSession) -> ProxyRepository:
    """Create a ProxyRepository instance."""
    return ProxyRepository(db_session)


@pytest.fixture
def metrics_repo(db_session: AsyncSession) -> MetricsRepository:
    """Create a MetricsRepository instance."""
    return MetricsRepository(db_session)

