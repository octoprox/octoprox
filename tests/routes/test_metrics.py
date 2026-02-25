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

