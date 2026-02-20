# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for health check endpoints."""

from starlette.testclient import TestClient


class TestHealthEndpoints:
    """Tests for health check endpoints."""

    def test_health_check(self, async_client: TestClient) -> None:
        """Test the health check endpoint."""
        response = async_client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data
        assert "proxy_count" in data
        assert "healthy_proxy_count" in data

    def test_readiness_check(self, async_client: TestClient) -> None:
        """Test the readiness probe endpoint."""
        response = async_client.get("/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"

    def test_liveness_check(self, async_client: TestClient) -> None:
        """Test the liveness probe endpoint."""
        response = async_client.get("/live")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"

