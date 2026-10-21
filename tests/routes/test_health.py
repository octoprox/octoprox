# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The ops endpoints of an api process: liveness, readiness, health and metrics.

What every role answers is in ``tests/roles/test_app.py``; here the api's,
with its readiness driven by hand to see the 503 shapes.
"""

import pytest
from starlette.testclient import TestClient

from api import __version__
from tests.conftest import api_runtime


class TestHealthEndpoints:
    """Tests for health check endpoints."""

    def test_liveness_check(self, async_client: TestClient) -> None:
        response = async_client.get("/live")

        assert response.status_code == 200
        assert response.json() == {"status": "alive"}

    def test_readiness_check(self, async_client: TestClient) -> None:
        response = async_client.get("/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "ready", "role": "api", "reasons": []}

    def test_health_check_names_the_process(self, async_client: TestClient) -> None:
        """The proxy counts are gone: an api process holds no proxies; the body names the process instead."""
        response = async_client.get("/health")

        assert response.status_code == 200
        node = async_client.app.state.node  # type: ignore[attr-defined]
        assert response.json() == {
            "status": "healthy",
            "role": "api",
            "instance": node.instance_id,
            "version": __version__,
        }

    def test_unready_runtime_is_a_503_with_its_reason(
        self, async_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api_runtime(async_client), "readiness", lambda: "api not started")

        ready = async_client.get("/ready")
        assert ready.status_code == 503
        assert ready.json() == {"status": "unready", "role": "api", "reasons": ["api: api not started"]}
        health = async_client.get("/health")
        assert health.status_code == 503
        assert health.json()["status"] == "unhealthy"
        assert health.json()["reasons"] == ["api: api not started"]
        assert "octoprox_ready{role=\"api\"} 0.0" in async_client.get("/metrics").text

    def test_metrics_are_prometheus_text(self, async_client: TestClient) -> None:
        response = async_client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
        text = response.text
        node = async_client.app.state.node  # type: ignore[attr-defined]
        # Labels come out sorted by name.
        assert (
            f'octoprox_build_info{{instance_id="{node.instance_id}",role="api",version="{__version__}"}} 1.0' in text
        )
        assert 'octoprox_ready{role="api"} 1.0' in text
        assert 'octoprox_uptime_seconds{role="api"}' in text

    def test_probes_need_no_sign_in(self, async_client: TestClient) -> None:
        for path in ("/live", "/ready", "/health", "/metrics"):
            assert async_client.get(path).status_code == 200, path
