# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for proxy endpoints, on an api process: writes through the proxy service, reads from the read model.

The app runs no worker, so a write that starts work is checked by the job it
asks for (its key on its kind's job stream), not by the job's effect.
"""

import time
import uuid
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import update
from starlette.testclient import TestClient

from api.core.roles import JobKind
from api.core.stats import MetricDelta
from api.db.models import ProxyModel
from api.db.redis import (
    JOB_STREAM_KEY,
    PROXY_METRICS_KEY,
    PROXY_REQUESTS_KEY,
    PROXY_WINDOWS,
    QUARANTINE_KEY,
    RedisClient,
)
from api.db.repository import MetricsRepository
from api.db.session import SessionFactory
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.providers.sdk.strategies import META_GEO_COUNTRY, META_SESSION_ID
from tests.conftest import api_runtime, on_app_loop, queued_jobs

# A vendor session pool of two slots per country listing no country: a provider that syncs.
OXYLABS_CREDENTIAL = {"proxy_type": "residential", "username": "alice", "password": "s3cret"}
OXYLABS_POOL = {"num_proxies": 2, "session_duration_minutes": 10}
# Cloud instances the scale job creates and terminates.
AWS_CREDENTIAL = {"access_key": "AKIAIOSFODNN7EXAMPLE", "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}
AWS_CONNECTOR = {
    "instance_name": "octo",
    "region": "us-east-1",
    "instance_type": "t3.micro",
    "key_pair_name": "octo",
    "security_group": "octo",
    "min_proxies": 1,
    "max_proxies": 4,
}


class TestProxyEndpoints:
    """Tests for proxy CRUD endpoints."""

    def test_list_proxies_empty(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """Test listing proxies when none exist (only connector, no proxy)."""
        project_id = created_project["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/proxies")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["healthy"] == 0
        assert data["proxies"] == []

    def test_list_proxies_project_not_found(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test listing proxies for non-existent project."""
        response = authenticated_client.get("/api/v1/projects/non-existent/proxies")

        assert response.status_code == 404

    def test_create_proxy(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
        sample_proxy_data: dict[str, Any],
    ) -> None:
        """Test creating a new proxy."""
        project_id = created_project["id"]
        proxy_data = sample_proxy_data.copy()
        proxy_data["connector_id"] = created_connector["id"]

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/proxies",
            json=proxy_data,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["host"] == proxy_data["host"]
        assert data["port"] == proxy_data["port"]
        assert data["connector_id"] == created_connector["id"]
        assert data["connector_name"] == created_connector["name"]
        # Nobody has checked it yet: unknown until the health job's first run.
        assert (data["status"], data["request_count"], data["quarantined"]) == ("unknown", 0, False)
        assert "id" in data

    def test_create_proxy_connector_not_found(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        sample_proxy_data: dict[str, Any],
    ) -> None:
        """Test creating proxy with non-existent connector."""
        project_id = created_project["id"]
        proxy_data = sample_proxy_data.copy()
        proxy_data["connector_id"] = "non-existent"

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/proxies",
            json=proxy_data,
        )

        assert response.status_code == 404
        assert "Connector" in response.json()["detail"]

    def test_create_proxy_on_a_connector_that_is_not_static_is_refused(
        self, authenticated_client: TestClient, created_project: dict[str, Any], sample_proxy_data: dict[str, Any],
    ) -> None:
        connector = _connector(authenticated_client, created_project, "oxylabs", OXYLABS_CREDENTIAL, OXYLABS_POOL)

        response = authenticated_client.post(
            f"/api/v1/projects/{created_project['id']}/proxies",
            json={**sample_proxy_data, "connector_id": connector["id"]},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "Proxies can only be manually added to STATIC_PROXY_PROVIDER connectors"

    def test_get_proxy(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_proxy: dict[str, Any],
    ) -> None:
        """Test getting a specific proxy."""
        project_id = created_project["id"]
        proxy_id = created_proxy["id"]

        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/proxies/{proxy_id}"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == proxy_id
        assert data["connector_name"] is not None

    def test_get_proxy_not_found(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test getting a non-existent proxy."""
        project_id = created_project["id"]
        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/proxies/non-existent"
        )

        assert response.status_code == 404

    def test_update_proxy(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_proxy: dict[str, Any],
    ) -> None:
        """Test updating a proxy."""
        project_id = created_project["id"]
        proxy_id = created_proxy["id"]

        response = authenticated_client.patch(
            f"/api/v1/projects/{project_id}/proxies/{proxy_id}",
            json={"host": "updated.example.com", "port": 9090},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["host"] == "updated.example.com"
        assert data["port"] == 9090
        # What was written is what a later read shows.
        assert authenticated_client.get(f"/api/v1/projects/{project_id}/proxies/{proxy_id}").json()["port"] == 9090

    def test_an_edit_never_moves_the_status(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        proxy = _add_proxy(authenticated_client, created_connector["id"], ProxyStatus.HEALTHY)
        url = f"/api/v1/projects/{created_project['id']}/proxies/{proxy.id}"

        response = authenticated_client.patch(url, json={"tags": ["edited"]})

        assert response.status_code == 200
        assert (response.json()["status"], response.json()["tags"]) == ("healthy", ["edited"])

    def test_create_proxy_with_country(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
        sample_proxy_data: dict[str, Any],
    ) -> None:
        """A manual country is stored upper-cased and returned."""
        project_id = created_project["id"]
        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/proxies",
            json={**sample_proxy_data, "connector_id": created_connector["id"], "country": "gb"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["country"] == "GB"
        assert response.json()["country_source"] == "manual"

    def test_create_proxy_rejects_bad_country(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
        sample_proxy_data: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/proxies",
            json={**sample_proxy_data, "connector_id": created_connector["id"], "country": "GBR"},
        )
        assert response.status_code == 422

    def test_update_proxy_country_set_and_clear(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_proxy: dict[str, Any],
    ) -> None:
        project_id = created_project["id"]
        proxy_id = created_proxy["id"]
        assert created_proxy["country"] is None

        response = authenticated_client.patch(f"/api/v1/projects/{project_id}/proxies/{proxy_id}", json={"country": "de"})
        assert response.status_code == 200
        assert response.json()["country"] == "DE"

        response = authenticated_client.patch(f"/api/v1/projects/{project_id}/proxies/{proxy_id}", json={"country": ""})
        assert response.status_code == 200
        assert response.json()["country"] is None

    def test_locate_proxy_records_exit_location(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_proxy: dict[str, Any],
        monkeypatch: Any,
    ) -> None:
        from unittest.mock import AsyncMock

        from api.geo.attributor import ProxyAttributor
        from api.models.location import LocationTarget

        monkeypatch.setattr(ProxyAttributor, "locate", AsyncMock(return_value=("198.51.100.7", LocationTarget(country="NL"))))
        project_id = created_project["id"]
        proxy_id = created_proxy["id"]

        response = authenticated_client.post(f"/api/v1/projects/{project_id}/proxies/{proxy_id}/locate")
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["country"] == "NL"
        assert data["display_host"] == "198.51.100.7"
        assert data["host"] == created_proxy["host"]
        # Written to the row: every api process and the next feed compile see it.
        stored = authenticated_client.get(f"/api/v1/projects/{project_id}/proxies/{proxy_id}").json()
        assert (stored["country"], stored["display_host"]) == ("NL", "198.51.100.7")

    def test_locate_proxy_failure_is_502(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_proxy: dict[str, Any],
        monkeypatch: Any,
    ) -> None:
        from unittest.mock import AsyncMock

        from api.geo.attributor import ProxyAttributor

        monkeypatch.setattr(ProxyAttributor, "locate", AsyncMock(return_value=(None, "")))
        project_id = created_project["id"]
        response = authenticated_client.post(f"/api/v1/projects/{project_id}/proxies/{created_proxy['id']}/locate")
        assert response.status_code == 502

    def test_locate_unknown_proxy(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        response = authenticated_client.post(f"/api/v1/projects/{created_project['id']}/proxies/nope/locate")
        assert response.status_code == 404

    def test_delete_proxy(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_proxy: dict[str, Any],
    ) -> None:
        """Test deleting a proxy."""
        project_id = created_project["id"]
        proxy_id = created_proxy["id"]

        response = authenticated_client.delete(
            f"/api/v1/projects/{project_id}/proxies/{proxy_id}"
        )

        assert response.status_code == 204

        # Verify it's deleted
        get_response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/proxies/{proxy_id}"
        )
        assert get_response.status_code == 404
        assert authenticated_client.delete(f"/api/v1/projects/{project_id}/proxies/{proxy_id}").status_code == 404

    def test_list_proxies_with_data(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
        sample_proxy_data: dict[str, Any],
    ) -> None:
        """Test listing proxies when some exist."""
        project_id = created_project["id"]

        # Create multiple proxies
        for i in range(3):
            proxy_data = sample_proxy_data.copy()
            proxy_data["connector_id"] = created_connector["id"]
            proxy_data["host"] = f"proxy{i}.example.com"
            authenticated_client.post(
                f"/api/v1/projects/{project_id}/proxies",
                json=proxy_data,
            )

        response = authenticated_client.get(f"/api/v1/projects/{project_id}/proxies")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 3
        assert len(data["proxies"]) == 3

    def test_list_proxies_order_is_stable(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
        sample_proxy_data: dict[str, Any],
    ) -> None:
        """Listing order is creation order, whatever the order Postgres returns rows in.

        Timestamps are pinned equal to exercise the id tie-break, which is what
        keeps bulk-uploaded rows ordered identically on every api process, so a
        load-balanced UI does not see rows shuffle between polls.
        """
        project_id = created_project["id"]

        created_ids = []
        for i in range(3):
            proxy_data = sample_proxy_data.copy()
            proxy_data["connector_id"] = created_connector["id"]
            proxy_data["host"] = f"proxy{i}.example.com"
            response = authenticated_client.post(
                f"/api/v1/projects/{project_id}/proxies",
                json=proxy_data,
            )
            assert response.status_code == 201
            created_ids.append(response.json()["id"])

        def listed_ids() -> list[str]:
            response = authenticated_client.get(f"/api/v1/projects/{project_id}/proxies")
            assert response.status_code == 200
            return [p["id"] for p in response.json()["proxies"]]

        assert listed_ids() == created_ids

        pinned = datetime(2026, 1, 1)
        on_app_loop(authenticated_client, _pin_created_at, api_runtime(authenticated_client).session_factory, created_ids, pinned)

        assert listed_ids() == sorted(created_ids)


class TestDetect:
    """POST /proxies/{id}/locate: a lookup through the proxy now, attributed as any sighting is."""

    def test_a_slot_its_project_refuses_is_rotated_and_the_answer_shows_what_was_found(
        self, authenticated_client: TestClient, created_project: dict[str, Any], monkeypatch: Any,
    ) -> None:
        from unittest.mock import AsyncMock

        from api.geo.attributor import ProxyAttributor
        from api.models.location import LocationTarget

        project_id = created_project["id"]
        connector = _connector(authenticated_client, created_project, "oxylabs", OXYLABS_CREDENTIAL, OXYLABS_POOL)
        # A vendor session slot provisioned for the US, its secret a placeholder the credential fills.
        slot = _add_proxy(
            authenticated_client, connector["id"], ProxyStatus.HEALTHY,
            username="customer-alice-sessid-abc", password="{password}",
            metadata={META_SESSION_ID: "abc", META_GEO_COUNTRY: "US"},
        )
        response = authenticated_client.patch(f"/api/v1/projects/{project_id}", json={"location_contradicted": "refuse"})
        assert response.status_code == 200, response.text
        _take_job(authenticated_client, JobKind.SYNC, connector["id"])
        monkeypatch.setattr(ProxyAttributor, "locate", AsyncMock(return_value=("198.51.100.7", LocationTarget(country="NL"))))

        response = authenticated_client.post(f"/api/v1/projects/{project_id}/proxies/{slot.id}/locate")

        assert response.status_code == 200, response.text
        data = response.json()
        assert (data["location_verdict"], data["display_host"]) == ("contradicted", "198.51.100.7")
        # The stored row's credentials, never the ones the lookup filled in.
        assert (data["username"], data["password"]) == ("customer-alice-sessid-abc", "{password}")
        # Rotated: the row is gone and its connector asked to provision a fresh session.
        assert authenticated_client.get(f"/api/v1/projects/{project_id}/proxies/{slot.id}").status_code == 404
        assert connector["id"] in queued_jobs(authenticated_client, JobKind.SYNC)


class TestProxyJobs:
    """What a proxy write asks the workers for."""

    def test_a_static_proxy_asks_for_its_first_check_and_an_exit_lookup(
        self, authenticated_client: TestClient, created_proxy: dict[str, Any],
    ) -> None:
        assert created_proxy["id"] in queued_jobs(authenticated_client, JobKind.HEALTH)
        assert created_proxy["id"] in queued_jobs(authenticated_client, JobKind.ENRICH)

    def test_a_proxy_is_saved_when_its_job_requests_fail(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
        sample_proxy_data: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Redis goes away between the commit and the job requests: the write
        # stands, rather than a 503 for a row that is stored.
        async def refused(*args: Any, **kwargs: Any) -> None:
            raise ConnectionError("Redis is down")

        monkeypatch.setattr(api_runtime(authenticated_client).jobs, "publish", refused)
        project_id = created_project["id"]
        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/proxies",
            json={**sample_proxy_data, "connector_id": created_connector["id"]},
        )

        assert response.status_code == 201, response.text
        proxy_id = response.json()["id"]
        assert authenticated_client.get(f"/api/v1/projects/{project_id}/proxies/{proxy_id}").status_code == 200
        assert proxy_id not in queued_jobs(authenticated_client, JobKind.HEALTH)

    def test_a_country_set_by_hand_is_not_looked_up(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
        sample_proxy_data: dict[str, Any],
    ) -> None:
        response = authenticated_client.post(
            f"/api/v1/projects/{created_project['id']}/proxies",
            json={**sample_proxy_data, "connector_id": created_connector["id"], "country": "FR"},
        )
        assert response.status_code == 201, response.text

        proxy_id = response.json()["id"]
        assert proxy_id in queued_jobs(authenticated_client, JobKind.HEALTH)
        assert proxy_id not in queued_jobs(authenticated_client, JobKind.ENRICH)

    def test_deleting_a_cloud_proxy_terminates_it_through_the_scale_job(
        self, authenticated_client: TestClient, created_project: dict[str, Any],
    ) -> None:
        connector = _connector(authenticated_client, created_project, "aws", AWS_CREDENTIAL, AWS_CONNECTOR)
        proxy = _add_proxy(authenticated_client, connector["id"], ProxyStatus.HEALTHY)
        # The connector's create asked for a scale already; a worker took it meanwhile.
        _take_job(authenticated_client, JobKind.SCALE, connector["id"])
        url = f"/api/v1/projects/{created_project['id']}/proxies/{proxy.id}"

        assert authenticated_client.delete(url).status_code == 204

        # Still there until the instance is gone: terminating, and the scale job asked to finish it.
        assert authenticated_client.get(url).json()["status"] == "terminating"
        assert connector["id"] in queued_jobs(authenticated_client, JobKind.SCALE)

    def test_deleting_a_synced_slot_asks_its_connector_for_a_sync(
        self, authenticated_client: TestClient, created_project: dict[str, Any],
    ) -> None:
        connector = _connector(authenticated_client, created_project, "oxylabs", OXYLABS_CREDENTIAL, OXYLABS_POOL)
        slot = _add_proxy(authenticated_client, connector["id"], ProxyStatus.HEALTHY)
        _take_job(authenticated_client, JobKind.SYNC, connector["id"])

        response = authenticated_client.delete(f"/api/v1/projects/{created_project['id']}/proxies/{slot.id}")

        assert response.status_code == 204
        assert connector["id"] in queued_jobs(authenticated_client, JobKind.SYNC)

    def test_deleting_a_static_proxy_asks_for_nothing(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_proxy: dict[str, Any],
    ) -> None:
        response = authenticated_client.delete(f"/api/v1/projects/{created_project['id']}/proxies/{created_proxy['id']}")

        assert response.status_code == 204
        assert created_proxy["connector_id"] not in queued_jobs(authenticated_client, JobKind.SYNC)
        assert created_proxy["connector_id"] not in queued_jobs(authenticated_client, JobKind.SCALE)


class TestRuntimeState:
    """What other roles keep about a proxy, shown with its row: quarantine, counters, status."""

    def test_quarantine_and_counters_come_with_the_row(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        proxy = _add_proxy(authenticated_client, created_connector["id"], ProxyStatus.HEALTHY)
        runtime = api_runtime(authenticated_client)
        on_app_loop(authenticated_client, runtime.redis_client.client.zadd, QUARANTINE_KEY, {proxy.id: time.time() + 600})
        # All-time totals a worker flushed, plus the window gateways counted since.
        flushed = MetricDelta(request_count=8, success_count=6, failure_count=2, latency_sum_ms=800, bytes_sent=10)
        window = MetricDelta(request_count=2, success_count=2, latency_sum_ms=100, bytes_received=5)
        on_app_loop(authenticated_client, _flush_totals, runtime.session_factory, proxy.id, flushed)
        on_app_loop(authenticated_client, _write_window, runtime.redis_client, proxy.id, window)

        data = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/proxies/{proxy.id}").json()

        assert data["quarantined"] is True
        assert 590 < data["quarantine_remaining_seconds"] <= 600
        assert (data["request_count"], data["success_count"], data["failure_count"]) == (10, 8, 2)
        # The latency over the eight successes.
        assert (data["success_rate"], data["avg_latency_ms"]) == (80.0, 112.5)
        assert (data["bytes_sent"], data["bytes_received"]) == (10, 5)
        # A quarantined proxy takes no requests, so it is not counted as healthy.
        listing = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/proxies").json()
        assert (listing["total"], listing["healthy"]) == (1, 0)

    def test_unquarantine_lifts_the_quarantine_and_empties_the_window(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        proxy = _add_proxy(authenticated_client, created_connector["id"], ProxyStatus.HEALTHY)
        redis = api_runtime(authenticated_client).redis_client
        on_app_loop(authenticated_client, redis.client.zadd, QUARANTINE_KEY, {proxy.id: time.time() + 600})
        on_app_loop(authenticated_client, redis.client.set, PROXY_REQUESTS_KEY.format(proxy_id=proxy.id), "window")
        url = f"/api/v1/projects/{created_project['id']}/proxies/{proxy.id}"

        response = authenticated_client.post(f"{url}/unquarantine")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "proxy_id": proxy.id}
        assert on_app_loop(authenticated_client, redis.client.zscore, QUARANTINE_KEY, proxy.id) is None
        assert on_app_loop(authenticated_client, redis.client.exists, PROXY_REQUESTS_KEY.format(proxy_id=proxy.id)) == 0
        assert authenticated_client.get(url).json()["quarantined"] is False
        again = authenticated_client.post(f"{url}/unquarantine")
        assert again.status_code == 400 and again.json()["detail"] == "Proxy is not quarantined"

    def test_the_status_is_the_stored_column(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        healthy = _add_proxy(authenticated_client, created_connector["id"], ProxyStatus.HEALTHY)
        draining = _add_proxy(authenticated_client, created_connector["id"], ProxyStatus.DRAINING)

        listing = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/proxies").json()

        assert {p["id"]: p["status"] for p in listing["proxies"]} == {healthy.id: "healthy", draining.id: "draining"}
        assert listing["healthy"] == 1

    def test_a_disabled_connectors_proxies_are_listed_but_not_healthy(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        proxy = _add_proxy(authenticated_client, created_connector["id"], ProxyStatus.HEALTHY)
        connector_url = f"/api/v1/projects/{created_project['id']}/connectors/{created_connector['id']}"
        assert authenticated_client.patch(connector_url, json={"enabled": False}).status_code == 200

        listing = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/proxies").json()

        assert [(p["id"], p["connector_enabled"]) for p in listing["proxies"]] == [(proxy.id, False)]
        assert listing["healthy"] == 0


class TestUpload:
    """POST /proxies/upload: one URL per line, added in one transaction."""

    def _upload(self, client: TestClient, project_id: str, connector_id: str, content: bytes) -> Any:
        return client.post(
            f"/api/v1/projects/{project_id}/proxies/upload",
            files={"file": ("proxies.csv", content, "text/csv")},
            data={"connector_id": connector_id},
        )

    def test_the_lines_that_parse_are_added(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        content = b"http://10.0.0.1:8080\nnot a proxy\n\nsocks5://user:pass@10.0.0.2:1080\n"

        response = self._upload(authenticated_client, created_project["id"], created_connector["id"], content)

        assert response.status_code == 201, response.text
        data = response.json()
        assert (data["total_lines"], data["successful"], data["failed"]) == (3, 2, 1)
        assert data["errors"] == [{
            "line_number": 2,
            "line": "not a proxy",
            "error": "Invalid proxy URL format. Expected: protocol://[username:password@]host:port",
        }]
        added = {p["host"]: p for p in data["proxies"]}
        assert set(added) == {"10.0.0.1", "10.0.0.2"}
        socks = added["10.0.0.2"]
        assert (socks["protocol"], socks["username"], socks["port"]) == ("socks5", "user", 1080)
        assert (socks["connector_name"], socks["status"], socks["request_count"]) == (
            created_connector["name"], "unknown", 0,
        )
        listing = authenticated_client.get(f"/api/v1/projects/{created_project['id']}/proxies").json()
        assert listing["total"] == 2
        # Each new row gets its first check and an exit lookup.
        assert {p["id"] for p in data["proxies"]} <= queued_jobs(authenticated_client, JobKind.HEALTH)
        assert {p["id"] for p in data["proxies"]} <= queued_jobs(authenticated_client, JobKind.ENRICH)

    def test_a_connector_that_is_not_static_takes_nothing(
        self, authenticated_client: TestClient, created_project: dict[str, Any],
    ) -> None:
        connector = _connector(authenticated_client, created_project, "oxylabs", OXYLABS_CREDENTIAL, OXYLABS_POOL)

        response = self._upload(authenticated_client, created_project["id"], connector["id"], b"http://10.0.0.1:8080\n")

        assert response.status_code == 400
        assert response.json()["detail"] == "Proxies can only be uploaded to STATIC_PROXY_PROVIDER connectors"
        assert authenticated_client.get(f"/api/v1/projects/{created_project['id']}/proxies").json()["total"] == 0

    def test_the_file_must_be_utf8(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        response = self._upload(authenticated_client, created_project["id"], created_connector["id"], b"\xff\xfe\x00")

        assert response.status_code == 400
        assert response.json()["detail"] == "File must be UTF-8 encoded"

    def test_unknown_connector(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        response = self._upload(authenticated_client, created_project["id"], "nope", b"http://10.0.0.1:8080\n")

        assert response.status_code == 404


class TestProxyProjectScope:
    """A proxy belongs to one project: under another project's path it is not found."""

    def test_other_projects_proxy_is_not_found(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_proxy: dict[str, Any],
        sample_project_data: dict[str, Any],
    ) -> None:
        other = _other_project(authenticated_client, sample_project_data)
        url = f"/api/v1/projects/{other['id']}/proxies/{created_proxy['id']}"

        assert authenticated_client.get(url).status_code == 404
        assert authenticated_client.patch(url, json={"host": "taken.example.com"}).status_code == 404
        assert authenticated_client.post(f"{url}/locate").status_code == 404
        assert authenticated_client.post(f"{url}/unquarantine").status_code == 404
        assert authenticated_client.delete(url).status_code == 404
        assert authenticated_client.get(f"/api/v1/projects/{other['id']}/proxies").json()["total"] == 0
        mine = f"/api/v1/projects/{created_project['id']}/proxies/{created_proxy['id']}"
        assert authenticated_client.get(mine).json()["host"] == created_proxy["host"]

    def test_a_connector_of_another_project_is_refused(
        self,
        authenticated_client: TestClient,
        created_connector: dict[str, Any],
        sample_project_data: dict[str, Any],
        sample_proxy_data: dict[str, Any],
    ) -> None:
        other = _other_project(authenticated_client, sample_project_data)

        response = authenticated_client.post(
            f"/api/v1/projects/{other['id']}/proxies", json={**sample_proxy_data, "connector_id": created_connector["id"]},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "Connector does not belong to this project"


class TestRemovedEndpoints:
    def test_the_process_local_strategy_endpoint_is_gone(
        self, authenticated_client: TestClient, created_project: dict[str, Any],
    ) -> None:
        """The strategy is the project's ``routing_strategy``; this endpoint changed one process's default only."""
        response = authenticated_client.post(
            f"/api/v1/projects/{created_project['id']}/proxies/strategy", json={"strategy": "random"},
        )

        assert response.status_code in (404, 405)


class TestProxyRoleAccess:
    """Tests for role-based access control on proxy endpoints."""

    def test_viewer_cannot_create_proxy(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot create proxies."""
        response = viewer_client.post(
            "/api/v1/projects/some-project-id/proxies",
            json={
                "host": "proxy.example.com",
                "port": 8080,
                "protocol": "http",
                "connector_id": "some-connector-id",
            },
        )
        assert response.status_code == 403

    def test_viewer_cannot_update_proxy(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot update proxies."""
        response = viewer_client.patch(
            "/api/v1/projects/some-project-id/proxies/some-proxy-id",
            json={"host": "updated.example.com"},
        )
        assert response.status_code == 403

    def test_viewer_cannot_delete_proxy(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot delete proxies."""
        response = viewer_client.delete(
            "/api/v1/projects/some-project-id/proxies/some-proxy-id"
        )
        assert response.status_code == 403

    def test_viewer_cannot_detect_or_unquarantine(self, viewer_client: TestClient) -> None:
        assert viewer_client.post("/api/v1/projects/some-project-id/proxies/some-proxy-id/locate").status_code == 403
        assert viewer_client.post("/api/v1/projects/some-project-id/proxies/some-proxy-id/unquarantine").status_code == 403


def _connector(
    client: TestClient, project: dict[str, Any], type_: str, credential: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """A connector of ``type_`` on a fresh credential of the project."""
    response = client.post(
        f"/api/v1/projects/{project['id']}/credentials",
        json={"name": f"{type_}-{uuid.uuid4().hex[:6]}", "type": type_, "config": credential},
    )
    assert response.status_code == 201, response.text
    response = client.post(
        f"/api/v1/projects/{project['id']}/connectors",
        json={"name": f"connector-{uuid.uuid4().hex[:6]}", "credential_id": response.json()["id"], "config": config},
    )
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


def _other_project(client: TestClient, sample_project_data: dict[str, Any]) -> dict[str, Any]:
    """A second project, to address the first one's rows under its path."""
    response = client.post(
        "/api/v1/projects",
        json={**sample_project_data, "name": f"{sample_project_data['name']} B", "username": f"{sample_project_data['username']}_b"},
    )
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


def _add_proxy(client: TestClient, connector_id: str, status: ProxyStatus, **fields: Any) -> Proxy:
    """A proxy stored with ``status``, as a job adds one (a vendor slot, a cloud instance) or a health check left it."""
    proxy = Proxy(host=f"{uuid.uuid4().hex[:8]}.example.com", port=8080, protocol=ProxyProtocol.HTTP,
                  connector_id=connector_id, status=status, **fields)
    on_app_loop(client, api_runtime(client).proxy_service.add, proxy)
    return proxy


def _take_job(client: TestClient, kind: JobKind, key: str) -> None:
    """Take a requested job's entries off its kind's stream, as a worker completing them would."""
    redis = api_runtime(client).redis_client
    stream = JOB_STREAM_KEY.format(kind=kind)
    entries = on_app_loop(client, redis.client.xrange, stream)
    taken = [entry_id for entry_id, fields in entries if fields.get("key") == key]
    if taken:
        on_app_loop(client, redis.client.xdel, stream, *taken)


async def _pin_created_at(session_factory: SessionFactory, proxy_ids: list[str], at: datetime) -> None:
    """Give the proxies one creation time, as a bulk upload can."""
    async with session_factory() as session:
        await session.execute(update(ProxyModel).where(ProxyModel.id.in_(proxy_ids)).values(created_at=at))
        await session.commit()


async def _flush_totals(session_factory: SessionFactory, proxy_id: str, delta: MetricDelta) -> None:
    """A proxy's all-time totals, as a worker's metrics flush adds them."""
    async with session_factory() as session:
        await MetricsRepository(session).upsert_totals(PROXY_WINDOWS, {proxy_id: delta})
        await session.commit()


async def _write_window(redis_client: RedisClient, proxy_id: str, delta: MetricDelta) -> None:
    """The proxy's metrics window as gateways leave it in Redis, not flushed yet."""
    await redis_client.client.hset(PROXY_METRICS_KEY.format(proxy_id=proxy_id), mapping=delta.to_dict())
