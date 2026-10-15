# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for RedisClient."""
from api.core.stats import MetricDelta, TunnelPeerMetricDelta
from api.db.redis import HOST_METRICS_KEY, PROJECT_METRICS_KEY, PROXY_METRICS_KEY, RedisClient
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

    async def test_exit_ip_is_written_only_into_an_existing_status_hash(self, redis_client: RedisClient) -> None:
        """The observation flusher annotates the hash the health checker owns; it never creates one."""
        await redis_client.delete_proxy_status("no-hash")
        await redis_client.set_proxy_status("has-hash", ProxyStatus.HEALTHY)
        try:
            assert await redis_client.set_proxy_exit_ips({"no-hash": "203.0.113.9", "has-hash": "203.0.113.10"}) == 1
            assert not await redis_client.client.exists("proxy:status:no-hash")
            assert await redis_client.get_proxy_exit_ips(["has-hash", "no-hash"]) == {"has-hash": "203.0.113.10", "no-hash": None}
            status = await redis_client.get_proxy_status("has-hash")
            assert status is not None and status["status"] == ProxyStatus.HEALTHY
        finally:
            await redis_client.delete_proxy_status("has-hash")

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
        assert metrics.request_count == 3
        assert metrics.success_count == 2
        assert metrics.failure_count == 1
        assert metrics.latency_sum_ms == 350.0
        # Average should be 350/3 ≈ 116.67
        assert abs(metrics.avg_latency_ms - 116.67) < 1
        # Check bytes tracking
        assert metrics.bytes_sent == 3500
        assert metrics.bytes_received == 15000

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

    async def test_many_hashes_among_unrelated_keys(self, redis_client: RedisClient) -> None:
        """More hashes than one read pipeline holds, scattered through a keyspace of other keys."""
        pipe = redis_client.client.pipeline(transaction=False)
        for i in range(2000):
            pipe.set(f"sticky:project:session-{i}", "proxy")
        await pipe.execute()
        proxies = {f"many-proxy-{i}": MetricDelta(request_count=i + 1) for i in range(1200)}
        await redis_client.flush_metric_deltas(proxies, {})
        for i in range(1200):
            await redis_client.set_proxy_status(f"many-proxy-{i}", ProxyStatus.HEALTHY, latency_ms=float(i))
        # A hash holding only a sighting carries no status and is left out.
        await redis_client.client.hset("proxy:status:sighting-only", "exit_ip", "203.0.113.7")

        metrics = await redis_client.get_all_proxy_metrics()
        statuses = await redis_client.get_all_proxy_statuses()

        assert metrics == proxies
        assert len(statuses) == 1200
        assert statuses["many-proxy-7"]["status"] == ProxyStatus.HEALTHY
        assert statuses["many-proxy-7"]["latency_ms"] == 7.0

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

    async def test_delete_proxy_status(self, redis_client: RedisClient) -> None:
        """Test deleting proxy status."""
        proxy_id = "delete-status-proxy"

        # Set status first
        await redis_client.set_proxy_status(
            proxy_id=proxy_id,
            status=ProxyStatus.HEALTHY,
            latency_ms=50.0,
        )

        # Verify it exists
        status = await redis_client.get_proxy_status(proxy_id)
        assert status is not None

        # Delete it
        await redis_client.delete_proxy_status(proxy_id)

        # Verify it's gone
        status = await redis_client.get_proxy_status(proxy_id)
        assert status is None

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
        assert metrics.request_count == 3
        assert metrics.success_count == 2
        assert metrics.failure_count == 1
        assert metrics.latency_sum_ms == 350.0
        # Average should be 350/3 ≈ 116.67
        assert abs(metrics.avg_latency_ms - 116.67) < 1
        # Check bytes tracking
        assert metrics.bytes_sent == 3500
        assert metrics.bytes_received == 15000

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


def _requests(n: int, latency_ms: float = 100.0, sent: int = 10) -> MetricDelta:
    delta = MetricDelta()
    for _ in range(n):
        delta.add_request(True, latency_ms, sent, 2 * sent)
    return delta


async def _drain(redis_client: RedisClient, **flushed: dict) -> None:  # type: ignore[type-arg]
    lists = {"proxies": {}, "projects": {}, "connectors": {}, "tunnel_peers": {}, "hosts": {}, "failures": {}}
    lists.update(flushed)
    await redis_client.drain_flushed_metrics(**lists)  # type: ignore[arg-type]


class TestDrainFlushedMetrics:
    """The flusher takes what it wrote off the hashes; what arrived since its read stays."""

    async def test_what_arrived_after_the_read_stays_for_the_next_flush(self, redis_client: RedisClient) -> None:
        await redis_client.flush_metric_deltas({"p1": _requests(3)}, {"proj": _requests(3)})
        read_proxies = await redis_client.get_all_proxy_metrics()
        read_projects = await redis_client.get_all_project_metrics()
        # Another instance publishes while the flush transaction runs.
        await redis_client.flush_metric_deltas({"p1": _requests(2, latency_ms=50)}, {"proj": _requests(2)})

        await _drain(redis_client, proxies=read_proxies, projects=read_projects)

        left = await redis_client.get_proxy_metrics("p1")
        assert left is not None
        assert (left.request_count, left.success_count, left.failure_count) == (2, 2, 0)
        assert (left.bytes_sent, left.bytes_received) == (20, 40)
        assert abs(left.latency_sum_ms - 100.0) < 1e-6
        project_left = await redis_client.get_project_metrics("proj")
        assert project_left is not None and project_left.request_count == 2

    async def test_a_fully_drained_hash_is_deleted_despite_float_rounding(self, redis_client: RedisClient) -> None:
        delta = MetricDelta()
        for latency in (0.1, 0.2, 0.7, 1e-9):
            delta.add_request(False, latency)
        await redis_client.flush_metric_deltas({"p1": delta}, {})
        read = await redis_client.get_all_proxy_metrics()

        await _drain(redis_client, proxies=read)

        assert not await redis_client.client.exists(PROXY_METRICS_KEY.format(proxy_id="p1"))

    async def test_a_hash_deleted_meanwhile_stays_gone(self, redis_client: RedisClient) -> None:
        await redis_client.flush_metric_deltas({}, {"proj": _requests(3)})
        read = await redis_client.get_all_project_metrics()
        await redis_client.reset_project_metrics("proj")  # the project was deleted

        await _drain(redis_client, projects=read)

        assert not await redis_client.client.exists(PROJECT_METRICS_KEY.format(project_id="proj"))

    async def test_a_hash_reset_and_refilled_meanwhile_is_not_left_below_zero(self, redis_client: RedisClient) -> None:
        await redis_client.flush_metric_deltas({"p1": _requests(5)}, {})
        read = await redis_client.get_all_proxy_metrics()
        await redis_client.reset_proxy_metrics("p1")
        await redis_client.flush_metric_deltas({"p1": _requests(1)}, {})  # a request still in flight

        await _drain(redis_client, proxies=read)

        assert not await redis_client.client.exists(PROXY_METRICS_KEY.format(proxy_id="p1"))

    async def test_tunnel_device_counters_are_drained_too(self, redis_client: RedisClient) -> None:
        device = TunnelPeerMetricDelta()
        device.add_request(True, 10.0)
        device.by_address = 4
        await redis_client.flush_metric_deltas({}, {}, tunnel_peer_deltas={"dev": device})
        read = await redis_client.get_all_tunnel_peer_metrics()
        later = TunnelPeerMetricDelta()
        later.encrypted_dns_blocked = 1
        await redis_client.flush_metric_deltas({}, {}, tunnel_peer_deltas={"dev": later})

        await _drain(redis_client, tunnel_peers=read)

        left = await redis_client.get_tunnel_peer_metrics("dev")
        assert left is not None
        assert (left.request_count, left.by_address, left.encrypted_dns_blocked) == (0, 0, 1)

    async def test_many_host_hashes_in_one_drain(self, redis_client: RedisClient) -> None:
        hosts = {("proj", "conn", f"host{i}.example.com"): _requests(1) for i in range(1200)}
        await redis_client.flush_metric_deltas({}, {}, host_deltas=hosts)
        read = await redis_client.get_all_host_metrics()
        assert len(read) == 1200
        await redis_client.flush_metric_deltas({}, {}, host_deltas={("proj", "conn", "host7.example.com"): _requests(1)})

        await _drain(redis_client, hosts=read)

        assert await redis_client.get_all_host_metrics() == {("proj", "conn", "host7.example.com"): _requests(1)}
        assert not await redis_client.client.exists(
            HOST_METRICS_KEY.format(project_id="proj", connector_id="conn", host="host8.example.com")
        )
