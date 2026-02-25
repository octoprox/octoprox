# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for ProxyManager."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.proxy_manager import ProxyManager
from api.db.redis import RedisClient
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus


class TestProxyManager:
    """Tests for ProxyManager operations."""

    @pytest.fixture
    async def proxy_manager(
        self,
        db_session_factory: async_sessionmaker[AsyncSession],
        redis_client: RedisClient,
        test_settings: Settings,
        db_session: AsyncSession, # Required to ensure tables are cleaned up
    ) -> ProxyManager:
        """Create a ProxyManager instance for testing."""
        manager = ProxyManager(
            session_factory=db_session_factory,
            redis_client=redis_client,
            settings=test_settings,
        )
        await manager._load_from_database()
        await manager._hydrate_from_redis()
        return manager

    async def test_add_project(self, proxy_manager: ProxyManager) -> None:
        """Test adding a project."""
        project = Project(
            name="Test Project",
            username="testuser",
            password="testpass",
        )

        await proxy_manager.add_project(project)

        assert project.id in [p.id for p in proxy_manager.projects]
        assert proxy_manager.get_project(project.id) is not None

    async def test_get_project_by_username(self, proxy_manager: ProxyManager) -> None:
        """Test getting a project by username."""
        project = Project(
            name="Username Test",
            username="uniqueuser",
            password="pass",
        )
        await proxy_manager.add_project(project)

        result = proxy_manager.get_project_by_username("uniqueuser")

        assert result is not None
        assert result.id == project.id

    async def test_update_project(self, proxy_manager: ProxyManager) -> None:
        """Test updating a project."""
        project = Project(
            name="Original",
            username="updateuser",
            password="pass",
        )
        await proxy_manager.add_project(project)

        project.name = "Updated"
        await proxy_manager.update_project(project)

        result = proxy_manager.get_project(project.id)
        assert result is not None
        assert result.name == "Updated"

    async def test_remove_project(self, proxy_manager: ProxyManager) -> None:
        """Test removing a project."""
        project = Project(
            name="Delete Me",
            username="deleteuser",
            password="pass",
        )
        await proxy_manager.add_project(project)

        result = await proxy_manager.remove_project(project.id)

        assert result is True
        assert proxy_manager.get_project(project.id) is None

    async def test_add_credential(self, proxy_manager: ProxyManager) -> None:
        """Test adding a credential."""
        project = Project(name="Cred Project", username="creduser", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Test Credential",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        assert credential.id in [c.id for c in proxy_manager.credentials]

    async def test_add_connector(self, proxy_manager: ProxyManager) -> None:
        """Test adding a connector."""
        project = Project(name="Conn Project", username="connuser", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Conn Credential",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        connector = Connector(
            name="Test Connector",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_connector(connector)

        assert connector.id in [c.id for c in proxy_manager.connectors]

    async def test_add_proxy(self, proxy_manager: ProxyManager) -> None:
        """Test adding a proxy."""
        # Create full chain
        project = Project(name="Proxy Project", username="proxyuser", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Proxy Credential",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        connector = Connector(
            name="Proxy Connector",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_connector(connector)

        proxy = Proxy(
            host="proxy.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await proxy_manager.add_proxy(proxy)

        assert proxy.id in [p.id for p in proxy_manager.proxies]

    async def test_get_proxies_for_project(self, proxy_manager: ProxyManager) -> None:
        """Test getting proxies for a specific project."""
        # Create project with proxies
        project = Project(name="Multi Proxy", username="multiuser", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Multi Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        connector = Connector(
            name="Multi Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_connector(connector)

        for i in range(3):
            proxy = Proxy(
                host=f"proxy{i}.example.com",
                port=8080,
                protocol=ProxyProtocol.HTTP,
                connector_id=connector.id,
            )
            await proxy_manager.add_proxy(proxy)

        proxies = proxy_manager.get_proxies_for_project(project.id)
        assert len(proxies) == 3

    async def test_healthy_proxies(self, proxy_manager: ProxyManager) -> None:
        """Test getting healthy proxies."""
        # Create project with proxies
        project = Project(name="Health Project", username="healthuser", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Health Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        connector = Connector(
            name="Health Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_connector(connector)

        # Add healthy proxy
        healthy_proxy = Proxy(
            host="healthy.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        healthy_proxy.status = ProxyStatus.HEALTHY
        await proxy_manager.add_proxy(healthy_proxy)

        # Add unhealthy proxy
        unhealthy_proxy = Proxy(
            host="unhealthy.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        unhealthy_proxy.status = ProxyStatus.UNHEALTHY
        await proxy_manager.add_proxy(unhealthy_proxy)

        healthy = proxy_manager.get_healthy_proxies_for_project(project.id)
        assert len(healthy) == 1
        assert healthy[0].host == "healthy.example.com"

    async def test_update_proxy_status(self, proxy_manager: ProxyManager) -> None:
        """Test updating proxy health status."""
        # Create full chain
        project = Project(name="Status Project", username="statususer", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Status Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        connector = Connector(
            name="Status Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_connector(connector)

        proxy = Proxy(
            host="status.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await proxy_manager.add_proxy(proxy)

        # Update status to unhealthy
        await proxy_manager.update_proxy_status(
            proxy.id,
            status=ProxyStatus.UNHEALTHY,
            latency_ms=0,
            consecutive_failures=3,
        )

        updated_proxy = proxy_manager.get_proxy(proxy.id)
        assert updated_proxy is not None
        assert updated_proxy.status == ProxyStatus.UNHEALTHY
        assert updated_proxy.consecutive_failures == 3

    async def test_set_project_strategy(self, proxy_manager: ProxyManager) -> None:
        """Test changing project routing strategy."""
        project = Project(
            name="Strategy Project",
            username="strategyuser",
            password="pass",
            routing_strategy="round_robin",
        )
        await proxy_manager.add_project(project)

        proxy_manager.set_project_strategy(project.id, "random")

        # Verify strategy was changed
        strategy = proxy_manager._project_strategies.get(project.id)
        assert strategy is not None
        assert strategy.name == "random"

    async def test_cascade_delete_project_cache(self, proxy_manager: ProxyManager) -> None:
        """Test that deleting a project cascades removal in cache.

        Note: The database requires manual deletion of dependent entities due to
        foreign key constraints. This test verifies the cache cleanup behavior.
        """
        # Create full chain
        project = Project(name="Cascade Project", username="cascadeuser", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Cascade Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        connector = Connector(
            name="Cascade Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_connector(connector)

        proxy = Proxy(
            host="cascade.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await proxy_manager.add_proxy(proxy)

        # Delete in reverse order of dependencies to respect foreign key constraints
        await proxy_manager.remove_proxy(proxy.id)
        await proxy_manager.remove_connector(connector.id)
        await proxy_manager.remove_credential(credential.id)
        await proxy_manager.remove_project(project.id)

        # Verify all are removed from cache
        assert proxy_manager.get_project(project.id) is None
        assert proxy_manager.get_credential(credential.id) is None
        assert proxy_manager.get_connector(connector.id) is None
        assert proxy_manager.get_proxy(proxy.id) is None

    async def test_remove_proxy_cleans_up_redis(
        self, proxy_manager: ProxyManager, redis_client: RedisClient
    ) -> None:
        """Test that removing a proxy cleans up Redis data."""
        # Create project, credential, connector, and proxy
        project = Project(name="Redis Cleanup", username="rediscleanup", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Redis Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        connector = Connector(
            name="Redis Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_connector(connector)

        proxy = Proxy(
            host="redis-cleanup.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await proxy_manager.add_proxy(proxy)

        # Add Redis data for the proxy
        await redis_client.set_proxy_status(proxy.id, ProxyStatus.HEALTHY, 50.0, 0)
        await redis_client.update_proxy_metrics(proxy.id, success=True, latency_ms=100)

        # Verify Redis data exists
        assert await redis_client.get_proxy_status(proxy.id) is not None
        assert await redis_client.get_proxy_metrics(proxy.id) is not None

        # Remove the proxy
        await proxy_manager.remove_proxy(proxy.id)

        # Verify Redis data is cleaned up
        assert await redis_client.get_proxy_status(proxy.id) is None
        assert await redis_client.get_proxy_metrics(proxy.id) is None

    async def test_remove_connector_cleans_up_redis(
        self, proxy_manager: ProxyManager, redis_client: RedisClient
    ) -> None:
        """Test that removing a connector cleans up Redis data for all its proxies."""
        # Create project, credential, connector, and proxies
        project = Project(name="Conn Cleanup", username="conncleanup", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Conn Cleanup Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        connector = Connector(
            name="Conn Cleanup Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_connector(connector)

        # Add multiple proxies
        proxy1 = Proxy(
            host="conn-cleanup1.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        proxy2 = Proxy(
            host="conn-cleanup2.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await proxy_manager.add_proxy(proxy1)
        await proxy_manager.add_proxy(proxy2)

        # Add Redis data for the proxies
        await redis_client.set_proxy_status(proxy1.id, ProxyStatus.HEALTHY, 50.0, 0)
        await redis_client.update_proxy_metrics(proxy1.id, success=True, latency_ms=100)
        await redis_client.set_proxy_status(proxy2.id, ProxyStatus.HEALTHY, 60.0, 0)
        await redis_client.update_proxy_metrics(proxy2.id, success=True, latency_ms=200)

        # Verify Redis data exists
        assert await redis_client.get_proxy_status(proxy1.id) is not None
        assert await redis_client.get_proxy_status(proxy2.id) is not None

        # Store proxy IDs before removal
        proxy1_id = proxy1.id
        proxy2_id = proxy2.id

        # Remove proxies first (required due to FK constraints), then connector
        # Note: remove_proxy also cleans up Redis, but we're testing the full flow
        await proxy_manager.remove_proxy(proxy1.id)
        await proxy_manager.remove_proxy(proxy2.id)
        await proxy_manager.remove_connector(connector.id)

        # Verify Redis data is cleaned up for both proxies
        assert await redis_client.get_proxy_status(proxy1_id) is None
        assert await redis_client.get_proxy_metrics(proxy1_id) is None
        assert await redis_client.get_proxy_status(proxy2_id) is None
        assert await redis_client.get_proxy_metrics(proxy2_id) is None

    async def test_remove_project_cleans_up_redis(
        self, proxy_manager: ProxyManager, redis_client: RedisClient
    ) -> None:
        """Test that removing a project cleans up Redis data for project metrics."""
        # Create a project without children to test project metrics cleanup
        project = Project(name="Proj Cleanup", username="projcleanup", password="pass")
        await proxy_manager.add_project(project)

        # Add Redis data for the project
        await redis_client.update_project_metrics(project.id, success=True, latency_ms=100)

        # Verify Redis data exists
        assert await redis_client.get_project_metrics(project.id) is not None

        # Store project ID before removal
        project_id = project.id

        # Remove the project
        await proxy_manager.remove_project(project.id)

        # Verify Redis data is cleaned up for project
        assert await redis_client.get_project_metrics(project_id) is None

    async def test_remove_project_with_proxies_cleans_up_redis(
        self, proxy_manager: ProxyManager, redis_client: RedisClient
    ) -> None:
        """Test that removing a project with proxies cleans up all Redis data."""
        # Create project, credential, connector, and proxy
        project = Project(name="Full Cleanup", username="fullcleanup", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Full Cleanup Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        connector = Connector(
            name="Full Cleanup Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_connector(connector)

        proxy = Proxy(
            host="full-cleanup.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await proxy_manager.add_proxy(proxy)

        # Add Redis data for the proxy and project
        await redis_client.set_proxy_status(proxy.id, ProxyStatus.HEALTHY, 50.0, 0)
        await redis_client.update_proxy_metrics(proxy.id, success=True, latency_ms=100)
        await redis_client.update_project_metrics(project.id, success=True, latency_ms=100)

        # Verify Redis data exists
        assert await redis_client.get_proxy_status(proxy.id) is not None
        assert await redis_client.get_proxy_metrics(proxy.id) is not None
        assert await redis_client.get_project_metrics(project.id) is not None

        # Store IDs before removal
        proxy_id = proxy.id
        project_id = project.id

        # Delete in reverse order of dependencies to respect foreign key constraints
        await proxy_manager.remove_proxy(proxy.id)
        await proxy_manager.remove_connector(connector.id)
        await proxy_manager.remove_credential(credential.id)
        await proxy_manager.remove_project(project.id)

        # Verify Redis data is cleaned up for proxy and project
        assert await redis_client.get_proxy_status(proxy_id) is None
        assert await redis_client.get_proxy_metrics(proxy_id) is None
        assert await redis_client.get_project_metrics(project_id) is None

    async def test_domain_whitelist_filters_proxies(self, proxy_manager: ProxyManager) -> None:
        """Test that domain whitelist filters proxy selection by target host."""
        project = Project(name="Domain WL", username="domainwl", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Domain Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        # Connector with whitelist: only example.com
        connector_wl = Connector(
            name="Whitelist Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
            routing_config={"domain_whitelist": ["example.com"]},
        )
        await proxy_manager.add_connector(connector_wl)

        proxy = Proxy(
            host="proxy1.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector_wl.id,
        )
        proxy.status = ProxyStatus.HEALTHY
        await proxy_manager.add_proxy(proxy)

        # Should find proxy for whitelisted domain
        healthy = proxy_manager.get_healthy_proxies_for_project(project.id, target_host="example.com")
        assert len(healthy) == 1

        # Should find proxy for subdomain of whitelisted domain
        healthy = proxy_manager.get_healthy_proxies_for_project(project.id, target_host="www.example.com")
        assert len(healthy) == 1

        # Should NOT find proxy for non-whitelisted domain
        healthy = proxy_manager.get_healthy_proxies_for_project(project.id, target_host="other.com")
        assert len(healthy) == 0

    async def test_domain_blacklist_filters_proxies(self, proxy_manager: ProxyManager) -> None:
        """Test that domain blacklist filters proxy selection by target host."""
        project = Project(name="Domain BL", username="domainbl", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="BL Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        # Connector with blacklist: block ads.example.com
        connector_bl = Connector(
            name="Blacklist Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
            routing_config={"domain_blacklist": ["ads.example.com"]},
        )
        await proxy_manager.add_connector(connector_bl)

        proxy = Proxy(
            host="proxy2.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector_bl.id,
        )
        proxy.status = ProxyStatus.HEALTHY
        await proxy_manager.add_proxy(proxy)

        # Should find proxy for non-blacklisted domain
        healthy = proxy_manager.get_healthy_proxies_for_project(project.id, target_host="example.com")
        assert len(healthy) == 1

        # Should NOT find proxy for blacklisted domain
        healthy = proxy_manager.get_healthy_proxies_for_project(project.id, target_host="ads.example.com")
        assert len(healthy) == 0

        # Should NOT find proxy for subdomain of blacklisted domain
        healthy = proxy_manager.get_healthy_proxies_for_project(project.id, target_host="sub.ads.example.com")
        assert len(healthy) == 0

    async def test_no_routing_config_allows_all(self, proxy_manager: ProxyManager) -> None:
        """Test that connectors without routing config allow all domains."""
        project = Project(name="No Routing", username="norouting", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="No Route Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        connector = Connector(
            name="No Route Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
            routing_config={},
        )
        await proxy_manager.add_connector(connector)

        proxy = Proxy(
            host="open.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        proxy.status = ProxyStatus.HEALTHY
        await proxy_manager.add_proxy(proxy)

        # Should allow any domain
        healthy = proxy_manager.get_healthy_proxies_for_project(project.id, target_host="anything.com")
        assert len(healthy) == 1

    async def test_mixed_connectors_domain_filtering(self, proxy_manager: ProxyManager) -> None:
        """Test domain filtering with multiple connectors having different rules."""
        project = Project(name="Mixed Domain", username="mixeddomain", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Mixed Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        # Connector 1: whitelist only google.com
        conn1 = Connector(
            name="Google Only",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
            routing_config={"domain_whitelist": ["google.com"]},
        )
        await proxy_manager.add_connector(conn1)

        # Connector 2: no restrictions
        conn2 = Connector(
            name="Open Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
            routing_config={},
        )
        await proxy_manager.add_connector(conn2)

        proxy1 = Proxy(
            host="p1.example.com", port=8080, protocol=ProxyProtocol.HTTP,
            connector_id=conn1.id,
        )
        proxy1.status = ProxyStatus.HEALTHY
        await proxy_manager.add_proxy(proxy1)

        proxy2 = Proxy(
            host="p2.example.com", port=8080, protocol=ProxyProtocol.HTTP,
            connector_id=conn2.id,
        )
        proxy2.status = ProxyStatus.HEALTHY
        await proxy_manager.add_proxy(proxy2)

        # google.com: both connectors should match (conn1 whitelist, conn2 open)
        healthy = proxy_manager.get_healthy_proxies_for_project(project.id, target_host="google.com")
        assert len(healthy) == 2

        # bing.com: only conn2 should match (conn1 restricts to google.com)
        healthy = proxy_manager.get_healthy_proxies_for_project(project.id, target_host="bing.com")
        assert len(healthy) == 1
        assert healthy[0].connector_id == conn2.id

    async def test_select_proxy_with_target_host(self, proxy_manager: ProxyManager) -> None:
        """Test that select_proxy_for_project respects domain filtering."""
        project = Project(name="Select Domain", username="selectdomain", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Select Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        # Connector with whitelist
        connector = Connector(
            name="Select Conn",
            credential_id=credential.id,
            credential_type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
            routing_config={"domain_whitelist": ["allowed.com"]},
        )
        await proxy_manager.add_connector(connector)

        proxy = Proxy(
            host="select.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        proxy.status = ProxyStatus.HEALTHY
        await proxy_manager.add_proxy(proxy)

        # Should return proxy for allowed domain
        result = proxy_manager.select_proxy_for_project(project.id, target_host="allowed.com")
        assert result is not None

        # Should return None for blocked domain
        result = proxy_manager.select_proxy_for_project(project.id, target_host="blocked.com")
        assert result is None

