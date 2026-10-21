# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the traffic config, usage, reset and history on the connector endpoints, and for a blocked connector.

Usage counts the history a metrics flush writes plus the Redis window gateways write, and the block is the one the
traffic job stores, so a test writes the first two and runs the job as a worker would, then reads the api back.
"""

from typing import Any

from starlette.testclient import TestClient

from api.core.roles import JobKind
from api.core.stats import MetricDelta
from api.db.redis import CONNECTOR_METRICS_KEY, RedisClient
from api.db.repository import MetricsRepository
from api.db.session import SessionFactory
from api.jobs.job import JobBatch
from api.jobs.traffic import TrafficJob
from api.models.connector import BYTES_PER_GB
from tests.conftest import api_runtime, on_app_loop, queued_jobs


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
        # A new connector waits for no decision.
        assert usage["pending"] is False
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

    def test_history_shows_the_flushed_windows(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        flush_connector_window(authenticated_client, created_connector["id"], 60, 40)

        url = f"/api/v1/projects/{created_project['id']}/connectors/{created_connector['id']}/metrics/history"
        snapshots = authenticated_client.get(url, params={"range": "1h"}).json()["snapshots"]

        assert [(s["bytes_sent"], s["bytes_received"]) for s in snapshots] == [(60, 40)]


class TestBlockedConnector:
    """A connector over its limit: the block the traffic job stored, as the api shows and previews it, and a reset."""

    def test_the_block_the_traffic_job_stored_is_shown_and_previewed(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any],
    ) -> None:
        connector = _create(
            authenticated_client, created_project["id"], created_credential["id"], "Capped",
            {"limit_bytes": 100, "action": "block"},
        )
        flush_connector_window(authenticated_client, connector["id"], 60, 60)
        url = f"/api/v1/projects/{created_project['id']}/connectors/{connector['id']}"

        # Over the limit, and taking requests until a worker decides.
        usage = authenticated_client.get(url).json()["traffic_usage"]
        assert (usage["total_bytes"], usage["percent"], usage["status"]) == (120, 120, "exceeded")
        assert usage["blocked"] is False
        assert previews_blocked(authenticated_client, created_project["id"], connector["id"]) is False

        decide_traffic(authenticated_client, connector["id"])

        usage = authenticated_client.get(url).json()["traffic_usage"]
        assert (usage["total_bytes"], usage["percent"], usage["status"]) == (120, 120, "exceeded")
        # Blocked by the job's own decision: no request waits on it.
        assert (usage["blocked"], usage["pending"]) == (True, False)
        assert previews_blocked(authenticated_client, created_project["id"], connector["id"]) is True

    def test_a_reset_answer_shows_the_stored_block_pending_until_a_worker_decides(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any],
    ) -> None:
        connector = _blocked_connector(authenticated_client, created_project, created_credential)
        url = f"/api/v1/projects/{created_project['id']}/connectors/{connector['id']}"

        response = authenticated_client.post(f"{url}/traffic/reset")

        assert response.status_code == 200
        data = response.json()
        usage = data["traffic_usage"]
        # Usage counts from the reset at once; the block is the one gateways still enforce, and a decision is pending.
        assert (usage["total_bytes"], usage["status"], usage["blocked"], usage["pending"]) == (0, "ok", True, True)
        assert usage["reset_at"] == data["traffic_reset_at"] is not None
        assert authenticated_client.get(url).json()["traffic_usage"]["pending"] is True
        assert previews_blocked(authenticated_client, created_project["id"], connector["id"]) is True
        # The reset asked a worker for the decision that lifts it.
        assert queued_jobs(authenticated_client, JobKind.TRAFFIC) == {connector["id"]}

        decide_traffic(authenticated_client, connector["id"])

        usage = authenticated_client.get(url).json()["traffic_usage"]
        assert (usage["blocked"], usage["pending"]) == (False, False)
        assert previews_blocked(authenticated_client, created_project["id"], connector["id"]) is False

    def test_a_raised_limits_answer_shows_the_stored_block_pending_until_a_worker_decides(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any],
    ) -> None:
        connector = _blocked_connector(authenticated_client, created_project, created_credential)
        url = f"/api/v1/projects/{created_project['id']}/connectors/{connector['id']}"

        response = authenticated_client.patch(url, json={"traffic_config": {"limit_bytes": 1000, "action": "block"}})

        assert response.status_code == 200
        usage = response.json()["traffic_usage"]
        assert (usage["limit_bytes"], usage["percent"], usage["status"], usage["blocked"]) == (1000, 12, "ok", True)
        assert usage["pending"] is True
        assert queued_jobs(authenticated_client, JobKind.TRAFFIC) == {connector["id"]}
        # Another edit asks for nothing new, and the connector still waits.
        renamed = authenticated_client.patch(url, json={"name": "Renamed"}).json()["traffic_usage"]
        assert (renamed["blocked"], renamed["pending"]) == (True, True)

        decide_traffic(authenticated_client, connector["id"])

        usage = authenticated_client.get(url).json()["traffic_usage"]
        assert (usage["blocked"], usage["pending"]) == (False, False)
        assert previews_blocked(authenticated_client, created_project["id"], connector["id"]) is False

    def test_bytes_not_flushed_yet_count_too(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any],
    ) -> None:
        """The connector's Redis window (what gateways wrote and no flush has taken) counts with the history."""
        connector = _create(
            authenticated_client, created_project["id"], created_credential["id"], "Capped",
            {"limit_bytes": 100, "action": "block"},
        )
        flush_connector_window(authenticated_client, connector["id"], 30, 30)
        window = MetricDelta(request_count=1, success_count=1, bytes_sent=20, bytes_received=20)
        on_app_loop(authenticated_client, _write_window, api_runtime(authenticated_client).redis_client, connector["id"], window)

        usage = authenticated_client.get(
            f"/api/v1/projects/{created_project['id']}/connectors/{connector['id']}"
        ).json()["traffic_usage"]

        assert (usage["total_bytes"], usage["status"], usage["blocked"]) == (100, "exceeded", False)

    def test_an_alert_only_limit_blocks_nothing(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any],
    ) -> None:
        connector = _create(
            authenticated_client, created_project["id"], created_credential["id"], "Watched",
            {"limit_bytes": 100, "action": "alert"},
        )
        flush_connector_window(authenticated_client, connector["id"], 60, 60)
        decide_traffic(authenticated_client, connector["id"])

        usage = authenticated_client.get(
            f"/api/v1/projects/{created_project['id']}/connectors/{connector['id']}"
        ).json()["traffic_usage"]

        assert (usage["status"], usage["blocked"]) == ("exceeded", False)
        assert previews_blocked(authenticated_client, created_project["id"], connector["id"]) is False


def _blocked_connector(client: TestClient, project: dict[str, Any], credential: dict[str, Any]) -> dict[str, Any]:
    """A connector with a 100-byte blocking limit, 120 bytes this period, and the block a worker decided for it."""
    connector = _create(client, project["id"], credential["id"], "Capped", {"limit_bytes": 100, "action": "block"})
    flush_connector_window(client, connector["id"], 60, 60)
    decide_traffic(client, connector["id"])
    return connector


def flush_connector_window(client: TestClient, connector_id: str, sent: int, received: int) -> None:
    """One flushed window of the connector's traffic, stamped now, as a worker's metrics flush writes it."""
    on_app_loop(client, _save_history, api_runtime(client).session_factory, connector_id, sent, received)


def decide_traffic(client: TestClient, connector_id: str) -> None:
    """The traffic decision a worker makes for the connector from what the app's Postgres and Redis hold now."""
    runtime = api_runtime(client)
    job = TrafficJob(runtime.session_factory, runtime.redis_client)
    on_app_loop(client, job.run, JobBatch(ids=("1-0",), keys=(connector_id,)))


def previews_blocked(client: TestClient, project_id: str, connector_id: str) -> bool:
    """Whether the api's routing preview of the project holds the connector back by its traffic limit."""
    router = on_app_loop(client, api_runtime(client).preview_router, project_id)
    return router.is_traffic_blocked(connector_id)


async def _save_history(session_factory: SessionFactory, connector_id: str, sent: int, received: int) -> None:
    delta = MetricDelta(request_count=1, success_count=1, bytes_sent=sent, bytes_received=received)
    async with session_factory() as session:
        await MetricsRepository(session).save_connector_metrics_snapshots({connector_id: delta})
        await session.commit()


async def _write_window(redis_client: RedisClient, connector_id: str, delta: MetricDelta) -> None:
    """The connector's metrics window as gateways leave it in Redis, not flushed yet."""
    await redis_client.client.hset(CONNECTOR_METRICS_KEY.format(connector_id=connector_id), mapping=delta.to_dict())
