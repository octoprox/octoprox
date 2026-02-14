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

    async def test_update_proxy_stats(self, proxy_manager: ProxyManager) -> None:
        """Test updating proxy statistics."""
        # Create full chain
        project = Project(name="Stats Project", username="statsuser", password="pass")
        await proxy_manager.add_project(project)

        credential = Credential(
            name="Stats Cred",
            type=CredentialType.STATIC_PROXY_PROVIDER,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_credential(credential)

        connector = Connector(
            name="Stats Conn",
            credential_id=credential.id,
            project_id=project.id,
            config={},
        )
        await proxy_manager.add_connector(connector)

        proxy = Proxy(
            host="stats.example.com",
            port=8080,
            protocol=ProxyProtocol.HTTP,
            connector_id=connector.id,
        )
        await proxy_manager.add_proxy(proxy)

        # Update stats with bytes tracking
        await proxy_manager.update_proxy_stats(
            proxy.id, success=True, latency_ms=100, bytes_sent=1000, bytes_received=5000
        )
        await proxy_manager.update_proxy_stats(
            proxy.id, success=True, latency_ms=200, bytes_sent=2000, bytes_received=10000
        )
        await proxy_manager.update_proxy_stats(
            proxy.id, success=False, latency_ms=50, bytes_sent=500, bytes_received=0
        )

        updated_proxy = proxy_manager.get_proxy(proxy.id)
        assert updated_proxy is not None
        assert updated_proxy.request_count == 3
        assert updated_proxy.success_count == 2
        assert updated_proxy.failure_count == 1
        assert updated_proxy.bytes_sent == 3500
        assert updated_proxy.bytes_received == 15000

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

