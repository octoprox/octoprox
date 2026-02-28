# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for MITM traffic inspection endpoints."""

import json
from typing import Any

from starlette.testclient import TestClient

from api.db.redis import RedisClient


def _make_fields(i: int = 0, **overrides: str) -> dict[str, str]:
    """Helper to create MITM request fields for testing."""
    fields = {
        "timestamp": f"2026-01-15T10:00:{i:02d}",
        "method": "GET",
        "url": f"https://example.com/path/{i}",
        "request_headers": json.dumps([["host", "example.com"]]),
        "request_body_size": "0",
        "request_content_type": "",
        "status_code": "200",
        "response_headers": json.dumps([["content-type", "text/html"]]),
        "response_body_size": "1024",
        "response_content_type": "text/html",
        "target_host": "example.com",
        "proxy_url": "http://proxy:8080",
        "mitm_mode": "match_ua",
        "mitm_engine": "curl_cffi",
        "mitm_browser": "",
        "latency_ms": "42.5",
    }
    fields.update(overrides)
    return fields


class TestMitmEndpoints:
    """Tests for MITM request listing and clearing endpoints."""

    def test_list_mitm_requests_empty(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test listing MITM requests when none exist."""
        project_id = created_project["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/mitm/requests")

        assert response.status_code == 200
        data = response.json()
        assert data["records"] == []
        assert data["next_cursor"] is None

    def test_list_mitm_requests_project_not_found(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test listing MITM requests for non-existent project."""
        response = authenticated_client.get("/api/v1/projects/nonexistent/mitm/requests")
        assert response.status_code == 404

    async def test_list_mitm_requests_with_data(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        redis_client: RedisClient,
    ) -> None:
        """Test listing MITM requests after recording some via Redis."""
        project_id = created_project["id"]

        for i in range(3):
            await redis_client.record_mitm_request(project_id, _make_fields(i))

        response = authenticated_client.get(f"/api/v1/projects/{project_id}/mitm/requests")

        assert response.status_code == 200
        data = response.json()
        assert len(data["records"]) == 3
        # Newest first
        assert data["records"][0]["url"] == "https://example.com/path/2"
        assert data["records"][2]["url"] == "https://example.com/path/0"
        # Verify field types are correct
        record = data["records"][0]
        assert isinstance(record["request_headers"], list)
        assert isinstance(record["response_headers"], list)
        assert isinstance(record["status_code"], int)
        assert isinstance(record["request_body_size"], int)
        assert isinstance(record["latency_ms"], float)

    async def test_list_mitm_requests_pagination(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        redis_client: RedisClient,
    ) -> None:
        """Test cursor-based pagination for MITM requests."""
        project_id = created_project["id"]

        for i in range(5):
            await redis_client.record_mitm_request(
                project_id,
                _make_fields(i, url=f"https://example.com/{i}"),
            )

        # First page: request 2 items
        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/mitm/requests",
            params={"count": 2},
        )
        assert response.status_code == 200
        page1 = response.json()
        assert len(page1["records"]) == 2
        assert page1["next_cursor"] is not None

        # Second page
        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/mitm/requests",
            params={"count": 2, "cursor": page1["next_cursor"]},
        )
        assert response.status_code == 200
        page2 = response.json()
        assert len(page2["records"]) == 2
        assert page2["next_cursor"] is not None

        # Third page (last item)
        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/mitm/requests",
            params={"count": 2, "cursor": page2["next_cursor"]},
        )
        assert response.status_code == 200
        page3 = response.json()
        assert len(page3["records"]) == 1
        assert page3["next_cursor"] is None

        # Verify no overlap between pages
        all_ids = [r["id"] for r in page1["records"] + page2["records"] + page3["records"]]
        assert len(all_ids) == len(set(all_ids))

    async def test_clear_mitm_requests(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        redis_client: RedisClient,
    ) -> None:
        """Test clearing MITM requests."""
        project_id = created_project["id"]

        await redis_client.record_mitm_request(project_id, _make_fields())

        # Verify record exists
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/mitm/requests")
        assert len(response.json()["records"]) == 1

        # Clear
        response = authenticated_client.delete(f"/api/v1/projects/{project_id}/mitm/requests")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

        # Verify empty
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/mitm/requests")
        assert response.json()["records"] == []

    def test_clear_mitm_requests_project_not_found(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test clearing MITM requests for non-existent project."""
        response = authenticated_client.delete("/api/v1/projects/nonexistent/mitm/requests")
        assert response.status_code == 404

    def test_list_mitm_requests_count_validation(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test that count parameter is validated."""
        project_id = created_project["id"]

        # count too small
        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/mitm/requests",
            params={"count": 0},
        )
        assert response.status_code == 422

        # count too large
        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/mitm/requests",
            params={"count": 201},
        )
        assert response.status_code == 422
