# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the project failure log endpoints, on an api process: examples, the head poll, the counted summary.

The examples are written to Redis as a gateway's failure log writes them,
and the counts to Postgres as a worker's metrics flush writes them.
"""

from datetime import timedelta
from typing import Any

from starlette.testclient import TestClient

from api.core import utc_now
from api.db.models import FailureMetricsModel
from api.db.redis import RedisClient
from api.db.repository import MetricsRepository
from api.db.session import SessionFactory
from tests.conftest import api_runtime, on_app_loop


def _entry(i: int = 0, **overrides: str) -> dict[str, str]:
    fields = {
        "kind": "upstream_rejected",
        "status": "502",
        "message": "407 Proxy Authentication Required",
        "error_type": "ProxyError",
        "method": "CONNECT",
        "target": f"host{i}.example:443",
        "proxy_id": "proxy-1",
        "connector_id": "connector-1",
        "connector_name": "Bright Data",
        "proxy": "brd.superproxy.io:33335",
        "upstream_status": "407",
        "latency_ms": "41.5",
        "count": "3",
        "first_at": f"2026-10-15T10:00:{i:02d}",
        "last_at": f"2026-10-15T10:00:{i:02d}.5",
    }
    fields.update(overrides)
    return fields


async def _append(redis_client: RedisClient, project_id: str, entries: list[dict[str, str]]) -> None:
    """Examples as one window of the failure log writes them, oldest first."""
    parts = redis_client.failures_batch(
        [(project_id, e["kind"], e) for e in entries], {}, max_entries=1000, retention_seconds=3600
    )
    await redis_client.send_batch(parts)


def _seed_counts(client: TestClient, project_id: str, connector_id: str) -> None:
    """Write failure history rows through the app's own session, as a worker's metrics flush would."""
    on_app_loop(client, _save_counts, api_runtime(client).session_factory, project_id, connector_id)


async def _save_counts(session_factory: SessionFactory, project_id: str, connector_id: str) -> None:
    async with session_factory() as session:
        repo = MetricsRepository(session)
        dropped = await repo.save_failure_metrics_snapshots({
            (project_id, connector_id, "upstream_rejected", 407): 30,
            (project_id, connector_id, "upstream_rejected", 502): 5,
            (project_id, connector_id, "upstream_timeout", None): 12,
            (project_id, None, "no_proxy_available", None): 4,
            (project_id, "no-such-connector", "upstream_timeout", None): 99,
            ("no-such-project", None, "no_proxy_available", None): 99,
        })
        assert sorted(dropped, key=str) == sorted([
            (project_id, "no-such-connector", "upstream_timeout", None),
            ("no-such-project", None, "no_proxy_available", None),
        ], key=str)
        # Outside every range but 30d.
        session.add(FailureMetricsModel(
            project_id=project_id, connector_id=connector_id, kind="upstream_timeout",
            timestamp=utc_now() - timedelta(days=10), failure_count=1000, granularity=3600,
        ))
        await session.commit()


class TestListFailures:
    def test_empty(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        response = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/failures")

        assert response.status_code == 200
        data = response.json()
        assert data["records"] == []
        assert data["next_cursor"] is None
        assert data["total"] == 0
        assert data["appended"] == 0
        assert data["enabled"] is True
        assert data["max_entries"] > 0

    def test_the_log_settings_are_this_installs(
        self, authenticated_client: TestClient, created_project: dict[str, Any], test_settings: Any,
    ) -> None:
        test_settings.failure_log_enabled = False
        test_settings.failure_log_max_entries = 77
        test_settings.failure_log_retention_hours = 5
        url = f"/api/v1/projects/{created_project['id']}/failures"

        data = authenticated_client.get(url).json()

        assert (data["enabled"], data["max_entries"], data["retention_hours"]) == (False, 77, 5)
        assert authenticated_client.get(f"{url}/summary").json()["enabled"] is False

    def test_unknown_project(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get("/api/v1/projects/nope/failures").status_code == 404

    def test_malformed_cursor_and_kind_are_client_errors(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        url = f"/api/v1/projects/{created_project['id']}/failures"
        assert authenticated_client.get(url, params={"cursor": "abc"}).status_code == 422
        assert authenticated_client.get(url, params={"cursor": "0-0"}).status_code == 422
        assert authenticated_client.get(url, params={"kind": "nonsense"}).status_code == 422
        response = authenticated_client.get(url, params={"cursor": "0-0:upstream_timeout"})
        assert response.status_code == 200
        assert response.json()["records"] == []

    async def test_examples_newest_first_across_kinds(
        self, authenticated_client: TestClient, created_project: dict[str, Any], redis_client: RedisClient
    ) -> None:
        project_id = created_project["id"]
        await _append(redis_client, project_id, [
            _entry(0),
            _entry(1, kind="upstream_timeout", status="504", upstream_status="", message="No answer"),
            _entry(2),
        ])

        data = authenticated_client.get(f"/api/v1/projects/{project_id}/failures").json()

        assert [r["target"] for r in data["records"]] == [f"host{i}.example:443" for i in (2, 1, 0)]
        record = data["records"][0]
        assert record["id"].endswith(":upstream_rejected")
        assert record["status"] == 502
        assert record["upstream_status"] == 407
        assert record["count"] == 3
        assert record["latency_ms"] == 41.5
        assert record["connector_name"] == "Bright Data"
        assert record["session_id"] is None
        assert data["records"][1]["upstream_status"] is None
        assert (data["total"], data["appended"]) == (3, 3)

    async def test_one_kind(
        self, authenticated_client: TestClient, created_project: dict[str, Any], redis_client: RedisClient
    ) -> None:
        project_id = created_project["id"]
        await _append(redis_client, project_id, [_entry(0), _entry(1, kind="upstream_timeout"), _entry(2)])

        data = authenticated_client.get(
            f"/api/v1/projects/{project_id}/failures", params={"kind": "upstream_timeout"}
        ).json()
        assert [r["target"] for r in data["records"]] == ["host1.example:443"]
        assert (data["total"], data["appended"]) == (1, 1)

    async def test_pagination(
        self, authenticated_client: TestClient, created_project: dict[str, Any], redis_client: RedisClient
    ) -> None:
        project_id = created_project["id"]
        await _append(redis_client, project_id, [
            _entry(i, kind="upstream_timeout" if i % 2 else "upstream_rejected") for i in range(5)
        ])
        url = f"/api/v1/projects/{project_id}/failures"

        first = authenticated_client.get(url, params={"count": 2}).json()
        assert [r["target"] for r in first["records"]] == ["host4.example:443", "host3.example:443"]
        assert first["next_cursor"] == first["records"][-1]["id"]

        rest = authenticated_client.get(url, params={"count": 10, "cursor": first["next_cursor"]}).json()
        assert [r["target"] for r in rest["records"]] == [f"host{i}.example:443" for i in (2, 1, 0)]
        assert rest["next_cursor"] is None

    def test_count_is_bounded(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        url = f"/api/v1/projects/{created_project['id']}/failures"
        assert authenticated_client.get(url, params={"count": 0}).status_code == 422
        assert authenticated_client.get(url, params={"count": 1001}).status_code == 422


class TestHead:
    async def test_tells_how_many_examples_arrived_since(
        self, authenticated_client: TestClient, created_project: dict[str, Any], redis_client: RedisClient
    ) -> None:
        project_id = created_project["id"]
        await _append(redis_client, project_id, [_entry(0)])
        loaded = authenticated_client.get(f"/api/v1/projects/{project_id}/failures").json()["appended"]

        await _append(redis_client, project_id, [_entry(1), _entry(2, kind="upstream_timeout")])
        head = authenticated_client.get(f"/api/v1/projects/{project_id}/failures/head").json()
        assert head["appended"] - loaded == 2
        assert head["total"] == 3
        one_kind = authenticated_client.get(
            f"/api/v1/projects/{project_id}/failures/head", params={"kind": "upstream_timeout"}
        ).json()
        assert one_kind == {"appended": 1, "total": 1}

    def test_unknown_project(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get("/api/v1/projects/nope/failures/head").status_code == 404


class TestSummary:
    def test_empty(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        data = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/failures/summary").json()
        assert data["total"] == 0
        assert data["kinds"] == [] and data["connectors"] == [] and data["series"] == []
        assert (data["range"], data["bucket_seconds"]) == ("24h", 900)

    def test_counts_by_kind_vendor_status_and_connector(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        project_id, connector_id = created_project["id"], created_connector["id"]
        _seed_counts(authenticated_client, project_id, connector_id)

        data = authenticated_client.get(
            f"/api/v1/projects/{project_id}/failures/summary", params={"range": "1h"}
        ).json()

        assert data["total"] == 51
        assert [(k["kind"], k["count"]) for k in data["kinds"]] == [
            ("upstream_rejected", 35), ("upstream_timeout", 12), ("no_proxy_available", 4),
        ]
        assert data["kinds"][0]["upstream_statuses"] == [{"status": 407, "count": 30}, {"status": 502, "count": 5}]
        assert data["kinds"][1]["upstream_statuses"] == []
        assert data["connectors"] == [
            {"connector_id": connector_id, "name": created_connector["name"], "count": 47},
            {"connector_id": None, "name": None, "count": 4},
        ]
        assert [s["kind"] for s in data["series"]] == ["upstream_rejected", "upstream_timeout", "no_proxy_available"]
        # Every bucket of the hour is there, zeros where it was quiet.
        points = data["series"][0]["points"]
        assert len(points) in (60, 61)
        assert sum(p["count"] for p in points) == 35

    def test_older_counts_join_a_wider_range(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        _seed_counts(authenticated_client, project_id, created_connector["id"])

        data = authenticated_client.get(
            f"/api/v1/projects/{project_id}/failures/summary", params={"range": "30d"}
        ).json()
        assert data["total"] == 1051
        assert data["kinds"][0] == {"kind": "upstream_timeout", "count": 1012, "upstream_statuses": []}
        assert data["bucket_seconds"] == 6 * 3600

    def test_range_is_validated(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        url = f"/api/v1/projects/{created_project['id']}/failures/summary"
        assert authenticated_client.get(url, params={"range": "2h"}).status_code == 422
        assert authenticated_client.get("/api/v1/projects/nope/failures/summary").status_code == 404


class TestClearFailures:
    async def test_clear(
        self, authenticated_client: TestClient, created_project: dict[str, Any], redis_client: RedisClient
    ) -> None:
        project_id = created_project["id"]
        await _append(redis_client, project_id, [_entry(), _entry(1, kind="upstream_timeout")])

        assert authenticated_client.delete(f"/api/v1/projects/{project_id}/failures").status_code == 200
        data = authenticated_client.get(f"/api/v1/projects/{project_id}/failures").json()
        assert data["records"] == [] and data["appended"] == 0

    async def test_viewers_can_read_but_not_clear(
        self,
        authenticated_client: TestClient,
        viewer_auth_headers: dict[str, str],
        created_project: dict[str, Any],
        redis_client: RedisClient,
    ) -> None:
        project_id = created_project["id"]
        await _append(redis_client, project_id, [_entry()])
        url = f"/api/v1/projects/{project_id}/failures"

        assert authenticated_client.get(url, headers=viewer_auth_headers).status_code == 200
        assert authenticated_client.get(f"{url}/summary", headers=viewer_auth_headers).status_code == 200
        assert authenticated_client.delete(url, headers=viewer_auth_headers).status_code == 403
        assert len(authenticated_client.get(url).json()["records"]) == 1

    def test_unknown_project(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.delete("/api/v1/projects/nope/failures").status_code == 404

    async def test_deleting_the_project_drops_its_log(
        self, authenticated_client: TestClient, created_project: dict[str, Any], redis_client: RedisClient
    ) -> None:
        """The examples go with the project; its counts not flushed yet are dropped by the metrics flush (``tests/jobs/test_metrics.py``)."""
        project_id = created_project["id"]
        parts = redis_client.failures_batch(
            [(project_id, "upstream_rejected", _entry())],
            {(project_id, None, "no_proxy_available", None): 3},
            max_entries=1000, retention_seconds=3600,
        )
        await redis_client.send_batch(parts)

        response = authenticated_client.request(
            "DELETE", f"/api/v1/projects/{project_id}", json={"confirmation": "permanently delete"}
        )
        assert response.status_code == 204
        assert await redis_client.count_failures(project_id, ["upstream_rejected"]) == 0
        assert await redis_client.failures_appended(project_id) == {}
        assert authenticated_client.get(f"/api/v1/projects/{project_id}/failures").status_code == 404
