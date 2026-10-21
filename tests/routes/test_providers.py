# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the provider catalog and admin descriptor endpoints, on an api process, with a mock vendor API where one is called.

A descriptor write appends a ``descriptor`` entry that every process follows and applies to the writing api at once; a
write of another api's is stored here as that api would store it, announced or not as the test chooses.
"""

import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, text
from starlette.testclient import TestClient

from api.core.config import Settings
from api.db.redis import CHANGES_STREAM_KEY, RedisClient
from api.db.repository import ProviderDescriptorRepository
from api.db.session import SessionFactory
from api.db.streams import ChangeKind, ChangeLog, ref_of
from api.models.provider import ProviderRecord
from api.providers.sdk.egress import EgressPolicy
from tests.conftest import api_runtime, on_app_loop

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


PROXIED_REQUESTS: list[tuple[str, str]] = []
"""``(proxy_url, target_url)`` pairs sent "through a proxy" by the mocked runtime."""


def _proxied_client(proxy_url: str, timeout: float) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        PROXIED_REQUESTS.append((proxy_url, str(request.url)))
        return httpx.Response(200, json={"origin": "203.0.113.7"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)


@pytest.fixture
def mocked_vendor(authenticated_client: TestClient) -> Iterator[TestClient]:
    """Point the registry's SDK runtime at a mock Acme API and a mock proxy, and relax egress."""
    registry = api_runtime(authenticated_client).provider_registry
    original = registry._runtime
    registry._runtime = replace(
        original,
        egress_policy=EgressPolicy(allow_http=True, allow_private=True, pin_dns=False),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_acme_api)),
        proxied_client_factory=_proxied_client,
    )
    PROXIED_REQUESTS.clear()
    yield authenticated_client
    registry._runtime = original


def _create_acme(client: TestClient, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    response = client.post("/api/v1/providers", json={"spec": spec or ACME_SPEC, "confirmed_hosts": ["api.acme.test"]})
    assert response.status_code == 201, response.text
    return response.json()


class Unheard(ChangeLog):
    """A change log that keeps what the routes give it: nothing reaches the stream, so no refresher hears of a write."""

    def __init__(self) -> None:
        # A client never connected: this log sends nothing.
        super().__init__(RedisClient("redis://redis.invalid"))
        self.entries: list[tuple[ChangeKind, str]] = []

    async def record(self, kind: ChangeKind, *ids: str) -> None:
        ref = ref_of(ids)
        self.entries.append((kind, ref))


def stream_head(client: TestClient) -> str:
    """The changes stream's last entry ID ("0-0" before any): what the entries of a test's writes come after."""
    redis = api_runtime(client).redis_client

    async def head() -> str:
        entries = await redis.client.xrevrange(CHANGES_STREAM_KEY, count=1)
        return str(entries[0][0]) if entries else "0-0"

    return on_app_loop(client, head)


def announced_since(client: TestClient, after: str) -> list[tuple[str, str]]:
    """The changes stream's entries after ``after`` (see :func:`stream_head`), as (kind, ref), oldest first."""
    redis = api_runtime(client).redis_client

    async def entries() -> list[tuple[str, str]]:
        found = await redis.client.xrange(CHANGES_STREAM_KEY, min=f"({after}")
        return [(fields["kind"], fields["ref"]) for _, fields in found]

    return on_app_loop(client, entries)


@pytest.fixture
def unheard(monkeypatch: pytest.MonkeyPatch) -> Unheard:
    """The descriptor routes announce their writes here instead: this api's refresher never hears of them."""
    log = Unheard()
    monkeypatch.setattr("api.routes.providers.change_log_of", lambda request: log)
    return log


def _create_unique_acme(client: TestClient) -> str:
    """The Acme descriptor under an id of its own: credentials other tests left under ``acme`` do not use it."""
    provider_id = f"acme_{uuid.uuid4().hex[:8]}"
    _create_acme(client, dict(ACME_SPEC, id=provider_id))
    return provider_id


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

    def test_usage_counts_credentials_and_their_connectors(
        self, mocked_vendor: TestClient, created_project: dict[str, Any],
    ) -> None:
        client = mocked_vendor
        provider_id = _create_unique_acme(client)
        config = {"api_key": "good", "username": "u", "password": "p"}
        credentials = []
        for name in ("one", "two"):
            response = client.post(
                f"/api/v1/projects/{created_project['id']}/credentials",
                json={"name": f"{provider_id}-{name}", "type": provider_id, "config": config},
            )
            assert response.status_code == 201, response.text
            credentials.append(response.json()["id"])
        response = client.post(
            f"/api/v1/projects/{created_project['id']}/connectors",
            json={"name": provider_id, "credential_id": credentials[0], "config": {"num_proxies": 1}},
        )
        assert response.status_code == 201, response.text

        detail = client.get(f"/api/v1/providers/{provider_id}").json()
        listed = {p["id"]: p for p in client.get("/api/v1/providers").json()["providers"]}[provider_id]

        assert (detail["credential_count"], detail["connector_count"]) == (2, 1)
        assert (listed["credential_count"], listed["connector_count"]) == (2, 1)
        refused = client.delete(f"/api/v1/providers/{provider_id}")
        assert refused.status_code == 400 and refused.json()["detail"] == "Cannot delete provider: 2 credential(s) use it"


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


class TestDefinitionsFollowPostgres:
    """A descriptor is a row: every process follows its change entry, the writing api applies its write at once."""

    def test_each_write_appends_one_descriptor_entry(self, authenticated_client: TestClient) -> None:
        after = stream_head(authenticated_client)

        provider_id = _create_unique_acme(authenticated_client)
        renamed = dict(ACME_SPEC, id=provider_id, name="Acme Renamed")
        assert authenticated_client.put(f"/api/v1/providers/{provider_id}", json={"spec": renamed}).status_code == 200
        assert authenticated_client.delete(f"/api/v1/providers/{provider_id}").status_code == 204
        disabled = _create_unique_acme(authenticated_client)
        assert authenticated_client.put(f"/api/v1/providers/{disabled}", json={"enabled": False}).status_code == 200

        assert announced_since(authenticated_client, after) == [
            *[("descriptor", provider_id)] * 3,
            *[("descriptor", disabled)] * 2,
        ]

    def test_a_refused_write_announces_nothing(self, authenticated_client: TestClient) -> None:
        provider_id = _create_unique_acme(authenticated_client)
        after = stream_head(authenticated_client)

        # The id is taken; a builtin cannot change; the hosts are not confirmed.
        taken, unconfirmed = dict(ACME_SPEC, id=provider_id), dict(ACME_SPEC, id="acme_new")
        assert authenticated_client.post("/api/v1/providers", json={"spec": taken}).status_code == 409
        assert authenticated_client.delete("/api/v1/providers/oxylabs").status_code == 409
        assert authenticated_client.post("/api/v1/providers", json={"spec": unconfirmed}).status_code == 409

        assert announced_since(authenticated_client, after) == []

    def test_a_descriptor_stored_by_another_process_is_picked_up(self, authenticated_client: TestClient) -> None:
        """Another api stored it and announced it: this one's definitions refresher reads it again as the entry lands.

        Watched in the registry, not through a catalog read, which would apply the row itself.
        """
        provider_id = _store_elsewhere(authenticated_client, announce=True)
        registry = api_runtime(authenticated_client).provider_registry

        deadline = time.monotonic() + 10
        while registry.get(provider_id) is None:
            assert time.monotonic() < deadline, "the stored descriptor never reached this api's registry"
            time.sleep(0.2)
        assert authenticated_client.get(f"/api/v1/providers/{provider_id}").json()["source"] == "custom"


class TestCatalogReadsFollowPostgres:
    """Another api's write shows in this api's next catalog read: the read applies the rows before it answers.

    The write is stored here as yet unannounced (its entry not here yet), and
    the hourly reconcile is far off, so only the reads themselves can pick it up.
    """

    @pytest.fixture
    def test_settings(self, test_settings: Settings) -> Settings:
        test_settings.reconcile_interval = 3600.0
        return test_settings

    def test_one_provider_is_answered_as_stored(self, authenticated_client: TestClient) -> None:
        provider_id = _store_elsewhere(authenticated_client)

        response = authenticated_client.get(f"/api/v1/providers/{provider_id}")
        assert response.status_code == 200, response.text
        assert response.json()["source"] == "custom" and response.json()["updated_at"] is not None

        _delete_elsewhere(authenticated_client, provider_id)
        assert authenticated_client.get(f"/api/v1/providers/{provider_id}").status_code == 404

    def test_the_catalog_is_answered_as_stored(self, authenticated_client: TestClient) -> None:
        provider_id = _store_elsewhere(authenticated_client)

        assert provider_id in _listed(authenticated_client)

        _delete_elsewhere(authenticated_client, provider_id)
        assert provider_id not in _listed(authenticated_client)

    def test_an_export_is_answered_as_stored(self, authenticated_client: TestClient) -> None:
        provider_id = _store_elsewhere(authenticated_client)

        export = authenticated_client.get(f"/api/v1/providers/{provider_id}/export")
        assert export.status_code == 200 and export.text.startswith(f"id: {provider_id}")


class TestWritesFollowPostgres:
    """A write decides from the stored rows: a descriptor another api stored a moment ago can be changed at once.

    The other api's write is stored here as yet unannounced, and the hourly
    reconcile is far off, so only the writes themselves can pick it up.
    """

    @pytest.fixture
    def test_settings(self, test_settings: Settings) -> Settings:
        test_settings.reconcile_interval = 3600.0
        return test_settings

    def test_an_update_finds_it(self, authenticated_client: TestClient) -> None:
        provider_id = _store_elsewhere(authenticated_client)

        response = authenticated_client.put(
            f"/api/v1/providers/{provider_id}", json={"spec": dict(ACME_SPEC, id=provider_id, name="Acme Renamed")}
        )
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Acme Renamed"

    def test_an_update_asks_to_confirm_only_the_hosts_the_stored_row_lacks(self, authenticated_client: TestClient) -> None:
        provider_id = _store_elsewhere(authenticated_client)
        stored = dict(ACME_SPEC, id=provider_id)
        added = dict(
            stored, options={**stored["options"], "extra": {"call": {"url": "https://other.acme.test/x"}, "value": "id"}}
        )

        response = authenticated_client.put(f"/api/v1/providers/{provider_id}", json={"spec": added})
        assert response.status_code == 409 and response.json()["detail"]["unconfirmed_hosts"] == ["other.acme.test"]

    def test_a_delete_finds_it(self, authenticated_client: TestClient) -> None:
        provider_id = _store_elsewhere(authenticated_client)

        assert authenticated_client.delete(f"/api/v1/providers/{provider_id}").status_code == 204
        assert provider_id not in _listed(authenticated_client)

    def test_a_replacing_import_replaces_it(self, authenticated_client: TestClient) -> None:
        provider_id = _store_elsewhere(authenticated_client)
        yaml_text = authenticated_client.post(
            "/api/v1/providers/validate", json={"spec": dict(ACME_SPEC, id=provider_id, name="Acme Two")}
        ).json()["yaml"]

        response = authenticated_client.post(
            "/api/v1/providers/import", json={"yaml": yaml_text, "confirmed_hosts": ["api.acme.test"], "replace": True}
        )
        assert response.status_code == 201, response.text
        assert response.json()["name"] == "Acme Two"



class TestTheWritingApiAppliesAtOnce:
    """The api that wrote a descriptor answers with it at once: it does not wait for its refresher.

    Its writes are announced to a log no refresher follows (``unheard``), so
    only the routes themselves can apply them here.
    """

    @pytest.fixture
    def test_settings(self, test_settings: Settings) -> Settings:
        test_settings.reconcile_interval = 3600.0
        return test_settings

    def test_a_created_descriptor_is_usable_at_once(
        self, mocked_vendor: TestClient, created_project: dict[str, Any], unheard: Unheard
    ) -> None:
        provider_id = _create_unique_acme(mocked_vendor)
        assert unheard.entries == [(ChangeKind.DESCRIPTOR, provider_id)]

        assert provider_id in {p["id"] for p in mocked_vendor.get("/api/v1/providers").json()["providers"]}
        response = mocked_vendor.post(
            f"/api/v1/projects/{created_project['id']}/credentials",
            json={"name": provider_id, "type": provider_id, "config": {"api_key": "good", "username": "u", "password": "p"}},
        )
        assert response.status_code == 201, response.text

    def test_a_disabled_descriptor_leaves_the_catalog_at_once(
        self, authenticated_client: TestClient, unheard: Unheard
    ) -> None:
        provider_id = _create_unique_acme(authenticated_client)

        assert authenticated_client.put(f"/api/v1/providers/{provider_id}", json={"enabled": False}).status_code == 200

        assert authenticated_client.get(f"/api/v1/providers/{provider_id}").status_code == 404

    def test_a_deleted_descriptor_leaves_the_registry_at_once(
        self, authenticated_client: TestClient, unheard: Unheard
    ) -> None:
        provider_id = _create_unique_acme(authenticated_client)

        assert authenticated_client.delete(f"/api/v1/providers/{provider_id}").status_code == 204

        assert authenticated_client.get(f"/api/v1/providers/{provider_id}").status_code == 404
        assert api_runtime(authenticated_client).provider_registry.get(provider_id) is None

    def test_an_apply_cut_by_a_deadline_is_logged_by_its_type(
        self, authenticated_client: TestClient, unheard: Unheard, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The write stands, and a bare TimeoutError, which has no text, is named by its type."""
        provider_id = _create_unique_acme(authenticated_client)
        store = api_runtime(authenticated_client).provider_store
        monkeypatch.setattr(store, "sync_all", AsyncMock(side_effect=TimeoutError))

        with patch("api.routes.providers.logger") as log:
            assert authenticated_client.delete(f"/api/v1/providers/{provider_id}").status_code == 204

        log.warning.assert_called_once_with("Provider descriptors not applied after a write", error="TimeoutError")


class TestBuiltinProviders:
    """Shipped descriptors can be exercised through the same test endpoint, without a spec."""

    def test_builtin_vendor_calls_and_proxy_request(self, mocked_vendor: TestClient) -> None:
        client = mocked_vendor
        no_validation = client.post("/api/v1/providers/oxylabs/test", json={"action": "validate", "credential_config": {"username": "u", "password": "p"}})
        assert no_validation.status_code == 200 and no_validation.json()["ok"] is False
        assert "no credential validation" in no_validation.json()["message"]

        request = client.post(
            "/api/v1/providers/oxylabs/test",
            json={"action": "proxy_request", "credential_config": {"proxy_type": "residential", "username": "alice", "password": "pw"}, "connector_config": {"num_proxies": 3, "country_code": "DE"}},
        )
        assert request.status_code == 200, request.text
        assert request.json()["ok"] is True and "pr.oxylabs.io:7777" in request.json()["message"]
        proxy_url, _target = PROXIED_REQUESTS[-1]
        assert proxy_url.startswith("http://customer-alice-cc-DE-sessid-") and proxy_url.endswith(":pw@pr.oxylabs.io:7777")

    def test_builtin_test_is_admin_only_and_needs_a_descriptor(self, authenticated_client: TestClient, viewer_auth_headers: dict[str, str]) -> None:
        body = {"action": "proxy_request", "credential_config": {}}
        assert authenticated_client.post("/api/v1/providers/oxylabs/test", json=body, headers=viewer_auth_headers).status_code == 403
        assert authenticated_client.post("/api/v1/providers/static/test", json=body).status_code == 404
        assert authenticated_client.post("/api/v1/providers/nope/test", json=body).status_code == 404


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

    def test_request_through_proxy_for_draft_and_stored_provider(self, mocked_vendor: TestClient) -> None:
        """``proxy_request`` provisions one endpoint in memory and fetches a URL through it; nothing is saved."""
        client = mocked_vendor
        config = {"credential_config": {"api_key": "good", "username": "u", "password": "s3cret-pw"}, "connector_config": {"num_proxies": 5, "region": "eu"}}

        draft = client.post("/api/v1/providers/acme_draft/test", json={"action": "proxy_request", "spec": dict(ACME_SPEC, id="acme_draft"), **config})
        assert draft.status_code == 200, draft.text
        body = draft.json()
        assert body["ok"] is True and body["message"].startswith("HTTP 200 through gw.acme.test:9000")
        assert body["result"]["exit_ip"] == "203.0.113.7" and body["result"]["proxy"]["host"] == "gw.acme.test"
        assert body["result"]["proxy"]["username"].startswith("u-r-eu-s-") and "password" not in body["result"]["proxy"]
        # The credential is validated first, as on credential creation, then the request goes through the proxy.
        assert [(t["url"], t["status"]) for t in body["traces"]] == [("https://api.acme.test/me", 200), ("https://httpbin.org/ip", 200)]
        assert body["traces"][0]["headers"]["Authorization"] == "***"
        assert body["traces"][1] == {"method": "GET", "url": "https://httpbin.org/ip", "status": 200, "elapsed_ms": body["traces"][1]["elapsed_ms"], "error": None, "page": 1, "headers": {}}
        proxy_url, target = PROXIED_REQUESTS[-1]
        assert proxy_url.startswith("http://u-r-eu-s-") and proxy_url.endswith(":s3cret-pw@gw.acme.test:9000") and target == "https://httpbin.org/ip"
        assert client.get("/api/v1/providers/acme_draft").status_code == 404  # still unsaved

        _create_acme(client)
        before = client.get("/api/v1/providers/acme").json()
        stored = client.post("/api/v1/providers/acme/test", json={"action": "proxy_request", "target_url": "https://example.org/whoami", **config})
        assert stored.status_code == 200 and stored.json()["ok"] is True
        assert PROXIED_REQUESTS[-1][1] == "https://example.org/whoami"
        after = client.get("/api/v1/providers/acme").json()
        assert (after["credential_count"], after["connector_count"]) == (before["credential_count"], before["connector_count"])

    def test_options_for_an_unsaved_draft(self, mocked_vendor: TestClient, editor_auth_headers: dict[str, str]) -> None:
        """The builder's test panel drives dynamic selects against the in-editor descriptor."""
        client = mocked_vendor
        draft = dict(ACME_SPEC, id="acme_draft")
        body = {"credential_config": {"api_key": "good"}, "spec": draft}
        resolved = client.post("/api/v1/providers/acme_draft/options/regions", json=body)
        assert resolved.status_code == 200, resolved.text
        assert [o["value"] for o in resolved.json()["options"]] == ["eu", "us"]
        assert client.get("/api/v1/providers/acme_draft").status_code == 404  # still unsaved
        assert client.post("/api/v1/providers/acme_draft/options/nope", json=body).status_code == 404
        # A draft sends credentials wherever it says, so only admins may use one.
        assert client.post("/api/v1/providers/acme_draft/options/regions", json=body, headers=editor_auth_headers).status_code == 403
        # A saved credential id is ignored with a draft: the throwaway config is required.
        assert client.post("/api/v1/providers/acme_draft/options/regions", json={"credential_id": "whatever", "spec": draft}).status_code == 422

    def test_options_endpoint_rejects_private_draft_hosts(self, authenticated_client: TestClient) -> None:
        """Same static egress checks as saving: a draft cannot probe private networks (strict policy here)."""
        draft = dict(ACME_SPEC, id="acme_bad", options={"regions": {**ACME_SPEC["options"]["regions"], "call": {"url": "https://10.0.0.1/regions"}}})
        response = authenticated_client.post("/api/v1/providers/acme_bad/options/regions", json={"credential_config": {"api_key": "k"}, "spec": draft})
        assert response.status_code == 422 and "not publicly routable" in response.json()["detail"]

    def test_request_through_proxy_rejects_private_targets(self, mocked_vendor: TestClient) -> None:
        # The relaxed test policy allows private hosts, so check the model-level guard instead.
        bad = mocked_vendor.post("/api/v1/providers/acme/test", json={"action": "proxy_request", "target_url": "ftp://example.org/", "spec": ACME_SPEC, "credential_config": {"api_key": "good", "username": "u", "password": "p"}})
        assert bad.status_code == 200 and bad.json()["ok"] is False and "target URL rejected" in bad.json()["message"]
        assert mocked_vendor.post("/api/v1/providers/acme/test", json={"action": "teleport", "spec": ACME_SPEC}).status_code == 422

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
        assert connector.json()["config"] == {
            "num_proxies": 2, "session_mode": "pool", "country_code": ["US"], "session_duration_minutes": 10
        }
        assert connector.json()["target"] == {
            "total": 2, "per_country": 2, "countries": ["US"], "on_demand": [], "dynamic": False,
            "exit_sample_percent": None,
        }
        bad = authenticated_client.post(
            f"/api/v1/projects/{project_id}/connectors",
            json={"name": "oxy-bad", "credential_id": credential.json()["id"], "config": {"num_proxies": 0}},
        )
        assert bad.status_code == 422


async def _store_record(session_factory: SessionFactory, record: ProviderRecord) -> None:
    """A descriptor row as another api process stores it."""
    async with session_factory() as session:
        await ProviderDescriptorRepository(session).create(record)
        await session.commit()


async def _delete_record(session_factory: SessionFactory, provider_id: str) -> None:
    """A descriptor row as another api process deletes it."""
    async with session_factory() as session:
        await ProviderDescriptorRepository(session).delete(provider_id)
        await session.commit()


def _store_elsewhere(client: TestClient, *, announce: bool = False) -> str:
    """Store the Acme descriptor under an id of its own, as another api would; announced when ``announce``.

    Unannounced, nothing tells this api (the other's entry has not arrived);
    announced, the other api's change log appends its ``descriptor`` entry.
    """
    provider_id = f"acme_{uuid.uuid4().hex[:8]}"
    record = ProviderRecord(
        id=provider_id, name="Acme Proxies", spec=dict(ACME_SPEC, id=provider_id), created_by="elsewhere"
    )
    runtime = api_runtime(client)
    on_app_loop(client, _store_record, runtime.session_factory, record)
    if announce:
        on_app_loop(client, ChangeLog(runtime.redis_client).record, ChangeKind.DESCRIPTOR, provider_id)
    return provider_id


def _delete_elsewhere(client: TestClient, provider_id: str) -> None:
    """Delete a descriptor row as another api would: nothing tells this one."""
    on_app_loop(client, _delete_record, api_runtime(client).session_factory, provider_id)


def _listed(client: TestClient) -> set[str]:
    """The provider ids the catalog lists."""
    response = client.get("/api/v1/providers")
    assert response.status_code == 200, response.text
    return {p["id"] for p in response.json()["providers"]}
