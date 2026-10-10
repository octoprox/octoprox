# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for metrics endpoints."""

from typing import Any

from starlette.testclient import TestClient


class TestMetricsEndpoints:
    """Tests for metrics endpoints."""

    def test_get_metrics(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test getting metrics for a project."""
        project_id = created_project["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics")

        assert response.status_code == 200
        data = response.json()

        # Check pool metrics
        assert "pool" in data
        pool = data["pool"]
        assert "total_proxies" in pool
        assert "healthy_proxies" in pool
        assert "unhealthy_proxies" in pool
        assert "total_requests" in pool
        assert "total_successes" in pool
        assert "total_failures" in pool
        assert "overall_success_rate" in pool
        assert "avg_latency_ms" in pool
        assert "total_bytes_sent" in pool
        assert "total_bytes_received" in pool

        # Check strategy metrics
        assert "strategy" in data
        strategy = data["strategy"]
        assert "current_strategy" in strategy
        assert "available_strategies" in strategy
        assert isinstance(strategy["available_strategies"], list)

    def test_get_metrics_project_not_found(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test getting metrics for non-existent project."""
        response = authenticated_client.get("/api/v1/projects/non-existent/metrics")

        assert response.status_code == 404

    def test_get_metrics_with_proxies(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_proxy: dict[str, Any],
    ) -> None:
        """Test getting metrics when proxies exist."""
        project_id = created_project["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics")

        assert response.status_code == 200
        data = response.json()
        assert data["pool"]["total_proxies"] >= 1

    def test_prometheus_metrics(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test getting Prometheus format metrics."""
        project_id = created_project["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics/prometheus")

        assert response.status_code == 200
        # Prometheus metrics are returned as text
        text = response.text
        assert "octoprox_proxies_total" in text
        assert "octoprox_proxies_healthy" in text
        assert "octoprox_requests_total" in text
        assert "octoprox_bytes_sent_total" in text
        assert "octoprox_bytes_received_total" in text

    def test_prometheus_metrics_project_not_found(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test getting Prometheus metrics for non-existent project."""
        response = authenticated_client.get("/api/v1/projects/non-existent/metrics/prometheus")

        assert response.status_code == 404



class TestTrafficSplit:
    """GET /metrics/traffic-split."""

    def test_project_not_found(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.get("/api/v1/projects/nope/metrics/traffic-split")
        assert response.status_code == 404

    def test_empty_project(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        response = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/traffic-split")
        assert response.status_code == 200
        data = response.json()
        assert data["connectors"] == []
        assert data["total_weight"] == 0
        assert data["observed_requests"] == 0
        assert data["range"] == "1h"

    def test_connector_without_proxies_is_excluded(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        response = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/traffic-split", params={"range": "24h"})
        assert response.status_code == 200
        data = response.json()
        assert data["range"] == "24h"
        assert len(data["connectors"]) == 1
        share = data["connectors"][0]
        assert share["connector_id"] == created_connector["id"]
        assert share["weight"] == 1
        assert share["eligible_proxies"] == 0
        assert share["expected_share"] == 0
        assert share["excluded_reason"] == "no_eligible_proxies"
        assert share["observed_share"] is None

    def test_rejects_unknown_range(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        response = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/traffic-split", params={"range": "2h"})
        assert response.status_code == 422


class TestHostMetricsEndpoints:
    """The Hosts page: where a project's traffic went, by destination and connector."""

    @staticmethod
    def _seed(client: TestClient, project_id: str, connector_id: str) -> None:
        """Write a few host history rows through the app's own session, as the flusher would."""
        import asyncio
        from datetime import timedelta

        from api.core import utc_now
        from api.core.stats import MetricDelta
        from api.db.models import HostMetricsModel
        from api.db.repository import MetricsRepository

        manager = client.app.state.proxy_manager

        async def seed() -> None:
            async with manager._session_factory() as session:
                repo = MetricsRepository(session)
                dropped = await repo.save_host_metrics_snapshots({
                    (project_id, connector_id, "shop.example.com"): MetricDelta(
                        request_count=6, success_count=5, failure_count=1, latency_sum_ms=600, bytes_sent=100, bytes_received=900,
                    ),
                    (project_id, connector_id, "news.example.org"): MetricDelta(
                        request_count=2, success_count=2, latency_sum_ms=100, bytes_sent=50, bytes_received=50,
                    ),
                    (project_id, "no-such-connector", "lost.example.net"): MetricDelta(request_count=1, success_count=1),
                })
                assert dropped == [(project_id, "no-such-connector", "lost.example.net")]
                # Outside every range but 30d.
                session.add(HostMetricsModel(
                    project_id=project_id, connector_id=connector_id, host="old.example.net",
                    timestamp=utc_now() - timedelta(days=10), request_count=40, success_count=40,
                    failure_count=0, avg_latency_ms=10.0, bytes_sent=1, bytes_received=1, granularity=3600,
                ))
                await session.commit()

        from tests.routes.test_connector_traffic import manager_loop

        asyncio.run_coroutine_threadsafe(seed(), manager_loop(manager)).result(10)

    def test_empty_project(
        self, authenticated_client: TestClient, created_project: dict[str, Any],
    ) -> None:
        response = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/hosts")
        assert response.status_code == 200
        data = response.json()
        assert data["hosts"] == [] and data["connectors"] == []
        assert data["totals"]["host_count"] == 0 and data["totals"]["request_count"] == 0
        assert data["enabled"] is True
        assert data["range"] == "24h"
        assert data["overflow_host"] == "(other)"
        history = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/hosts/history").json()
        assert history["series"] == [] and history["bucket_seconds"] == 900

    def test_hosts_with_totals_shares_and_connector_split(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        project_id, connector_id = created_project["id"], created_connector["id"]
        self._seed(authenticated_client, project_id, connector_id)

        response = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics/hosts", params={"range": "1h"})
        assert response.status_code == 200
        data = response.json()
        assert [h["host"] for h in data["hosts"]] == ["shop.example.com", "news.example.org"]
        shop = data["hosts"][0]
        assert shop["request_count"] == 6 and shop["failure_count"] == 1
        assert shop["avg_latency_ms"] == 100.0
        assert shop["request_share"] == 75.0
        assert shop["bytes_share"] == round(1000 / 1100 * 100, 2)
        assert shop["connectors"] == [{
            "connector_id": connector_id, "name": created_connector["name"],
            "credential_type": "static_proxy_provider", "request_count": 6, "success_count": 5,
            "failure_count": 1, "avg_latency_ms": 100.0, "bytes_sent": 100, "bytes_received": 900,
        }]
        assert data["totals"] == {
            "host_count": 2, "request_count": 8, "success_count": 7, "failure_count": 1,
            "avg_latency_ms": 87.5, "bytes_sent": 150, "bytes_received": 950,
        }
        assert [c["connector_id"] for c in data["connectors"]] == [connector_id]

        # The older row only shows up in the widest range; search and limit narrow the list.
        wide = authenticated_client.get(f"/api/v1/projects/{project_id}/metrics/hosts", params={"range": "30d"}).json()
        assert wide["totals"]["host_count"] == 3 and wide["hosts"][0]["host"] == "old.example.net"
        narrowed = authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts", params={"range": "30d", "search": "EXAMPLE.ORG"},
        ).json()
        assert [h["host"] for h in narrowed["hosts"]] == ["news.example.org"]
        assert narrowed["totals"]["host_count"] == 1
        limited = authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts", params={"range": "30d", "limit": 1},
        ).json()
        assert len(limited["hosts"]) == 1 and limited["totals"]["host_count"] == 3 and limited["limit"] == 1
        by_connector = authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts", params={"connector_id": connector_id},
        ).json()
        assert by_connector["totals"]["request_count"] == 8

    def test_history_series_and_overflow(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        project_id, connector_id = created_project["id"], created_connector["id"]
        self._seed(authenticated_client, project_id, connector_id)

        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts/history", params={"range": "1h", "top": 1},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["bucket_seconds"] == 60
        assert [s["host"] for s in data["series"]] == ["shop.example.com", "(other)"]
        shop, other = data["series"]
        assert len(shop["points"]) == len(other["points"]) == 1
        assert shop["points"][0]["request_count"] == 6
        assert other["points"][0]["request_count"] == 2
        assert other["points"][0]["bytes_received"] == 50

        # Named hosts get a series each, even a quiet one, so a chart's legend is stable.
        named = authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts/history",
            params={"range": "1h", "hosts": "news.example.org, quiet.example.com"},
        ).json()
        assert [s["host"] for s in named["series"]] == ["news.example.org", "quiet.example.com", "(other)"]
        assert named["series"][1]["points"][0]["request_count"] == 0
        assert named["series"][2]["points"][0]["request_count"] == 6

    def test_errors(
        self, authenticated_client: TestClient, created_project: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        assert authenticated_client.get("/api/v1/projects/nope/metrics/hosts").status_code == 404
        assert authenticated_client.get("/api/v1/projects/nope/metrics/hosts/history").status_code == 404
        assert authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts", params={"connector_id": "nope"},
        ).status_code == 404
        assert authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts/history", params={"connector_id": "nope"},
        ).status_code == 404
        assert authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts", params={"range": "2h"},
        ).status_code == 422
        assert authenticated_client.get(
            f"/api/v1/projects/{project_id}/metrics/hosts", params={"limit": 0},
        ).status_code == 422

    def test_viewer_can_read(self, viewer_client: TestClient) -> None:
        # Read-only, so a viewer gets past the role check (and then a 404 for a project that is not there).
        assert viewer_client.get("/api/v1/projects/nope/metrics/hosts").status_code == 404
        assert viewer_client.get("/api/v1/projects/nope/metrics/hosts/history").status_code == 404
