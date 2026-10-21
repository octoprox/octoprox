# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for connector endpoints, on an api process: writes through the connector service, reads from the read model.

The app runs no worker, so a write that starts work is checked by the job
it asks for (its key on its kind's job stream), not by the job's effect.
"""

import time
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import update
from starlette.testclient import TestClient

from api.core.roles import JobKind
from api.db.models import ConnectorModel
from api.db.redis import JOB_STREAM_KEY, QUARANTINE_KEY
from api.db.repository import ProxyRepository
from api.db.session import SessionFactory
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
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


class TestConnectorEndpoints:
    """Tests for connector CRUD endpoints."""

    def test_list_connectors_empty(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
    ) -> None:
        """Test listing connectors when none exist (only credential, no connector)."""
        project_id = created_project["id"]
        response = authenticated_client.get(f"/api/v1/projects/{project_id}/connectors")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["connectors"] == []

    def test_list_connectors_order_is_stable(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Listing order is creation order, whatever the order Postgres returns rows in.

        Timestamps are pinned equal to exercise the id tie-break, which is what
        keeps bulk-imported entries ordered identically on every api process.
        """
        project_id = created_project["id"]

        created_ids = []
        for i in range(3):
            connector_data = sample_connector_data.copy()
            connector_data["credential_id"] = created_credential["id"]
            connector_data["name"] = f"Connector {i}"
            response = authenticated_client.post(
                f"/api/v1/projects/{project_id}/connectors",
                json=connector_data,
            )
            assert response.status_code == 201
            created_ids.append(response.json()["id"])

        def listed_ids() -> list[str]:
            response = authenticated_client.get(f"/api/v1/projects/{project_id}/connectors")
            assert response.status_code == 200
            return [c["id"] for c in response.json()["connectors"]]

        assert listed_ids() == created_ids

        pinned = datetime(2026, 1, 1)
        on_app_loop(authenticated_client, _pin_created_at, api_runtime(authenticated_client).session_factory, created_ids, pinned)

        assert listed_ids() == sorted(created_ids)

    def test_list_connectors_project_not_found(
        self,
            authenticated_client: TestClient,
    ) -> None:
        """Test listing connectors for non-existent project."""
        response = authenticated_client.get("/api/v1/projects/non-existent/connectors")

        assert response.status_code == 404

    def test_create_connector(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test creating a new connector."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == connector_data["name"]
        assert data["credential_id"] == created_credential["id"]
        assert data["project_id"] == project_id
        assert "id" in data

    def test_create_connector_credential_not_found(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test creating connector with non-existent credential."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = "non-existent"

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )

        assert response.status_code == 404
        assert "Credential" in response.json()["detail"]

    def test_get_connector(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """Test getting a specific connector."""
        project_id = created_project["id"]
        connector_id = created_connector["id"]

        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == connector_id
        assert data["credential_name"] is not None

    def test_get_connector_not_found(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
    ) -> None:
        """Test getting a non-existent connector."""
        project_id = created_project["id"]
        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/connectors/non-existent"
        )

        assert response.status_code == 404

    def test_update_connector(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """Test updating a connector."""
        project_id = created_project["id"]
        connector_id = created_connector["id"]

        response = authenticated_client.patch(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}",
            json={"name": "Updated Connector", "enabled": False},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Connector"
        assert data["enabled"] is False

    def test_duplicate_names_are_rejected_within_a_project(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """The per-project unique index on lower(name) is what refuses the duplicate; the route makes it a 400."""
        project_id = created_project["id"]
        url = f"/api/v1/projects/{project_id}/connectors"
        body = {**sample_connector_data, "credential_id": created_connector["credential_id"]}
        duplicate = authenticated_client.post(url, json=body)
        assert duplicate.status_code == 400 and "already exists" in duplicate.json()["detail"]
        assert authenticated_client.post(url, json={**body, "name": body["name"].upper()}).status_code == 400

        other = authenticated_client.post(url, json={**body, "name": "Second"})
        assert other.status_code == 201, other.text
        rename = authenticated_client.patch(f"{url}/{other.json()['id']}", json={"name": body["name"]})
        assert rename.status_code == 400
        # The refused rename left the stored connector as it was.
        assert authenticated_client.get(f"{url}/{other.json()['id']}").json()["name"] == "Second"
        assert authenticated_client.patch(f"{url}/{other.json()['id']}", json={"name": "second"}).status_code == 200

    def test_delete_connector(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """Test deleting a connector."""
        project_id = created_project["id"]
        connector_id = created_connector["id"]

        response = authenticated_client.delete(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}"
        )

        assert response.status_code == 204

        # Verify it's deleted
        get_response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}"
        )
        assert get_response.status_code == 404

    def test_create_connector_with_whitelist(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test creating a connector with a domain whitelist."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]
        connector_data["routing_config"] = {"domain_whitelist": ["example.com", "test.org"]}

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["routing_config"] == {"domain_whitelist": ["example.com", "test.org"]}

    def test_create_connector_with_blacklist(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test creating a connector with a domain blacklist."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]
        connector_data["routing_config"] = {"domain_blacklist": ["blocked.com"]}

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["routing_config"] == {"domain_blacklist": ["blocked.com"]}

    def test_create_connector_both_lists_rejected(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test that creating a connector with both whitelist and blacklist is rejected."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]
        connector_data["routing_config"] = {
            "domain_whitelist": ["a.com"],
            "domain_blacklist": ["b.com"],
        }

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )

        assert response.status_code == 422

    def test_create_connector_empty_routing_config(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test creating a connector with empty routing config."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]
        connector_data["routing_config"] = {}

        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )

        assert response.status_code == 201
        data = response.json()
        assert data["routing_config"] == {}

    def test_update_connector_routing_config(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """Test updating a connector's routing config."""
        project_id = created_project["id"]
        connector_id = created_connector["id"]

        response = authenticated_client.patch(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}",
            json={"routing_config": {"domain_whitelist": ["updated.com"]}},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["routing_config"] == {"domain_whitelist": ["updated.com"]}

    def test_update_connector_routing_config_both_lists_rejected(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_connector: dict[str, Any],
    ) -> None:
        """Test that updating with both whitelist and blacklist is rejected."""
        project_id = created_project["id"]
        connector_id = created_connector["id"]

        response = authenticated_client.patch(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}",
            json={"routing_config": {
                "domain_whitelist": ["a.com"],
                "domain_blacklist": ["b.com"],
            }},
        )

        assert response.status_code == 422

    def test_get_connector_includes_routing_config(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test that GET connector response includes routing_config."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]
        connector_data["routing_config"] = {"domain_blacklist": ["blocked.com"]}

        create_resp = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )
        assert create_resp.status_code == 201
        connector_id = create_resp.json()["id"]

        response = authenticated_client.get(
            f"/api/v1/projects/{project_id}/connectors/{connector_id}"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["routing_config"] == {"domain_blacklist": ["blocked.com"]}

    def test_list_connectors_includes_routing_config(
        self,
        authenticated_client: TestClient,
        created_project: dict[str, Any],
        created_credential: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        """Test that list connectors includes routing_config."""
        project_id = created_project["id"]
        connector_data = sample_connector_data.copy()
        connector_data["credential_id"] = created_credential["id"]
        connector_data["routing_config"] = {"domain_whitelist": ["example.com"]}

        create_resp = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json=connector_data,
        )
        assert create_resp.status_code == 201

        response = authenticated_client.get(f"/api/v1/projects/{project_id}/connectors")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["connectors"][0]["routing_config"] == {"domain_whitelist": ["example.com"]}

    def test_get_connector_options(
        self,
        authenticated_client: TestClient,
    ) -> None:
        """Test getting connector options."""
        response = authenticated_client.get("/api/v1/connector-options")

        assert response.status_code == 200
        # Response should contain available options
        data = response.json()
        assert isinstance(data, dict)


class TestConnectorJobs:
    """What a connector write asks the workers for, and the Redis state it changes."""

    def _credential(self, client: TestClient, project: dict[str, Any], type_: str, config: dict[str, Any]) -> str:
        response = client.post(
            f"/api/v1/projects/{project['id']}/credentials",
            json={"name": f"{type_}-{uuid.uuid4().hex[:6]}", "type": type_, "config": config},
        )
        assert response.status_code == 201, response.text
        credential_id: str = response.json()["id"]
        return credential_id

    def _connector(
        self, client: TestClient, project: dict[str, Any], credential_id: str, config: dict[str, Any], **fields: Any
    ) -> dict[str, Any]:
        response = client.post(
            f"/api/v1/projects/{project['id']}/connectors",
            json={"name": f"connector-{uuid.uuid4().hex[:6]}", "credential_id": credential_id, "config": config, **fields},
        )
        assert response.status_code == 201, response.text
        data: dict[str, Any] = response.json()
        return data

    def test_a_syncing_provider_is_synced_on_create_and_edit(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        credential_id = self._credential(authenticated_client, created_project, "oxylabs", OXYLABS_CREDENTIAL)
        connector = self._connector(authenticated_client, created_project, credential_id, OXYLABS_POOL)

        assert connector["proxy_count"] == 0
        assert connector["id"] in queued_jobs(authenticated_client, JobKind.SYNC)
        assert connector["id"] not in queued_jobs(authenticated_client, JobKind.SCALE)

        # Taken by a worker meanwhile; an edit asks again.
        redis = api_runtime(authenticated_client).redis_client
        on_app_loop(authenticated_client, redis.client.delete, JOB_STREAM_KEY.format(kind=JobKind.SYNC))
        url = f"/api/v1/projects/{created_project['id']}/connectors/{connector['id']}"
        assert authenticated_client.patch(url, json={"config": {**OXYLABS_POOL, "num_proxies": 3}}).status_code == 200
        assert connector["id"] in queued_jobs(authenticated_client, JobKind.SYNC)

    def test_a_static_connector_asks_for_nothing(
        self, authenticated_client: TestClient, created_connector: dict[str, Any]
    ) -> None:
        assert created_connector["id"] not in queued_jobs(authenticated_client, JobKind.SYNC)
        assert created_connector["id"] not in queued_jobs(authenticated_client, JobKind.SCALE)

    def test_a_cloud_connector_is_scaled_on_create_and_drained_on_delete(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        credential_id = self._credential(authenticated_client, created_project, "aws", AWS_CREDENTIAL)
        connector = self._connector(authenticated_client, created_project, credential_id, AWS_CONNECTOR)
        assert connector["target"]["total"] == 4
        assert connector["id"] in queued_jobs(authenticated_client, JobKind.SCALE)
        proxy = _add_proxy(authenticated_client, connector["id"], ProxyStatus.HEALTHY)

        url = f"/api/v1/projects/{created_project['id']}/connectors/{connector['id']}"
        redis = api_runtime(authenticated_client).redis_client
        on_app_loop(authenticated_client, redis.client.delete, JOB_STREAM_KEY.format(kind=JobKind.SCALE))
        assert authenticated_client.delete(url).status_code == 204

        # Not deleted yet: disabled, its instances terminating, and the scale job asked to finish it.
        data = authenticated_client.get(url).json()
        assert data["enabled"] is False and data["proxy_count"] == 1
        assert connector["id"] in queued_jobs(authenticated_client, JobKind.SCALE)
        stored = on_app_loop(authenticated_client, _stored_status, api_runtime(authenticated_client).session_factory, proxy.id)
        assert stored == ProxyStatus.TERMINATING

    def test_a_rate_limit_change_lifts_the_connectors_quarantines(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any]
    ) -> None:
        proxy = _add_proxy(authenticated_client, created_connector["id"], ProxyStatus.HEALTHY)
        redis = api_runtime(authenticated_client).redis_client
        on_app_loop(authenticated_client, redis.client.zadd, QUARANTINE_KEY, {proxy.id: time.time() + 600})
        url = f"/api/v1/projects/{created_project['id']}/connectors/{created_connector['id']}"

        assert authenticated_client.patch(url, json={"name": "Renamed"}).status_code == 200
        assert on_app_loop(authenticated_client, redis.client.zscore, QUARANTINE_KEY, proxy.id) is not None

        response = authenticated_client.patch(
            url,
            json={"rate_limit_config": {
                "max_requests": 10, "window_seconds": 60, "quarantine_seconds_min": 30, "quarantine_seconds_max": 60,
            }},
        )
        assert response.status_code == 200, response.text
        assert on_app_loop(authenticated_client, redis.client.zscore, QUARANTINE_KEY, proxy.id) is None


class TestConnectorProjectScope:
    """A connector belongs to one project: under another project's path it is not found."""

    def test_other_projects_connector_is_not_found(
        self,
        authenticated_client: TestClient,
        created_connector: dict[str, Any],
        sample_project_data: dict[str, Any],
    ) -> None:
        other = authenticated_client.post(
            "/api/v1/projects",
            json={**sample_project_data, "name": f"{sample_project_data['name']} B", "username": f"{sample_project_data['username']}_b"},
        )
        assert other.status_code == 201, other.text
        url = f"/api/v1/projects/{other.json()['id']}/connectors/{created_connector['id']}"

        assert authenticated_client.get(url).status_code == 404
        assert authenticated_client.patch(url, json={"name": "Taken over"}).status_code == 404
        assert authenticated_client.post(f"{url}/traffic/reset").status_code == 404
        assert authenticated_client.get(f"{url}/metrics/history").status_code == 404
        assert authenticated_client.delete(url).status_code == 404
        mine = f"/api/v1/projects/{created_connector['project_id']}/connectors/{created_connector['id']}"
        assert authenticated_client.get(mine).json()["name"] == created_connector["name"]

    def test_a_credential_of_another_project_is_refused(
        self,
        authenticated_client: TestClient,
        created_credential: dict[str, Any],
        sample_project_data: dict[str, Any],
        sample_connector_data: dict[str, Any],
    ) -> None:
        other = authenticated_client.post(
            "/api/v1/projects",
            json={**sample_project_data, "name": f"{sample_project_data['name']} B", "username": f"{sample_project_data['username']}_b"},
        )
        assert other.status_code == 201, other.text

        response = authenticated_client.post(
            f"/api/v1/projects/{other.json()['id']}/connectors",
            json={**sample_connector_data, "credential_id": created_credential["id"]},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "Credential does not belong to this project"


class TestConnectorRoleAccess:
    """Tests for role-based access control on connector endpoints."""

    def test_viewer_cannot_create_connector(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot create connectors."""
        response = viewer_client.post(
            "/api/v1/projects/some-project-id/connectors",
            json={
                "name": "Viewer Connector",
                "credential_id": "some-credential-id",
                "config": {},
                "enabled": True,
            },
        )
        assert response.status_code == 403

    def test_viewer_cannot_update_connector(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot update connectors."""
        response = viewer_client.patch(
            "/api/v1/projects/some-project-id/connectors/some-connector-id",
            json={"name": "Updated"},
        )
        assert response.status_code == 403

    def test_viewer_cannot_delete_connector(self, viewer_client: TestClient) -> None:
        """Test that viewers cannot delete connectors."""
        response = viewer_client.delete(
            "/api/v1/projects/some-project-id/connectors/some-connector-id"
        )
        assert response.status_code == 403



class TestConnectorWeight:
    """routing_config.weight through the API."""

    def _create(self, client: TestClient, project: dict[str, Any], credential: dict[str, Any], base: dict[str, Any], routing: dict[str, Any]):
        data = base.copy()
        data["credential_id"] = credential["id"]
        data["routing_config"] = routing
        return client.post(f"/api/v1/projects/{project['id']}/connectors", json=data)

    def test_create_with_weight(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any], sample_connector_data: dict[str, Any],
    ) -> None:
        response = self._create(authenticated_client, created_project, created_credential, sample_connector_data, {"weight": 3, "domain_blacklist": ["blocked.com"]})
        assert response.status_code == 201
        assert response.json()["routing_config"] == {"weight": 3, "domain_blacklist": ["blocked.com"]}

    def test_default_weight_is_omitted(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any], sample_connector_data: dict[str, Any],
    ) -> None:
        response = self._create(authenticated_client, created_project, created_credential, sample_connector_data, {"weight": 1})
        assert response.status_code == 201
        assert response.json()["routing_config"] == {}

    def test_invalid_weight_rejected(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any], sample_connector_data: dict[str, Any],
    ) -> None:
        for weight in (0, 101, "heavy"):
            response = self._create(authenticated_client, created_project, created_credential, sample_connector_data, {"weight": weight})
            assert response.status_code == 422, weight

    def test_update_weight(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_connector: dict[str, Any],
    ) -> None:
        url = f"/api/v1/projects/{created_project['id']}/connectors/{created_connector['id']}"
        response = authenticated_client.patch(url, json={"routing_config": {"weight": 5}})
        assert response.status_code == 200
        assert response.json()["routing_config"] == {"weight": 5}
        response = authenticated_client.patch(url, json={"routing_config": {}})
        assert response.status_code == 200
        assert response.json()["routing_config"] == {}


async def _pin_created_at(session_factory: SessionFactory, connector_ids: list[str], at: datetime) -> None:
    """Give the connectors one creation time, as a bulk import can."""
    async with session_factory() as session:
        await session.execute(update(ConnectorModel).where(ConnectorModel.id.in_(connector_ids)).values(created_at=at))
        await session.commit()


def _add_proxy(client: TestClient, connector_id: str, status: ProxyStatus) -> Proxy:
    """A proxy stored with ``status``, as a job adds one (the proxies routes are another module's)."""
    proxy = Proxy(host=f"{uuid.uuid4().hex[:8]}.example.com", port=8080, protocol=ProxyProtocol.HTTP,
                  connector_id=connector_id, status=status)
    on_app_loop(client, api_runtime(client).proxy_service.add, proxy)
    return proxy


async def _stored_status(session_factory: SessionFactory, proxy_id: str) -> ProxyStatus | None:
    """The status column of a proxy row."""
    async with session_factory() as session:
        proxy = await ProxyRepository(session).get_by_id(proxy_id)
    return proxy.status if proxy is not None else None
