# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for ProxyRepository."""
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol


class TestProxyRepository:
    """Tests for ProxyRepository CRUD operations."""

    async def _create_project_credential_connector(
        self,
        project_repo: ProjectRepository,
        credential_repo: CredentialRepository,
        connector_repo: ConnectorRepository,
        session: AsyncSession,
        suffix: str = "",
    ) -> tuple[Project, Credential, Connector]:
        """Helper to create prerequisites for proxy tests."""
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

        connector = Connector(
            name=f"Test Connector{suffix}",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
            enabled=True,
        )
        await connector_repo.create(connector)
        await session.commit()

        return project, credential, connector

    async def test_create_proxy(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test creating a new proxy."""
        project, credential, connector = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session
        )

        proxy = Proxy(
            host="proxy.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )

        result = await proxy_repo.create(proxy)
        await db_session.commit()

        assert result.id == proxy.id
        assert result.host == "proxy.example.com"
        assert result.port == 8080

    async def test_get_all_proxies(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving all proxies."""
        project, credential, connector = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session
        )

        for i in range(3):
            proxy = Proxy(
                host=f"proxy{i}.example.com",
                port=8080 + i,
                protocol=ProxyProtocol.HTTP,
                connector_id=connector.id,
            )
            await proxy_repo.create(proxy)
        await db_session.commit()

        proxies = await proxy_repo.get_all()

        assert len(proxies) == 3

    async def test_get_by_connector(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving proxies by connector."""
        project1, cred1, conn1 = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session, "_1"
        )
        project2, cred2, conn2 = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session, "_2"
        )

        # Create proxies for connector1
        for i in range(2):
            proxy = Proxy(
                host=f"c1proxy{i}.example.com",
                port=8080,
                protocol=ProxyProtocol.HTTP,
                connector_id=conn1.id,
            )
            await proxy_repo.create(proxy)

        # Create proxy for connector2
        proxy = Proxy(
            host="c2proxy.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=conn2.id,
        )
        await proxy_repo.create(proxy)
        await db_session.commit()

        c1_proxies = await proxy_repo.get_by_connector(conn1.id)
        c2_proxies = await proxy_repo.get_by_connector(conn2.id)

        assert len(c1_proxies) == 2
        assert len(c2_proxies) == 1

    async def test_get_by_id(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test retrieving a proxy by ID."""
        project, credential, connector = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session
        )

        proxy = Proxy(
            host="findme.example.com",
            port=3128,
            protocol=ProxyProtocol.SOCKS5,
            connector_id=connector.id,
            username="proxyuser",
            password="proxypass",
        )
        await proxy_repo.create(proxy)
        await db_session.commit()

        result = await proxy_repo.get_by_id(proxy.id)

        assert result is not None
        assert result.id == proxy.id
        assert result.host == "findme.example.com"
        assert result.username == "proxyuser"

    async def test_get_by_id_not_found(
        self,
        proxy_repo: ProxyRepository,
    ) -> None:
        """Test retrieving a non-existent proxy."""
        result = await proxy_repo.get_by_id("non-existent-id")
        assert result is None

    async def test_update_proxy(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test updating a proxy."""
        project, credential, connector = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session
        )

        proxy = Proxy(
            host="original.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await proxy_repo.create(proxy)
        await db_session.commit()

        proxy.host = "updated.example.com"
        proxy.port = 3128
        proxy.protocol = ProxyProtocol.SOCKS5
        await proxy_repo.update(proxy)
        await db_session.commit()

        result = await proxy_repo.get_by_id(proxy.id)
        assert result is not None
        assert result.host == "updated.example.com"
        assert result.port == 3128
        assert result.protocol == ProxyProtocol.SOCKS5

    async def test_delete_proxy(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test deleting a proxy."""
        project, credential, connector = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session
        )

        proxy = Proxy(
            host="deleteme.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await proxy_repo.create(proxy)
        await db_session.commit()

        await proxy_repo.delete(proxy.id)
        await db_session.commit()

        result = await proxy_repo.get_by_id(proxy.id)
        assert result is None

    async def test_proxy_protocols(
        self,
        proxy_repo: ProxyRepository,
        connector_repo: ConnectorRepository,
        credential_repo: CredentialRepository,
        project_repo: ProjectRepository,
        db_session: AsyncSession,
    ) -> None:
        """Test creating proxies with different protocols."""
        project, credential, connector = await self._create_project_credential_connector(
            project_repo, credential_repo, connector_repo, db_session
        )

        protocols = [ProxyProtocol.HTTP, ProxyProtocol.HTTPS, ProxyProtocol.SOCKS5]

        for i, protocol in enumerate(protocols):
            proxy = Proxy(
                host=f"proxy{i}.example.com",
                port=8080,
                protocol=protocol,
                connector_id=connector.id,
            )
            await proxy_repo.create(proxy)

        await db_session.commit()

        proxies = await proxy_repo.get_all()
        assert len(proxies) == len(protocols)

