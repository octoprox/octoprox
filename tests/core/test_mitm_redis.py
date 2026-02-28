# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for MITM request recording in RedisClient."""

import json

from api.db.redis import RedisClient


class TestMitmRequestRecording:
    """Tests for MITM request stream operations."""

    async def test_record_and_get_mitm_request(self, redis_client: RedisClient) -> None:
        """Test recording a single MITM request and retrieving it."""
        project_id = "test-project"
        fields = {
            "timestamp": "2026-01-15T10:00:00",
            "method": "GET",
            "url": "https://example.com/api/data",
            "request_headers": json.dumps({"host": "example.com", "user-agent": "TestBot/1.0"}),
            "request_body_size": "0",
            "request_content_type": "",
            "status_code": "200",
            "response_headers": json.dumps({"content-type": "application/json"}),
            "response_body_size": "1234",
            "response_content_type": "application/json",
            "target_host": "example.com",
            "proxy_url": "http://proxy:8080",
            "mitm_mode": "match_ua",
            "mitm_engine": "curl_cffi",
            "mitm_browser": "",
            "latency_ms": "42.5",
        }

        await redis_client.record_mitm_request(project_id, fields)

        records = await redis_client.get_mitm_requests(project_id)
        assert len(records) == 1
        record = records[0]
        assert "id" in record
        assert record["method"] == "GET"
        assert record["url"] == "https://example.com/api/data"
        assert record["status_code"] == "200"
        assert record["latency_ms"] == "42.5"
        assert record["target_host"] == "example.com"

    async def test_get_mitm_requests_newest_first(self, redis_client: RedisClient) -> None:
        """Test that records are returned newest first."""
        project_id = "test-project-order"

        for i in range(5):
            fields = {
                "timestamp": f"2026-01-15T10:00:0{i}",
                "method": "GET",
                "url": f"https://example.com/path/{i}",
                "request_headers": "{}",
                "request_body_size": "0",
                "request_content_type": "",
                "status_code": "200",
                "response_headers": "{}",
                "response_body_size": "0",
                "response_content_type": "",
                "target_host": "example.com",
                "proxy_url": "http://proxy:8080",
                "mitm_mode": "plain",
                "mitm_engine": "",
                "mitm_browser": "",
                "latency_ms": "10.0",
            }
            await redis_client.record_mitm_request(project_id, fields)

        records = await redis_client.get_mitm_requests(project_id, count=5)
        assert len(records) == 5
        # Newest first: /path/4 should be first, /path/0 last
        assert records[0]["url"].endswith("/path/4")
        assert records[-1]["url"].endswith("/path/0")

    async def test_get_mitm_requests_count_limit(self, redis_client: RedisClient) -> None:
        """Test that count parameter limits results."""
        project_id = "test-project-limit"

        for i in range(10):
            fields = {
                "timestamp": f"2026-01-15T10:00:{i:02d}",
                "method": "GET",
                "url": f"https://example.com/{i}",
                "request_headers": "{}",
                "request_body_size": "0",
                "request_content_type": "",
                "status_code": "200",
                "response_headers": "{}",
                "response_body_size": "0",
                "response_content_type": "",
                "target_host": "example.com",
                "proxy_url": "http://proxy:8080",
                "mitm_mode": "plain",
                "mitm_engine": "",
                "mitm_browser": "",
                "latency_ms": "10.0",
            }
            await redis_client.record_mitm_request(project_id, fields)

        records = await redis_client.get_mitm_requests(project_id, count=3)
        assert len(records) == 3

    async def test_get_mitm_requests_cursor_pagination(self, redis_client: RedisClient) -> None:
        """Test cursor-based pagination returns older records."""
        project_id = "test-project-cursor"

        for i in range(6):
            fields = {
                "timestamp": f"2026-01-15T10:00:{i:02d}",
                "method": "GET",
                "url": f"https://example.com/{i}",
                "request_headers": "{}",
                "request_body_size": "0",
                "request_content_type": "",
                "status_code": "200",
                "response_headers": "{}",
                "response_body_size": "0",
                "response_content_type": "",
                "target_host": "example.com",
                "proxy_url": "http://proxy:8080",
                "mitm_mode": "plain",
                "mitm_engine": "",
                "mitm_browser": "",
                "latency_ms": "10.0",
            }
            await redis_client.record_mitm_request(project_id, fields)

        # Get first page
        page1 = await redis_client.get_mitm_requests(project_id, count=3)
        assert len(page1) == 3
        # Newest 3: /5, /4, /3
        assert page1[0]["url"].endswith("/5")
        assert page1[2]["url"].endswith("/3")

        # Get second page using last id as cursor
        cursor = page1[-1]["id"]
        page2 = await redis_client.get_mitm_requests(project_id, count=3, before_id=cursor)
        assert len(page2) == 3
        # Older 3: /2, /1, /0
        assert page2[0]["url"].endswith("/2")
        assert page2[2]["url"].endswith("/0")

    async def test_get_mitm_requests_empty(self, redis_client: RedisClient) -> None:
        """Test getting records when none exist."""
        records = await redis_client.get_mitm_requests("nonexistent-project")
        assert records == []

    async def test_clear_mitm_requests(self, redis_client: RedisClient) -> None:
        """Test clearing all MITM records for a project."""
        project_id = "test-project-clear"

        for i in range(5):
            fields = {
                "timestamp": f"2026-01-15T10:00:{i:02d}",
                "method": "GET",
                "url": f"https://example.com/{i}",
                "request_headers": "{}",
                "request_body_size": "0",
                "request_content_type": "",
                "status_code": "200",
                "response_headers": "{}",
                "response_body_size": "0",
                "response_content_type": "",
                "target_host": "example.com",
                "proxy_url": "http://proxy:8080",
                "mitm_mode": "plain",
                "mitm_engine": "",
                "mitm_browser": "",
                "latency_ms": "10.0",
            }
            await redis_client.record_mitm_request(project_id, fields)

        records = await redis_client.get_mitm_requests(project_id)
        assert len(records) == 5

        await redis_client.clear_mitm_requests(project_id)

        records = await redis_client.get_mitm_requests(project_id)
        assert records == []

    async def test_clear_nonexistent_project(self, redis_client: RedisClient) -> None:
        """Test clearing records for a non-existent project is a no-op."""
        await redis_client.clear_mitm_requests("nonexistent-project")

    async def test_project_isolation(self, redis_client: RedisClient) -> None:
        """Test that records are isolated per project."""
        fields_a = {
            "timestamp": "2026-01-15T10:00:00",
            "method": "GET",
            "url": "https://a.com/",
            "request_headers": "{}",
            "request_body_size": "0",
            "request_content_type": "",
            "status_code": "200",
            "response_headers": "{}",
            "response_body_size": "0",
            "response_content_type": "",
            "target_host": "a.com",
            "proxy_url": "http://proxy:8080",
            "mitm_mode": "plain",
            "mitm_engine": "",
            "mitm_browser": "",
            "latency_ms": "10.0",
        }
        fields_b = {**fields_a, "url": "https://b.com/", "target_host": "b.com"}

        await redis_client.record_mitm_request("project-a", fields_a)
        await redis_client.record_mitm_request("project-b", fields_b)

        records_a = await redis_client.get_mitm_requests("project-a")
        records_b = await redis_client.get_mitm_requests("project-b")

        assert len(records_a) == 1
        assert len(records_b) == 1
        assert records_a[0]["target_host"] == "a.com"
        assert records_b[0]["target_host"] == "b.com"
