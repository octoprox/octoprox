# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A project's entities' History through the API: each write recorded by the user who made it, and read back by anyone
who may read the entity, a secret only ever set, changed or cleared.

Bright Data's account check is answered by a mock of its API behind the registry's SDK runtime, so a credential and a
connector holding secrets are written as the UI writes them.
"""

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from api.models.audit import MASK
from api.providers.sdk.egress import EgressPolicy
from tests.conftest import api_runtime

# The secrets the writes send, none of which a History may show.
TOKEN = "bright-token-secret"
ZONE = {"zone_name": "residential_1", "zone_password": "zone-secret-1", "proxy_type": "residential"}


def _bright_data(request: httpx.Request) -> httpx.Response:
    """Bright Data's account status call: an active account, and the customer id the credential captures."""
    return httpx.Response(200, json={"status": "active", "customer": "c_777"})


@pytest.fixture
def vendor(authenticated_client: TestClient) -> Iterator[TestClient]:
    """The api's provider registry asking the mock instead of Bright Data."""
    registry = api_runtime(authenticated_client).provider_registry
    original = registry._runtime
    registry._runtime = replace(
        original,
        egress_policy=EgressPolicy(allow_http=True, allow_private=True, pin_dns=False),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_bright_data)),
    )
    yield authenticated_client
    registry._runtime = original


def _history(client: TestClient, entity_type: str, entity_id: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    """The entity's entries, oldest first, as the user ``headers`` names reads them."""
    response = client.get(f"/api/v1/audit/{entity_type}/{entity_id}", headers=headers)
    assert response.status_code == 200, response.text
    assert TOKEN not in response.text and "zone-secret" not in response.text
    return list(reversed(response.json()["entries"]))


def _sent(response: httpx.Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


class TestEntityHistory:
    def test_a_viewer_reads_what_an_editor_changed_with_the_secrets_masked(
        self,
        vendor: TestClient,
        created_project: dict[str, Any],
        editor_auth_headers: dict[str, str],
        viewer_auth_headers: dict[str, str],
    ) -> None:
        client, project_id = vendor, created_project["id"]
        base = f"/api/v1/projects/{project_id}"
        credential = _sent(
            client.post(
                f"{base}/credentials",
                json={"name": "Bright", "type": "brightdata", "config": {"token": TOKEN}},
                headers=editor_auth_headers,
            ),
            201,
        )
        created = {"name": "Bright", "credential_id": credential["id"], "config": ZONE}
        connector = _sent(client.post(f"{base}/connectors", json=created, headers=editor_auth_headers), 201)
        edit = {"config": {**ZONE, "zone_password": "zone-secret-2"}, "routing_config": {"weight": 3}}
        _sent(
            client.patch(
                f"{base}/connectors/{connector['id']}",
                json=edit,
                headers={**editor_auth_headers, "X-Request-ID": "edit-1"},
            )
        )

        first, last = _history(client, "connector", connector["id"], viewer_auth_headers)
        assert (last["action"], last["actor"], last["actor_id"], last["request_id"], last["project_id"]) == (
            "updated",
            "testeditor",
            "test-editor-id",
            "edit-1",
            project_id,
        )
        assert last["changes"] == {"config.zone_password": [MASK, MASK], "routing_config.weight": [None, 3]}
        assert (first["action"], first["entity_name"], first["changes"]["config.zone_password"]) == (
            "created",
            "Bright",
            [None, MASK],
        )
        [stored] = _history(client, "credential", credential["id"], viewer_auth_headers)
        assert (stored["changes"]["config.token"], stored["changes"]["config.customer_id"]) == ([None, MASK], [None, "c_777"])

    def test_each_write_of_a_projects_entities_is_recorded_by_the_user_who_made_it(
        self,
        authenticated_client: TestClient,
        sample_project_data: dict[str, Any],
        editor_auth_headers: dict[str, str],
        viewer_auth_headers: dict[str, str],
    ) -> None:
        client, editor = authenticated_client, editor_auth_headers
        project = _sent(client.post("/api/v1/projects", json=sample_project_data, headers=editor), 201)
        base = f"/api/v1/projects/{project['id']}"
        _sent(client.patch(base, json={"description": "Edited"}, headers=editor))
        credential = _sent(
            client.post(f"{base}/credentials", json={"name": "Static", "type": "static_proxy_provider"}, headers=editor),
            201,
        )
        _sent(client.patch(f"{base}/credentials/{credential['id']}", json={"name": "Renamed"}, headers=editor))
        connector = _sent(
            client.post(f"{base}/connectors", json={"name": "ISP", "credential_id": credential["id"]}, headers=editor),
            201,
        )
        _sent(client.patch(f"{base}/connectors/{connector['id']}", json={"enabled": False}, headers=editor))
        _sent(client.post(f"{base}/connectors/{connector['id']}/traffic/reset", headers=editor))
        _sent(client.put(f"{base}/countries/DE", json={"connectors": {connector["id"]: "off"}}, headers=editor))
        proxy = _sent(
            client.post(f"{base}/proxies", json={"host": "10.0.0.1", "port": 8080, "connector_id": connector["id"]}, headers=editor),
            201,
        )
        _sent(client.patch(f"{base}/proxies/{proxy['id']}", json={"port": 3128}, headers=editor))
        uploaded = _sent(
            client.post(
                f"{base}/proxies/upload",
                files={"file": ("proxies.txt", b"http://10.0.0.2:8080")},
                data={"connector_id": connector["id"]},
                headers=editor,
            ),
            201,
        )
        _sent(client.delete(f"{base}/proxies/{proxy['id']}", headers=editor), 204)
        _sent(client.delete(f"{base}/connectors/{connector['id']}", headers=editor), 204)
        _sent(client.delete(f"{base}/credentials/{credential['id']}", headers=editor), 204)
        _sent(
            client.request("DELETE", base, json={"confirmation": "permanently delete"}, headers=editor),
            204,
        )

        def actions(entity_type: str, entity_id: str) -> list[tuple[str, str]]:
            return [(e["action"], e["actor"]) for e in _history(client, entity_type, entity_id, viewer_auth_headers)]

        editor_did = [("created", "testeditor"), ("updated", "testeditor"), ("deleted", "testeditor")]
        assert actions("project", project["id"]) == editor_did
        assert actions("credential", credential["id"]) == editor_did
        assert actions("connector", connector["id"]) == [
            ("created", "testeditor"),
            ("updated", "testeditor"),
            ("traffic_reset", "testeditor"),
            ("updated", "testeditor"),
            ("deleted", "testeditor"),
        ]
        assert actions("proxy", proxy["id"]) == editor_did
        [upload] = _history(client, "proxy", uploaded["proxies"][0]["id"], viewer_auth_headers)
        assert (upload["action"], upload["details"]) == ("created", {"via": "upload"})
