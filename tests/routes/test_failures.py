# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the project failure log endpoints."""

import json
from typing import Any

from starlette.testclient import TestClient

from api.db.redis import RedisClient


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
    await redis_client.append_failures(
        [(project_id, e) for e in entries], max_entries=1000, retention_seconds=3600
    )


class TestListFailures:
    def test_empty(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        response = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/failures")

        assert response.status_code == 200
        data = response.json()
        assert data["records"] == []
        assert data["next_cursor"] is None
        assert data["total"] == 0
        assert data["enabled"] is True
        assert data["max_entries"] > 0

    def test_unknown_project(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get("/api/v1/projects/nope/failures").status_code == 404

    def test_a_malformed_cursor_is_a_client_error(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        url = f"/api/v1/projects/{created_project['id']}/failures"
        assert authenticated_client.get(url, params={"cursor": "abc"}).status_code == 422
        response = authenticated_client.get(url, params={"cursor": "0-0"})
        assert response.status_code == 200
        assert response.json()["records"] == []

    async def test_entries_newest_first_with_types(
        self, authenticated_client: TestClient, created_project: dict[str, Any], redis_client: RedisClient
    ) -> None:
        project_id = created_project["id"]
        await _append(redis_client, project_id, [_entry(0), _entry(1)])

        data = authenticated_client.get(f"/api/v1/projects/{project_id}/failures").json()

        assert [r["target"] for r in data["records"]] == ["host1.example:443", "host0.example:443"]
        record = data["records"][0]
        assert record["status"] == 502
        assert record["upstream_status"] == 407
        assert record["count"] == 3
        assert record["latency_ms"] == 41.5
        assert record["connector_name"] == "Bright Data"
        assert record["session_id"] is None
        assert data["total"] == 2

    async def test_a_suppressed_entry_carries_its_breakdown(
        self, authenticated_client: TestClient, created_project: dict[str, Any], redis_client: RedisClient
    ) -> None:
        project_id = created_project["id"]
        await _append(redis_client, project_id, [{
            "kind": "suppressed", "status": "0", "message": "4 more failures", "count": "4",
            "breakdown": json.dumps({"upstream_timeout": 3, "no_proxy_available": 1}),
            "first_at": "2026-10-15T10:00:00", "last_at": "2026-10-15T10:00:04",
        }])

        (record,) = authenticated_client.get(f"/api/v1/projects/{project_id}/failures").json()["records"]
        assert record["kind"] == "suppressed"
        assert record["breakdown"] == {"upstream_timeout": 3, "no_proxy_available": 1}
        assert record["proxy"] is None

    async def test_pagination(
        self, authenticated_client: TestClient, created_project: dict[str, Any], redis_client: RedisClient
    ) -> None:
        project_id = created_project["id"]
        await _append(redis_client, project_id, [_entry(i) for i in range(5)])

        first = authenticated_client.get(f"/api/v1/projects/{project_id}/failures", params={"count": 2}).json()
        assert [r["target"] for r in first["records"]] == ["host4.example:443", "host3.example:443"]
        assert first["next_cursor"] is not None

        rest = authenticated_client.get(
            f"/api/v1/projects/{project_id}/failures", params={"count": 10, "cursor": first["next_cursor"]}
        ).json()
        assert [r["target"] for r in rest["records"]] == [f"host{i}.example:443" for i in (2, 1, 0)]
        assert rest["next_cursor"] is None

    def test_count_is_bounded(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        url = f"/api/v1/projects/{created_project['id']}/failures"
        assert authenticated_client.get(url, params={"count": 0}).status_code == 422
        assert authenticated_client.get(url, params={"count": 1001}).status_code == 422


class TestClearFailures:
    async def test_clear(
        self, authenticated_client: TestClient, created_project: dict[str, Any], redis_client: RedisClient
    ) -> None:
        project_id = created_project["id"]
        await _append(redis_client, project_id, [_entry()])

        assert authenticated_client.delete(f"/api/v1/projects/{project_id}/failures").status_code == 200
        assert authenticated_client.get(f"/api/v1/projects/{project_id}/failures").json()["records"] == []

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
        assert authenticated_client.delete(url, headers=viewer_auth_headers).status_code == 403
        assert len(authenticated_client.get(url).json()["records"]) == 1

    def test_unknown_project(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.delete("/api/v1/projects/nope/failures").status_code == 404

    async def test_deleting_the_project_drops_its_log(
        self, authenticated_client: TestClient, created_project: dict[str, Any], redis_client: RedisClient
    ) -> None:
        project_id = created_project["id"]
        await _append(redis_client, project_id, [_entry()])

        response = authenticated_client.request(
            "DELETE", f"/api/v1/projects/{project_id}", json={"confirmation": "permanently delete"}
        )
        assert response.status_code == 204
        assert await redis_client.count_failures(project_id) == 0
