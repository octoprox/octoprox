# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the OpenVPN API: server settings and per-project devices."""

from typing import Any

from starlette.testclient import TestClient

SETTINGS = "/api/v1/openvpn/settings"


def _peers_url(project: dict[str, Any]) -> str:
    return f"/api/v1/projects/{project['id']}/openvpn/peers"


def _reset(client: TestClient) -> None:
    for project in client.get("/api/v1/projects").json()["projects"]:
        for peer in client.get(f"/api/v1/projects/{project['id']}/openvpn/peers").json()["peers"]:
            client.delete(f"/api/v1/projects/{project['id']}/openvpn/peers/{peer['id']}")
    assert client.put(SETTINGS, json={"endpoint_host": "", "subnet": "10.67.0.0/16"}).status_code == 200


def _delete_project(client: TestClient, project_id: str) -> None:
    response = client.request("DELETE", f"/api/v1/projects/{project_id}", json={"confirmation": "permanently delete"})
    assert response.status_code in (200, 204), response.text


class TestAccess:
    def test_unauthenticated(self, async_client: TestClient) -> None:
        assert async_client.get(SETTINGS).status_code == 401

    def test_viewer_reads_but_cannot_write(self, viewer_client: TestClient) -> None:
        assert viewer_client.get(SETTINGS).status_code == 200
        assert viewer_client.get("/api/v1/projects/nope/openvpn/peers").status_code == 404
        assert viewer_client.post("/api/v1/projects/nope/openvpn/peers", json={"name": "tv"}).status_code == 403
        assert viewer_client.put(SETTINGS, json={"endpoint_host": "x"}).status_code == 403
        assert viewer_client.post(f"{SETTINGS}/rotate-identity").status_code == 403

    def test_editor_manages_peers_but_not_the_server(self, editor_client: TestClient, sample_project_data: dict[str, Any]) -> None:
        project = editor_client.post("/api/v1/projects", json=sample_project_data).json()
        assert editor_client.post(_peers_url(project), json={"name": "tv"}).status_code == 201
        assert editor_client.put(SETTINGS, json={"endpoint_host": "x"}).status_code == 403
        assert editor_client.post(f"{SETTINGS}/rotate-identity").status_code == 403


class TestServerSettings:
    def test_fresh_install_has_an_identity_and_no_endpoint(self, authenticated_client: TestClient) -> None:
        body = authenticated_client.get(SETTINGS).json()
        assert len(body["ca_fingerprint"].split(":")) == 32
        assert body["configured"] is False and body["protocol"] == "udp"
        assert body["gateway"] == "10.67.0.1" and body["endpoint_port"] == 1194
        assert body["status"]["state"] == "disabled" and body["status"]["enabled"] is False
        assert "ca_key" not in body and "server_key" not in body

    def test_save_and_rotate(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        _reset(authenticated_client)
        before = authenticated_client.get(SETTINGS).json()
        saved = authenticated_client.put(SETTINGS, json={
            "endpoint_host": "vpn.example.net", "endpoint_port": 443, "protocol": "tcp", "subnet": "10.77.0.0/24",
            "keepalive_interval": 5, "keepalive_timeout": 30, "client_mtu": 1380,
        })
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["configured"] is True and body["gateway"] == "10.77.0.1" and body["protocol"] == "tcp"
        assert body["ca_fingerprint"] == before["ca_fingerprint"]

        url = _peers_url(created_project)
        peer = authenticated_client.post(url, json={"name": "tv"}).json()
        profile = authenticated_client.get(f"{url}/{peer['id']}/config").json()
        assert "remote vpn.example.net 443" in profile["config"] and "proto tcp" in profile["config"]

        rotated = authenticated_client.post(f"{SETTINGS}/rotate-identity").json()
        assert rotated["ca_fingerprint"] != before["ca_fingerprint"]
        assert rotated["endpoint_host"] == "vpn.example.net"
        # Every device was reissued under the new CA: new serial, profile carries the new CA.
        reissued = authenticated_client.get(f"{url}/{peer['id']}").json()
        assert reissued["serial"] != peer["serial"]
        new_profile = authenticated_client.get(f"{url}/{peer['id']}/config").json()
        assert new_profile["config"] != profile["config"]

        _reset(authenticated_client)

    def test_invalid_settings(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.put(SETTINGS, json={"subnet": "10.0.0.1/16"}).status_code == 422
        assert authenticated_client.put(SETTINGS, json={"endpoint_host": "host:1194"}).status_code == 422
        assert authenticated_client.put(SETTINGS, json={"protocol": "sctp"}).status_code == 422
        assert authenticated_client.put(SETTINGS, json={"keepalive_interval": 60, "keepalive_timeout": 60}).status_code == 422

    def test_subnet_must_keep_existing_devices(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        _reset(authenticated_client)
        created = authenticated_client.post(_peers_url(created_project), json={"name": "tv"})
        assert created.status_code == 201 and created.json()["address"] == "10.67.0.2"
        moved = authenticated_client.put(SETTINGS, json={"subnet": "10.77.0.0/24"})
        assert moved.status_code == 400 and "outside" in moved.json()["detail"]


class TestPeers:
    def test_project_scoping(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get("/api/v1/projects/nope/openvpn/peers").status_code == 404

    def test_create_list_get_config(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        url = _peers_url(created_project)
        _reset(authenticated_client)
        authenticated_client.put(SETTINGS, json={"endpoint_host": "vpn.example.net", "subnet": "10.67.0.0/16"})

        created = authenticated_client.post(url, json={
            "name": "Living room TV", "session_id": "sofa", "country": "us", "state": "ny", "city": "New York",
        })
        assert created.status_code == 201, created.text
        peer = created.json()
        assert peer["address"] == "10.67.0.2"
        assert (peer["country"], peer["state"], peer["city"]) == ("US", "NY", "new_york")
        assert peer["serial"].isdigit() and "certificate_expires_at" in peer
        assert "private_key" not in peer and "certificate" not in peer

        second = authenticated_client.post(url, json={"name": "Router"}).json()
        assert second["address"] == "10.67.0.3" and second["serial"] != peer["serial"]

        listed = authenticated_client.get(url).json()
        assert listed["total"] == 2 and listed["server_configured"] is True and ":" in listed["ca_fingerprint"]
        assert [p["name"] for p in listed["peers"]] == ["Living room TV", "Router"]
        status = listed["peers"][0]["status"]
        assert status["live"] is False and status["online"] is False and status["last_seen_at"] is None
        assert listed["peers"][0]["metrics"]["request_count"] == 0

        history = authenticated_client.get(f"{url}/{peer['id']}/metrics/history", params={"range": "7d"})
        assert history.status_code == 200 and history.json() == {"snapshots": []}
        assert authenticated_client.get(f"{url}/nope/metrics/history").status_code == 404

        config = authenticated_client.get(f"{url}/{peer['id']}/config").json()
        assert config["filename"] == "Living-room-TV.ovpn" and config["complete"] is True
        assert "remote vpn.example.net 1194" in config["config"]
        assert "<cert>" in config["config"] and "<key>" in config["config"] and "<tls-crypt>" in config["config"]

        _reset(authenticated_client)

    def test_validation(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        url = _peers_url(created_project)
        assert authenticated_client.post(url, json={"name": "x", "state": "ny"}).status_code == 422
        assert authenticated_client.post(url, json={"name": "x", "country": "usa"}).status_code == 422
        assert authenticated_client.post(url, json={"name": " "}).status_code == 422
        assert authenticated_client.post(url, json={"name": "tv"}).status_code == 201
        assert authenticated_client.post(url, json={"name": "TV"}).status_code == 400

    def test_update_delete_and_rotate(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        url = _peers_url(created_project)
        peer = authenticated_client.post(url, json={"name": "tv", "country": "de", "session_id": "s1"}).json()

        updated = authenticated_client.patch(f"{url}/{peer['id']}", json={"enabled": False, "session_id": "", "state": "by"})
        assert updated.status_code == 200, updated.text
        assert updated.json()["enabled"] is False and updated.json()["session_id"] is None
        assert updated.json()["state"] == "BY" and updated.json()["country"] == "DE"
        assert authenticated_client.patch(f"{url}/{peer['id']}", json={"country": ""}).status_code == 400

        rotated = authenticated_client.post(f"{url}/{peer['id']}/rotate-certificate").json()
        assert rotated["serial"] != peer["serial"] and rotated["address"] == peer["address"]

        assert authenticated_client.delete(f"{url}/{peer['id']}").status_code == 204
        assert authenticated_client.get(f"{url}/{peer['id']}").status_code == 404
        assert authenticated_client.get(url).json()["total"] == 0

    def test_deleting_the_project_removes_its_peers(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        url = _peers_url(created_project)
        peer = authenticated_client.post(url, json={"name": "tv"}).json()
        runtime = authenticated_client.app.state.openvpn_runtime
        assert runtime.peers.get(peer["id"]) is not None
        _delete_project(authenticated_client, created_project["id"])
        assert authenticated_client.get(url).status_code == 404
        assert runtime.peers.get(peer["id"]) is None
