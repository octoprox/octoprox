# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for ConnectorRepository."""

from sqlalchemy.ext.asyncio import AsyncSession

from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    ProjectRepository,
)
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project


class TestConnectorRepository:
    """Tests for ConnectorRepository CRUD operations."""

    async def _create_project_and_credential(
        self,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        session: AsyncSession,
        suffix: str = "",
    ) -> tuple[Project, Credential]:
        """Helper to create a project and credential for connector tests."""
        project = Project(
            name=f"Test Project{suffix}",
            username=f"user{suffix}",
            password="pass",
        )
        await project_repo.create(project)

        credential = Credential(
            name=f"Test Credential{suffix}",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await credential_repo.create(credential)
        await session.commit()

        return project, credential

    async def test_create_connector(
        self,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test creating a new connector."""
        project, credential = await self._create_project_and_credential(
            project_repo, credential_repo, db_session
        )

        connector = Connector(
            name="Test Connector",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={"region": "us-east-1"},
            enabled=True,
        )

        result = await connector_repo.create(connector)
        await db_session.commit()

        assert result.id == connector.id
        assert result.name == "Test Connector"
        assert result.config == {"region": "us-east-1"}

    async def test_get_all_connectors(
        self,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving all connectors."""
        project, credential = await self._create_project_and_credential(
            project_repo, credential_repo, db_session
        )

        for i in range(3):
            connector = Connector(
                name=f"Connector {i}",
                credential_id=credential.id,
                credential_type=CredentialType.STATIC_PROXY_PROVIDER,
                project_id=project.id,
                config={},
            )
            await connector_repo.create(connector)
        await db_session.commit()

        connectors = await connector_repo.get_all()

        assert len(connectors) == 3

    async def test_get_by_project(
        self,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving connectors by project."""
        project1, cred1 = await self._create_project_and_credential(
            project_repo, credential_repo, db_session, "_1"
        )
        project2, cred2 = await self._create_project_and_credential(
            project_repo, credential_repo, db_session, "_2"
        )

        # Create connectors for project1
        for i in range(2):
            conn = Connector(
                name=f"P1 Conn {i}",
                credential_id=cred1.id,
                credential_type=CredentialType.STATIC_PROXY_PROVIDER,
                project_id=project1.id,
                config={},
            )
            await connector_repo.create(conn)

        # Create connector for project2
        conn = Connector(
            name="P2 Conn",
            credential_id=cred2.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project2.id,
            config={},
        )
        await connector_repo.create(conn)
        await db_session.commit()

        p1_conns = await connector_repo.get_by_project(project1.id)
        p2_conns = await connector_repo.get_by_project(project2.id)

        assert len(p1_conns) == 2
        assert len(p2_conns) == 1

    async def test_get_by_id(
        self,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving a connector by ID."""
        project, credential = await self._create_project_and_credential(
            project_repo, credential_repo, db_session
        )

        connector = Connector(
            name="Find Me",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={"key": "value"},
        )
        await connector_repo.create(connector)
        await db_session.commit()

        result = await connector_repo.get_by_id(connector.id)

        assert result is not None
        assert result.id == connector.id
        assert result.config == {"key": "value"}

    async def test_get_by_id_not_found(
        self,
        connector_repo: ConnectorRepository,
    ) -> None:
        """Test retrieving a non-existent connector."""
        result = await connector_repo.get_by_id("non-existent-id")
        assert result is None

    async def test_update_connector(
        self,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test updating a connector."""
        project, credential = await self._create_project_and_credential(
            project_repo, credential_repo, db_session
        )

        connector = Connector(
            name="Original",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
            enabled=True,
        )
        await connector_repo.create(connector)
        await db_session.commit()

        connector.name = "Updated"
        connector.enabled = False
        connector.config = {"updated": True}
        await connector_repo.update(connector)
        await db_session.commit()

        result = await connector_repo.get_by_id(connector.id)
        assert result is not None
        assert result.name == "Updated"
        assert result.enabled is False
        assert result.config == {"updated": True}

    async def test_create_connector_with_routing_config(
        self,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test creating a connector with routing config."""
        project, credential = await self._create_project_and_credential(
            project_repo, credential_repo, db_session
        )

        connector = Connector(
            name="Routing Connector",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
            routing_config={"domain_whitelist": ["example.com", "test.org"]},
        )

        result = await connector_repo.create(connector)
        await db_session.commit()

        assert result.routing_config == {"domain_whitelist": ["example.com", "test.org"]}

    async def test_get_connector_preserves_routing_config(
        self,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test that routing config is preserved through create/get cycle."""
        project, credential = await self._create_project_and_credential(
            project_repo, credential_repo, db_session
        )

        connector = Connector(
            name="Persist Routing",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
            routing_config={"domain_blacklist": ["blocked.com"]},
        )
        await connector_repo.create(connector)
        await db_session.commit()

        result = await connector_repo.get_by_id(connector.id)
        assert result is not None
        assert result.routing_config == {"domain_blacklist": ["blocked.com"]}

    async def test_update_connector_routing_config(
        self,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test updating a connector's routing config."""
        project, credential = await self._create_project_and_credential(
            project_repo, credential_repo, db_session
        )

        connector = Connector(
            name="Update Routing",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
            routing_config={},
        )
        await connector_repo.create(connector)
        await db_session.commit()

        # Update routing config
        connector.routing_config = {"domain_whitelist": ["new-domain.com"]}
        await connector_repo.update(connector)
        await db_session.commit()

        result = await connector_repo.get_by_id(connector.id)
        assert result is not None
        assert result.routing_config == {"domain_whitelist": ["new-domain.com"]}

    async def test_connector_empty_routing_config(
        self,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test that empty routing config defaults to empty dict."""
        project, credential = await self._create_project_and_credential(
            project_repo, credential_repo, db_session
        )

        connector = Connector(
            name="No Routing",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await connector_repo.create(connector)
        await db_session.commit()

        result = await connector_repo.get_by_id(connector.id)
        assert result is not None
        assert result.routing_config == {}

    async def test_delete_connector(
        self,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test deleting a connector."""
        project, credential = await self._create_project_and_credential(
            project_repo, credential_repo, db_session
        )

        connector = Connector(
            name="Delete Me",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await connector_repo.create(connector)
        await db_session.commit()

        await connector_repo.delete(connector.id)
        await db_session.commit()

        result = await connector_repo.get_by_id(connector.id)
        assert result is None

