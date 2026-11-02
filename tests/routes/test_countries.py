# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The Countries page endpoints: where each country of a project routes now, and one country set across connectors."""

import uuid
from typing import Any

from starlette.testclient import TestClient

from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from tests.conftest import api_runtime, on_app_loop


def _url(project: dict[str, Any], country: str = "") -> str:
    return f"/api/v1/projects/{project['id']}/countries" + (f"/{country}" if country else "")


def _add_proxy(client: TestClient, connector_id: str, **metadata: str) -> None:
    proxy = Proxy(host=f"{uuid.uuid4().hex[:8]}.example.com", port=8080, protocol=ProxyProtocol.HTTP,
                  connector_id=connector_id, status=ProxyStatus.HEALTHY, metadata=metadata)
    on_app_loop(client, api_runtime(client).proxy_service.add, proxy)


def _connector(client: TestClient, project: dict[str, Any], credential: dict[str, Any], name: str, **fields: Any) -> str:
    response = client.post(
        f"/api/v1/projects/{project['id']}/connectors", json={"name": name, "credential_id": credential["id"], **fields}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


class TestCountries:
    def test_overview_lists_every_country_in_play_and_where_it_routes(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any]
    ) -> None:
        uk = _connector(
            authenticated_client, created_project, created_credential, "UK",
            config={"countries": ["GB"]}, routing_config={"reserved_countries": ["GB"]},
        )
        mixed = _connector(authenticated_client, created_project, created_credential, "Mixed")
        _add_proxy(authenticated_client, uk)
        _add_proxy(authenticated_client, mixed, country="GB")
        _add_proxy(authenticated_client, mixed, country="US")

        response = authenticated_client.get(_url(created_project))
        assert response.status_code == 200
        data = response.json()

        connectors = {c["name"]: c for c in data["connectors"]}
        assert connectors["UK"] | {"id": None} == {
            "id": None, "name": "UK", "credential_type": "static_proxy_provider", "enabled": True, "weight": 1,
            "countries": ["GB"], "provisions_per_country": False, "reserved_countries": ["GB"], "excluded_countries": [],
            "dynamic": False,
        }
        routes = {row["country"]: [r["connector_name"] for r in row["routes"]] for row in data["countries"]}
        assert routes == {"GB": ["UK"], "US": ["Mixed"]}
        # Any other country finds nothing; untargeted traffic keeps off the reserved exit.
        assert data["other"] == []
        assert [(r["connector_name"], r["proxies"]) for r in data["untargeted"]] == [("Mixed", 2)]

    def test_setting_a_country_writes_every_connector_named_and_answers_the_new_overview(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any]
    ) -> None:
        uk = _connector(authenticated_client, created_project, created_credential, "UK", config={"countries": ["GB"]})
        mixed = _connector(authenticated_client, created_project, created_credential, "Mixed")
        _add_proxy(authenticated_client, uk)
        _add_proxy(authenticated_client, mixed, country="GB")

        response = authenticated_client.put(
            _url(created_project, "uk"), json={"connectors": {uk: "reserved", mixed: "off"}}
        )
        assert response.status_code == 200, response.text
        connectors = {c["name"]: c for c in response.json()["connectors"]}
        assert connectors["UK"]["reserved_countries"] == ["GB"]
        assert connectors["Mixed"]["excluded_countries"] == ["GB"]

        refused = authenticated_client.put(_url(created_project, "GB"), json={"connectors": {uk: "off"}})
        assert refused.status_code == 422 and "UK lists only GB" in refused.json()["detail"]
        assert authenticated_client.put(_url(created_project, "GBR"), json={"connectors": {}}).status_code == 422

    def test_any_other_country_routes_to_the_connectors_serving_every_country(
        self, authenticated_client: TestClient, created_project: dict[str, Any], created_credential: dict[str, Any]
    ) -> None:
        """An "All countries" pool provisions any country on demand, so a country nothing names still routes there."""
        _connector(authenticated_client, created_project, created_credential, "US", config={"countries": ["US"]})
        response = authenticated_client.post(
            f"/api/v1/projects/{created_project['id']}/credentials",
            json={"name": "Oxylabs", "type": "oxylabs", "config": {"proxy_type": "residential", "username": "alice", "password": "s3cret"}},
        )
        assert response.status_code == 201, response.text
        _connector(
            authenticated_client, created_project, response.json(), "Pool",
            config={"num_proxies": 1, "session_duration_minutes": 10}, routing_config={"reserved_countries": ["AU"]},
        )

        data = authenticated_client.get(_url(created_project)).json()

        assert [(r["connector_name"], r["on_demand"]) for r in data["other"]] == [("Pool", True)]
        # The pool holds slots per listed country; the static connector's list only declares where its exits are.
        assert {c["name"]: c["provisions_per_country"] for c in data["connectors"]} == {"Pool": True, "US": False}
        # The US connector has no proxy yet, so the pool covers US as well.
        assert {row["country"]: [r["connector_name"] for r in row["routes"]] for row in data["countries"]} == {
            "AU": ["Pool"], "US": ["Pool"],
        }

    def test_viewers_read_and_never_write(self, viewer_client: TestClient) -> None:
        assert viewer_client.get(_url({"id": "nope"})).status_code == 404
        assert viewer_client.put(_url({"id": "nope"}, "GB"), json={"connectors": {}}).status_code == 403
