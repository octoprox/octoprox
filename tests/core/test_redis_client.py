"""Tests for RedisClient."""
from api.db.redis import RedisClient
from api.models.proxy import ProxyStatus


class TestRedisClient:
    """Tests for RedisClient operations."""

    async def test_connect_and_close(self, test_redis_url: str) -> None:
        """Test connecting and closing Redis client."""
        client = RedisClient(test_redis_url)
        await client.connect()
        assert client._client is not None
        await client.close()

    async def test_set_and_get_proxy_status(self, redis_client: RedisClient) -> None:
        """Test setting and getting proxy status."""
        proxy_id = "test-proxy-1"

        await redis_client.set_proxy_status(
            proxy_id=proxy_id,
            status=ProxyStatus.HEALTHY,
            latency_ms=50.5,
            consecutive_failures=0,
        )

        status = await redis_client.get_proxy_status(proxy_id)

        assert status is not None
        assert status["status"] == ProxyStatus.HEALTHY
        assert status["latency_ms"] == 50.5
        assert status["consecutive_failures"] == 0
        assert "updated_at" in status

    async def test_get_proxy_status_not_found(self, redis_client: RedisClient) -> None:
        """Test getting status for non-existent proxy."""
        status = await redis_client.get_proxy_status("non-existent")
        assert status is None

    async def test_get_all_proxy_statuses(self, redis_client: RedisClient) -> None:
        """Test getting all proxy statuses."""
        # Set multiple statuses
        for i in range(3):
            await redis_client.set_proxy_status(
                proxy_id=f"proxy-{i}",
                status=ProxyStatus.HEALTHY,
                latency_ms=float(i * 10),
            )

        statuses = await redis_client.get_all_proxy_statuses()

        assert len(statuses) == 3
        assert "proxy-0" in statuses
        assert "proxy-1" in statuses
        assert "proxy-2" in statuses

    async def test_update_proxy_metrics(self, redis_client: RedisClient) -> None:
        """Test updating proxy metrics."""
        proxy_id = "metrics-proxy"

        # Record some requests with bytes
        await redis_client.update_proxy_metrics(
            proxy_id, success=True, latency_ms=100, bytes_sent=1000, bytes_received=5000
        )
        await redis_client.update_proxy_metrics(
            proxy_id, success=True, latency_ms=200, bytes_sent=2000, bytes_received=10000
        )
        await redis_client.update_proxy_metrics(
            proxy_id, success=False, latency_ms=50, bytes_sent=500, bytes_received=0
        )

        metrics = await redis_client.get_proxy_metrics(proxy_id)

        assert metrics is not None
        assert metrics["request_count"] == 3
        assert metrics["success_count"] == 2
        assert metrics["failure_count"] == 1
        assert metrics["latency_sum_ms"] == 350.0
        # Average should be 350/3 ≈ 116.67
        assert abs(metrics["avg_latency_ms"] - 116.67) < 1
        # Check bytes tracking
        assert metrics["bytes_sent"] == 3500
        assert metrics["bytes_received"] == 15000

    async def test_get_proxy_metrics_not_found(self, redis_client: RedisClient) -> None:
        """Test getting metrics for non-existent proxy."""
        metrics = await redis_client.get_proxy_metrics("non-existent")
        assert metrics is None

    async def test_get_all_proxy_metrics(self, redis_client: RedisClient) -> None:
        """Test getting all proxy metrics."""
        # Record metrics for multiple proxies
        for i in range(3):
            await redis_client.update_proxy_metrics(
                f"metrics-proxy-{i}",
                success=True,
                latency_ms=float(i * 50),
            )

        all_metrics = await redis_client.get_all_proxy_metrics()

        assert len(all_metrics) == 3

    async def test_reset_proxy_metrics(self, redis_client: RedisClient) -> None:
        """Test resetting proxy metrics."""
        proxy_id = "reset-proxy"

        await redis_client.update_proxy_metrics(proxy_id, success=True, latency_ms=100)
        await redis_client.reset_proxy_metrics(proxy_id)

        metrics = await redis_client.get_proxy_metrics(proxy_id)
        assert metrics is None

    async def test_session_operations(self, redis_client: RedisClient) -> None:
        """Test session set, get, and delete operations."""
        session_id = "test-session"
        proxy_id = "session-proxy"

        # Set session
        await redis_client.set_session(session_id, proxy_id, ttl_seconds=60)

        # Get session
        result = await redis_client.get_session(session_id)
        assert result == proxy_id

        # Delete session
        await redis_client.delete_session(session_id)
        result = await redis_client.get_session(session_id)
        assert result is None

    async def test_get_session_not_found(self, redis_client: RedisClient) -> None:
        """Test getting non-existent session."""
        result = await redis_client.get_session("non-existent-session")
        assert result is None

    async def test_proxy_status_unhealthy(self, redis_client: RedisClient) -> None:
        """Test setting unhealthy proxy status."""
        proxy_id = "unhealthy-proxy"

        await redis_client.set_proxy_status(
            proxy_id=proxy_id,
            status=ProxyStatus.UNHEALTHY,
            latency_ms=0,
            consecutive_failures=5,
        )

        status = await redis_client.get_proxy_status(proxy_id)

        assert status is not None
        assert status["status"] == ProxyStatus.UNHEALTHY
        assert status["consecutive_failures"] == 5

    # Project metrics tests

    async def test_update_project_metrics(self, redis_client: RedisClient) -> None:
        """Test updating project-level metrics."""
        project_id = "metrics-project"

        # Record some requests with bytes
        await redis_client.update_project_metrics(
            project_id, success=True, latency_ms=100, bytes_sent=1000, bytes_received=5000
        )
        await redis_client.update_project_metrics(
            project_id, success=True, latency_ms=200, bytes_sent=2000, bytes_received=10000
        )
        await redis_client.update_project_metrics(
            project_id, success=False, latency_ms=50, bytes_sent=500, bytes_received=0
        )

        metrics = await redis_client.get_project_metrics(project_id)

        assert metrics is not None
        assert metrics["request_count"] == 3
        assert metrics["success_count"] == 2
        assert metrics["failure_count"] == 1
        assert metrics["latency_sum_ms"] == 350.0
        # Average should be 350/3 ≈ 116.67
        assert abs(metrics["avg_latency_ms"] - 116.67) < 1
        # Check bytes tracking
        assert metrics["bytes_sent"] == 3500
        assert metrics["bytes_received"] == 15000

    async def test_get_project_metrics_not_found(self, redis_client: RedisClient) -> None:
        """Test getting metrics for non-existent project."""
        metrics = await redis_client.get_project_metrics("non-existent-project")
        assert metrics is None

    async def test_get_all_project_metrics(self, redis_client: RedisClient) -> None:
        """Test getting all project metrics."""
        # Record metrics for multiple projects
        for i in range(3):
            await redis_client.update_project_metrics(
                f"metrics-project-{i}",
                success=True,
                latency_ms=float(i * 50),
            )

        all_metrics = await redis_client.get_all_project_metrics()

        assert len(all_metrics) == 3
        assert "metrics-project-0" in all_metrics
        assert "metrics-project-1" in all_metrics
        assert "metrics-project-2" in all_metrics

    async def test_reset_project_metrics(self, redis_client: RedisClient) -> None:
        """Test resetting project metrics."""
        project_id = "reset-project"

        await redis_client.update_project_metrics(project_id, success=True, latency_ms=100)
        await redis_client.reset_project_metrics(project_id)

        metrics = await redis_client.get_project_metrics(project_id)
        assert metrics is None
