# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the traffic config, usage, reset and history on the connector endpoints,
and for how a blocked connector shows up in the traffic split, Prometheus and selection."""

from typing import Any

from starlette.testclient import TestClient

from api.models.connector import BYTES_PER_GB


def _create(client: TestClient, project_id: str, credential_id: str, name: str, traffic: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/projects/{project_id}/connectors",
        json={"name": name, "credential_id": credential_id, "config": {}, "traffic_config": traffic},
    )
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


class TestTrafficConfigEndpoints:
    def test_create_with_traffic_config_stores_choices_and_reports_usage(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any],
    ) -> None:
        data = _create(
            authenticated_client, created_project["id"], created_credential["id"], "Metered",
            {"limit_bytes": 5 * BYTES_PER_GB, "action": "block", "price_per_gb": 3.0, "period": "day"},
        )
        assert data["traffic_config"] == {
            "limit_bytes": 5 * BYTES_PER_GB, "action": "block", "price_per_gb": 3.0, "period": "day",
        }
        usage = data["traffic_usage"]
        assert usage["period"] == "day"
        assert usage["total_bytes"] == 0
        assert usage["limit_bytes"] == 5 * BYTES_PER_GB
        assert usage["percent"] == 0
        assert usage["status"] == "ok"
        assert usage["blocked"] is False
        assert usage["cost"] == 0
        assert usage["currency"] == "USD"
        assert usage["action"] == "block"

    def test_defaults_give_usage_without_a_limit(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        response = authenticated_client.get(
            f"/api/v1/projects/{created_project['id']}/connectors/{created_connector['id']}"
        )
        assert response.status_code == 200
        usage = response.json()["traffic_usage"]
        assert usage["period"] == "month"
        assert usage["limit_bytes"] is None
        assert usage["percent"] is None
        assert usage["cost"] is None

    def test_invalid_traffic_config_is_rejected(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any],
    ) -> None:
        response = authenticated_client.post(
            f"/api/v1/projects/{created_project['id']}/connectors",
            json={"name": "Bad", "credential_id": created_credential["id"], "config": {},
                  "traffic_config": {"limit_bytes": 10, "limit_status": 418}},
        )
        assert response.status_code == 422
        assert "limit_status" in response.json()["detail"]

    def test_update_traffic_config(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        url = f"/api/v1/projects/{created_project['id']}/connectors/{created_connector['id']}"
        response = authenticated_client.patch(url, json={"traffic_config": {"limit_bytes": 100, "warn_percent": 50}})
        assert response.status_code == 200
        assert response.json()["traffic_config"] == {"limit_bytes": 100, "warn_percent": 50}
        assert response.json()["traffic_usage"]["limit_bytes"] == 100
        # Back to the defaults clears it.
        response = authenticated_client.patch(url, json={"traffic_config": {}})
        assert response.status_code == 200
        assert response.json()["traffic_config"] == {}

    def test_viewer_cannot_reset(self, viewer_client: TestClient) -> None:
        response = viewer_client.post("/api/v1/projects/some-project/connectors/some-connector/traffic/reset")
        assert response.status_code == 403

    def test_reset_unknown_connector(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        response = authenticated_client.post(f"/api/v1/projects/{created_project['id']}/connectors/nope/traffic/reset")
        assert response.status_code == 404

    def test_history_endpoint(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        url = f"/api/v1/projects/{created_project['id']}/connectors/{created_connector['id']}/metrics/history"
        response = authenticated_client.get(url, params={"range": "7d"})
        assert response.status_code == 200
        assert response.json() == {"snapshots": []}
        assert authenticated_client.get(url, params={"range": "2h"}).status_code == 422
        response = authenticated_client.get(
            f"/api/v1/projects/{created_project['id']}/connectors/nope/metrics/history"
        )
        assert response.status_code == 404


class TestBlockedConnector:
    """Drive the limiter through the live manager and read the consequences over the API."""

    async def _block(self, client: TestClient, connector_id: str) -> None:
        manager = client.app.state.proxy_manager
        limiter = manager.traffic_limiter
        assert limiter.record(connector_id, 60, 60) is True
        await limiter.evaluate(connector_id)
        assert limiter.is_blocked(connector_id) is True

    def test_usage_reports_the_block_and_reset_lifts_it(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any],
    ) -> None:
        import asyncio

        connector = _create(
            authenticated_client, created_project["id"], created_credential["id"], "Capped",
            {"limit_bytes": 100, "action": "block"},
        )
        manager = authenticated_client.app.state.proxy_manager
        asyncio.run_coroutine_threadsafe(self._block(authenticated_client, connector["id"]), manager_loop(manager)).result(10)

        url = f"/api/v1/projects/{created_project['id']}/connectors/{connector['id']}"
        usage = authenticated_client.get(url).json()["traffic_usage"]
        assert usage["blocked"] is True
        assert usage["status"] == "exceeded"
        assert usage["total_bytes"] == 120
        assert usage["percent"] == 120

        # Selection leaves the connector out; the request path reports a limit.
        assert manager.is_traffic_blocked(connector["id"]) is True
        assert manager._enabled_connectors(created_project["id"]) == []

        response = authenticated_client.post(f"{url}/traffic/reset")
        assert response.status_code == 200
        data = response.json()
        assert data["traffic_reset_at"] is not None
        assert data["traffic_usage"]["blocked"] is False
        assert data["traffic_usage"]["total_bytes"] == 0
        assert data["traffic_usage"]["reset_at"] == data["traffic_reset_at"]
        assert manager.is_traffic_blocked(connector["id"]) is False

    def test_traffic_split_and_prometheus_show_the_block(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any],
    ) -> None:
        import asyncio

        connector = _create(
            authenticated_client, created_project["id"], created_credential["id"], "Capped",
            {"limit_bytes": 100, "action": "block", "price_per_gb": 1.0},
        )
        manager = authenticated_client.app.state.proxy_manager
        asyncio.run_coroutine_threadsafe(self._block(authenticated_client, connector["id"]), manager_loop(manager)).result(10)

        split = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/traffic-split").json()
        share = split["connectors"][0]
        assert share["excluded_reason"] == "traffic_limit"
        assert share["observed_bytes"] == 0
        assert share["cost"] == 0
        assert share["currency"] == "USD"

        # The endpoint returns a str, which FastAPI JSON-encodes; decode it back to the exposition text.
        text = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/metrics/prometheus").json()
        labels = f'{{project="{created_project["id"]}",connector="{connector["id"]}",name="Capped"}}'
        assert f"octoprox_connector_traffic_bytes{labels} 120" in text
        assert f"octoprox_connector_traffic_limit_bytes{labels} 100" in text
        assert 'currency="USD"} 0' in text
        assert f"octoprox_connector_traffic_blocked{labels} 1" in text


def manager_loop(manager: Any) -> Any:
    """The event loop the app's manager runs on (the TestClient's portal thread)."""
    return manager._tasks[0].get_loop()
