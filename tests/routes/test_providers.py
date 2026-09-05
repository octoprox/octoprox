# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the provider catalog and admin descriptor endpoints."""

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, text
from starlette.testclient import TestClient

from api.core.config import Settings
from api.providers.sdk.egress import EgressPolicy

ACME_SPEC: dict[str, Any] = {
    "id": "acme",
    "name": "Acme Proxies",
    "description": "Test vendor",
    "credential_fields": [
        {"key": "api_key", "label": "API key", "type": "password", "secret": True, "required": True},
        {"key": "username", "label": "Username", "required": True},
        {"key": "password", "label": "Password", "type": "password", "secret": True, "required": True},
    ],
    "connector_fields": [
        {"key": "num_proxies", "label": "Proxies", "type": "number", "default": 1, "min": 1, "required": True},
        {"key": "region", "label": "Region", "type": "select", "options_from": "regions"},
    ],
    "validation": {
        "call": {"url": "https://api.acme.test/me", "headers": {"Authorization": "Bearer {credential.api_key}"}},
        "success": "active",
        "capture": {"account_id": "id"},
        "error_message": "Bad Acme key",
    },
    "options": {
        "regions": {
            "call": {"url": "https://api.acme.test/regions", "headers": {"Authorization": "Bearer {credential.api_key}"}},
            "items": "regions",
            "value": "code",
            "label": "name",
        }
    },
    "proxy_types": [
        {
            "key": "res",
            "label": "Residential",
            "mode": "session",
            "host": "gw.acme.test",
            "port": 9000,
            "username": {"parts": [{"text": "{credential.username}"}, {"text": "r-{connector.region}", "when": {"field": "connector.region"}}, {"text": "s-{session_id}"}]},
            "password": "{credential.password}",
        }
    ],
}


def _acme_api(request: httpx.Request) -> httpx.Response:
    if request.headers.get("Authorization") != "Bearer good":
        return httpx.Response(401, json={"detail": "nope"})
    if request.url.path == "/me":
        return httpx.Response(200, json={"active": True, "id": "acct-9"})
    if request.url.path == "/regions":
        return httpx.Response(200, json={"regions": [{"code": "eu", "name": "Europe"}, {"code": "us", "name": "US"}]})
    return httpx.Response(404)


@pytest.fixture(autouse=True)
def clean_custom_providers(test_settings: Settings) -> Iterator[None]:
    """Custom descriptors are global rows; wipe them so tests do not see each other."""
    yield
    engine = create_engine(test_settings.database_url_sync)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM provider_audit_log"))
        conn.execute(text("DELETE FROM provider_descriptors"))
    engine.dispose()


@pytest.fixture
def mocked_vendor(authenticated_client: TestClient) -> Iterator[TestClient]:
    """Point the registry's SDK runtime at a mock Acme API and relax egress."""
    registry = authenticated_client.app.state.proxy_manager.provider_registry
    original = registry._runtime
    registry._runtime = replace(
        original,
        egress_policy=EgressPolicy(allow_http=True, allow_private=True, pin_dns=False),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_acme_api)),
    )
    yield authenticated_client
    registry._runtime = original


def _create_acme(client: TestClient, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    response = client.post("/api/v1/providers", json={"spec": spec or ACME_SPEC, "confirmed_hosts": ["api.acme.test"]})
    assert response.status_code == 201, response.text
    return response.json()


class TestCatalog:
    def test_list_includes_code_and_builtin_types(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.get("/api/v1/providers")
        assert response.status_code == 200
        data = response.json()
        by_id = {p["id"]: p for p in data["providers"]}
        assert {"static_proxy_provider", "aws", "oxylabs", "brightdata", "webshare"} <= set(by_id)
        assert by_id["aws"]["kind"] == "code" and by_id["aws"]["cloud"] is True
        oxylabs = by_id["oxylabs"]
        assert oxylabs["kind"] == "descriptor" and oxylabs["source"] == "builtin" and oxylabs["editable"] is False
        assert {f["key"] for f in oxylabs["credential_fields"]} == {"proxy_type", "username", "password"}
        assert oxylabs["proxy_type_field"] == "credential.proxy_type"
        assert by_id["brightdata"]["egress_hosts"] == ["api.brightdata.com"]
        assert by_id["brightdata"]["has_validation"] is True
        bd_fields = {f["key"]: f for f in by_id["brightdata"]["connector_fields"]}
        assert bd_fields["zone_name"]["depends_on"] == []
        assert bd_fields["country_code"]["depends_on"] == ["zone_name"]
        assert bd_fields["proxy_type"]["readonly"] is True
        assert bd_fields["num_proxies"]["max_from_option"] == [{"field": "country_code", "extra": "count"}, {"field": "zone_name", "extra": "total_ips"}]
        assert data["presets"]["countries"][1]["value"] == "US"
        assert data["countries"][0]["code"] == ""

    def test_viewer_can_read_catalog(self, viewer_client: TestClient) -> None:
        assert viewer_client.get("/api/v1/providers").status_code == 200
        detail = viewer_client.get("/api/v1/providers/brightdata")
        assert detail.status_code == 200 and detail.json()["spec"]["id"] == "brightdata"
        assert viewer_client.get("/api/v1/providers/nope").status_code == 404

    def test_usage_counts(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        project_id = created_project["id"]
        response = authenticated_client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json={"name": "oxy", "type": "oxylabs", "config": {"proxy_type": "residential", "username": "u", "password": "p"}},
        )
        assert response.status_code == 201, response.text
        oxylabs = authenticated_client.get("/api/v1/providers/oxylabs").json()
        assert oxylabs["credential_count"] >= 1


class TestAuthoring:
    def test_validate_reports_hosts_errors_and_warnings(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.post("/api/v1/providers/validate", json={"spec": ACME_SPEC})
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True and data["egress_hosts"] == ["api.acme.test"]
        assert data["gateway_hosts"] == ["gw.acme.test"] and data["yaml"].startswith("id: acme")

        bad = dict(ACME_SPEC, validation={"call": {"url": "https://169.254.169.254/latest"}})
        data = authenticated_client.post("/api/v1/providers/validate", json={"spec": bad}).json()
        assert data["valid"] is False and any("not publicly routable" in e for e in data["errors"])

        shadow = dict(ACME_SPEC, id="oxylabs")
        data = authenticated_client.post("/api/v1/providers/validate", json={"spec": shadow}).json()
        assert data["valid"] is False and any("builtin" in e for e in data["errors"])

        malformed = authenticated_client.post("/api/v1/providers/validate", json={"spec": {"id": "x"}}).json()
        assert malformed["valid"] is False and malformed["errors"]

    def test_create_requires_host_confirmation(self, authenticated_client: TestClient) -> None:
        response = authenticated_client.post("/api/v1/providers", json={"spec": ACME_SPEC})
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["unconfirmed_hosts"] == ["api.acme.test"]

    def test_create_get_update_delete(self, authenticated_client: TestClient) -> None:
        created = _create_acme(authenticated_client)
        assert created["id"] == "acme" and created["source"] == "custom" and created["editable"] is True
        assert created["spec"]["name"] == "Acme Proxies"

        listed = {p["id"] for p in authenticated_client.get("/api/v1/providers").json()["providers"]}
        assert "acme" in listed

        duplicate = authenticated_client.post("/api/v1/providers", json={"spec": ACME_SPEC, "confirmed_hosts": ["api.acme.test"]})
        assert duplicate.status_code == 409

        renamed = dict(ACME_SPEC, name="Acme Renamed")
        updated = authenticated_client.put("/api/v1/providers/acme", json={"spec": renamed})
        assert updated.status_code == 200, updated.text
        assert updated.json()["name"] == "Acme Renamed" and updated.json()["version"] == 1
        assert updated.json()["spec"]["name"] == "Acme Renamed"

        # Adding a new egress host needs confirmation again.
        new_host = dict(renamed, options={**renamed["options"], "extra": {"call": {"url": "https://other.acme.test/x"}, "value": "id"}})
        response = authenticated_client.put("/api/v1/providers/acme", json={"spec": new_host})
        assert response.status_code == 409 and response.json()["detail"]["unconfirmed_hosts"] == ["other.acme.test"]
        response = authenticated_client.put("/api/v1/providers/acme", json={"spec": new_host, "confirmed_hosts": ["api.acme.test", "other.acme.test"]})
        assert response.status_code == 200

        audit = authenticated_client.get("/api/v1/providers/acme/audit").json()
        assert [e["action"] for e in audit["entries"]] == ["updated", "updated", "created"]
        assert audit["entries"][0]["hosts_changed"] is True and audit["entries"][2]["actor"] == "testadmin"

        export = authenticated_client.get("/api/v1/providers/acme/export")
        assert export.status_code == 200 and export.text.startswith("id: acme")

        assert authenticated_client.delete("/api/v1/providers/acme").status_code == 204
        assert authenticated_client.get("/api/v1/providers/acme").status_code == 404

    def test_builtins_are_read_only(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.put("/api/v1/providers/oxylabs", json={"enabled": False}).status_code == 409
        assert authenticated_client.delete("/api/v1/providers/oxylabs").status_code == 409
        export = authenticated_client.get("/api/v1/providers/oxylabs/export")
        assert export.status_code == 200 and "pr.oxylabs.io" in export.text

    def test_import_yaml(self, authenticated_client: TestClient) -> None:
        yaml_text = authenticated_client.post("/api/v1/providers/validate", json={"spec": ACME_SPEC}).json()["yaml"]
        response = authenticated_client.post("/api/v1/providers/import", json={"yaml": yaml_text, "confirmed_hosts": ["api.acme.test"]})
        assert response.status_code == 201, response.text
        replaced = authenticated_client.post(
            "/api/v1/providers/import",
            json={"yaml": yaml_text.replace("name: Acme Proxies", "name: Acme Two"), "confirmed_hosts": ["api.acme.test"], "replace": True},
        )
        assert replaced.status_code == 201 and replaced.json()["name"] == "Acme Two"
        assert authenticated_client.post("/api/v1/providers/import", json={"yaml": "id: [broken"}).status_code == 422

    def test_disable_removes_from_registry_but_keeps_record(self, authenticated_client: TestClient) -> None:
        _create_acme(authenticated_client)
        response = authenticated_client.put("/api/v1/providers/acme", json={"enabled": False})
        assert response.status_code == 200
        assert "acme" not in {p["id"] for p in authenticated_client.get("/api/v1/providers").json()["providers"]}

    def test_admin_only(
        self, authenticated_client: TestClient, editor_auth_headers: dict[str, str], viewer_auth_headers: dict[str, str]
    ) -> None:
        client = authenticated_client
        assert client.post("/api/v1/providers", json={"spec": ACME_SPEC}, headers=editor_auth_headers).status_code == 403
        assert client.post("/api/v1/providers/validate", json={"spec": ACME_SPEC}, headers=editor_auth_headers).status_code == 403
        assert client.delete("/api/v1/providers/oxylabs", headers=editor_auth_headers).status_code == 403
        assert client.get("/api/v1/providers/oxylabs/export", headers=viewer_auth_headers).status_code == 403
        # Editors may resolve options; viewers may not.
        assert client.post("/api/v1/providers/oxylabs/options/x", json={}, headers=viewer_auth_headers).status_code == 403


class TestUsingACustomProvider:
    def test_credentials_and_connectors_flow(self, mocked_vendor: TestClient, created_project: dict[str, Any]) -> None:
        client = mocked_vendor
        _create_acme(client)
        project_id = created_project["id"]

        bad = client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json={"name": "acme", "type": "acme", "config": {"api_key": "wrong", "username": "u", "password": "p"}},
        )
        assert bad.status_code == 400 and "Bad Acme key" in bad.json()["detail"]

        missing = client.post(f"/api/v1/projects/{project_id}/credentials", json={"name": "acme", "type": "acme", "config": {"api_key": "good"}})
        assert missing.status_code == 422 and "Username is required" in missing.json()["detail"]

        created = client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json={"name": "acme", "type": "acme", "config": {"api_key": "good", "username": "u", "password": "p"}},
        )
        assert created.status_code == 201, created.text
        credential = created.json()
        assert credential["config"]["account_id"] == "acct-9"  # captured by validation

        options = client.post("/api/v1/providers/acme/options/regions", json={"credential_id": credential["id"]})
        assert options.status_code == 200
        assert [o["value"] for o in options.json()["options"]] == ["eu", "us"]
        inline = client.post("/api/v1/providers/acme/options/regions", json={"credential_config": {"api_key": "good"}})
        assert inline.status_code == 200
        assert client.post("/api/v1/providers/acme/options/nope", json={"credential_id": credential["id"]}).status_code == 404

        connector = client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json={"name": "acme-eu", "credential_id": credential["id"], "config": {"num_proxies": "2", "region": "eu"}},
        )
        assert connector.status_code == 201, connector.text
        assert connector.json()["config"] == {"num_proxies": 2, "region": "eu"}
        assert connector.json()["credential_type"] == "acme"

        # Updating the credential without changing fields keeps the captured account id.
        patched = client.patch(f"/api/v1/projects/{project_id}/credentials/{credential['id']}", json={"config": {"api_key": "good", "username": "u", "password": "p"}})
        assert patched.status_code == 200 and patched.json()["config"]["account_id"] == "acct-9"

        # A provider in use cannot be deleted.
        assert client.delete("/api/v1/providers/acme").status_code == 400

        test = client.post("/api/v1/providers/acme/test", json={"action": "validate", "credential_config": {"api_key": "good"}})
        assert test.status_code == 200 and test.json()["ok"] is True
        assert test.json()["result"] == {"captured": {"account_id": "acct-9"}}
        assert test.json()["traces"][0]["headers"]["Authorization"] == "***"

        draft = dict(ACME_SPEC, id="acme_draft")
        test = client.post("/api/v1/providers/acme_draft/test", json={"action": "options", "option_name": "regions", "credential_config": {"api_key": "good"}, "spec": draft})
        assert test.status_code == 200 and test.json()["message"] == "2 option(s)"

    def test_test_endpoint_rejects_private_draft_hosts(self, authenticated_client: TestClient) -> None:
        draft = dict(ACME_SPEC, id="acme_bad", validation={"call": {"url": "https://10.0.0.1/steal"}})
        response = authenticated_client.post("/api/v1/providers/acme_bad/test", json={"action": "validate", "spec": draft})
        assert response.status_code == 422

    def test_legacy_oxylabs_credential_and_connector_still_work(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        """Rows shaped like the pre-SDK Oxylabs provider are accepted by the built-in descriptor."""
        project_id = created_project["id"]
        credential = authenticated_client.post(
            f"/api/v1/projects/{project_id}/credentials",
            json={"name": "oxy", "type": "oxylabs", "config": {"proxy_type": "residential", "username": "u", "password": "p"}},
        )
        assert credential.status_code == 201, credential.text
        connector = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json={"name": "oxy-us", "credential_id": credential.json()["id"], "config": {"num_proxies": 2, "country_code": "US", "session_duration_minutes": 10}},
        )
        assert connector.status_code == 201, connector.text
        assert connector.json()["config"] == {"num_proxies": 2, "country_code": "US", "session_duration_minutes": 10}
        bad = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json={"name": "oxy-bad", "credential_id": credential.json()["id"], "config": {"num_proxies": 0}},
        )
        assert bad.status_code == 422
