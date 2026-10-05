# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the WireGuard API: server settings and per-project devices."""

from typing import Any

from starlette.testclient import TestClient

SETTINGS = "/api/v1/wireguard/settings"


def _peers_url(project: dict[str, Any]) -> str:
    return f"/api/v1/projects/{project['id']}/wireguard/peers"


def _reset(client: TestClient) -> None:
    """Remove what other tests left behind: the database outlives the app, and only
    tests on the db_session fixture get tables truncated."""
    for project in client.get("/api/v1/projects").json()["projects"]:
        for peer in client.get(f"/api/v1/projects/{project['id']}/wireguard/peers").json()["peers"]:
            client.delete(f"/api/v1/projects/{project['id']}/wireguard/peers/{peer['id']}")
    assert client.put(SETTINGS, json={"endpoint_host": "", "subnet": "10.66.0.0/16"}).status_code == 200


def _delete_project(client: TestClient, project_id: str) -> None:
    response = client.request("DELETE", f"/api/v1/projects/{project_id}", json={"confirmation": "permanently delete"})
    assert response.status_code in (200, 204), response.text


class TestAccess:
    def test_unauthenticated(self, async_client: TestClient) -> None:
        assert async_client.get(SETTINGS).status_code == 401

    def test_viewer_reads_but_cannot_write(self, viewer_client: TestClient) -> None:
        # One client per test: a second TestClient would start the lifespan on another loop.
        assert viewer_client.get(SETTINGS).status_code == 200
        assert viewer_client.get("/api/v1/projects/nope/wireguard/peers").status_code == 404
        # Role is checked before the project exists or not.
        assert viewer_client.post("/api/v1/projects/nope/wireguard/peers", json={"name": "tv"}).status_code == 403
        assert viewer_client.put(SETTINGS, json={"endpoint_host": "x"}).status_code == 403
        assert viewer_client.post(f"{SETTINGS}/rotate-key").status_code == 403

    def test_editor_manages_peers_but_not_the_server(self, editor_client: TestClient, sample_project_data: dict[str, Any]) -> None:
        project = editor_client.post("/api/v1/projects", json=sample_project_data).json()
        assert editor_client.post(_peers_url(project), json={"name": "tv"}).status_code == 201
        assert editor_client.put(SETTINGS, json={"endpoint_host": "x"}).status_code == 403
        assert editor_client.post(f"{SETTINGS}/rotate-key").status_code == 403


class TestServerSettings:
    def test_fresh_install_has_a_key_and_no_endpoint(self, authenticated_client: TestClient) -> None:
        body = authenticated_client.get(SETTINGS).json()
        assert len(body["public_key"]) == 44
        assert body["configured"] is False
        assert body["gateway"] == "10.66.0.1"
        assert body["status"]["state"] == "disabled"
        assert body["status"]["enabled"] is False

    def test_save_and_rotate(self, authenticated_client: TestClient) -> None:
        _reset(authenticated_client)
        before = authenticated_client.get(SETTINGS).json()
        saved = authenticated_client.put(SETTINGS, json={
            "endpoint_host": "vpn.example.net", "endpoint_port": 51821, "subnet": "10.77.0.0/24",
            "persistent_keepalive": 15, "client_mtu": 1380,
        })
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["configured"] is True and body["gateway"] == "10.77.0.1" and body["client_mtu"] == 1380
        assert body["public_key"] == before["public_key"]

        rotated = authenticated_client.post(f"{SETTINGS}/rotate-key").json()
        assert rotated["public_key"] != before["public_key"]
        assert rotated["endpoint_host"] == "vpn.example.net"

        _reset(authenticated_client)

    def test_invalid_settings(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.put(SETTINGS, json={"subnet": "10.0.0.1/16"}).status_code == 422
        assert authenticated_client.put(SETTINGS, json={"endpoint_host": "host:51820"}).status_code == 422
        assert authenticated_client.put(SETTINGS, json={"endpoint_port": 0}).status_code == 422

    def test_subnet_must_keep_existing_devices(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        _reset(authenticated_client)
        created = authenticated_client.post(_peers_url(created_project), json={"name": "tv"})
        assert created.status_code == 201
        assert created.json()["address"] == "10.66.0.2"
        moved = authenticated_client.put(SETTINGS, json={"subnet": "10.77.0.0/24"})
        assert moved.status_code == 400
        assert "outside" in moved.json()["detail"]


class TestPeers:
    def test_project_scoping(self, authenticated_client: TestClient) -> None:
        assert authenticated_client.get("/api/v1/projects/nope/wireguard/peers").status_code == 404

    def test_create_list_get_config(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        url = _peers_url(created_project)
        _reset(authenticated_client)
        authenticated_client.put(SETTINGS, json={"endpoint_host": "vpn.example.net", "subnet": "10.66.0.0/16"})

        created = authenticated_client.post(url, json={
            "name": "Living room TV", "session_id": "sofa", "country": "us", "state": "ny", "city": "New York",
        })
        assert created.status_code == 201, created.text
        peer = created.json()
        assert peer["address"] == "10.66.0.2"
        assert peer["has_private_key"] is True and peer["has_preshared_key"] is True
        assert (peer["country"], peer["state"], peer["city"]) == ("US", "NY", "new_york")
        assert "private_key" not in peer

        second = authenticated_client.post(url, json={"name": "Router", "preshared": False}).json()
        assert second["address"] == "10.66.0.3" and second["has_preshared_key"] is False

        listed = authenticated_client.get(url).json()
        assert listed["total"] == 2 and listed["server_configured"] is True
        assert [p["name"] for p in listed["peers"]] == ["Living room TV", "Router"]
        # Nobody is carrying the tunnel in tests: no live reading, never seen, nothing counted yet.
        status = listed["peers"][0]["status"]
        assert status["live"] is False and status["online"] is False and status["last_handshake_at"] is None
        assert listed["peers"][0]["metrics"]["request_count"] == 0

        history = authenticated_client.get(f"{url}/{peer['id']}/metrics/history", params={"range": "7d"})
        assert history.status_code == 200 and history.json() == {"snapshots": []}
        assert authenticated_client.get(f"{url}/{peer['id']}/metrics/history", params={"range": "1y"}).status_code == 422
        assert authenticated_client.get(f"{url}/nope/metrics/history").status_code == 404

        got = authenticated_client.get(f"{url}/{peer['id']}")
        assert got.status_code == 200 and got.json()["name"] == "Living room TV"

        config = authenticated_client.get(f"{url}/{peer['id']}/config").json()
        assert config["filename"] == "Living-room-TV.conf"
        assert config["complete"] is True
        assert "Address = 10.66.0.2/32" in config["config"]
        assert "DNS = 10.66.0.1" in config["config"]
        assert "Endpoint = vpn.example.net:51820" in config["config"]
        assert f"PublicKey = {listed['server_public_key']}" in config["config"]

        _reset(authenticated_client)

    def test_bring_your_own_key(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        url = _peers_url(created_project)
        public = "HIgo9xNzJMWLKASShiTqIybxZ0U3wGLiUeJ1PKf8ykw="
        created = authenticated_client.post(url, json={"name": "phone", "public_key": public})
        assert created.status_code == 201, created.text
        assert created.json()["public_key"] == public and created.json()["has_private_key"] is False
        config = authenticated_client.get(f"{url}/{created.json()['id']}/config").json()
        assert config["complete"] is False
        assert "paste the device's private key" in config["config"]

        assert authenticated_client.post(url, json={"name": "bad", "public_key": "nope"}).status_code == 422
        duplicate = authenticated_client.post(url, json={"name": "phone2", "public_key": public})
        assert duplicate.status_code == 400

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
        assert updated.json()["enabled"] is False
        assert updated.json()["session_id"] is None
        assert updated.json()["state"] == "BY" and updated.json()["country"] == "DE"

        orphaned = authenticated_client.patch(f"{url}/{peer['id']}", json={"country": ""})
        assert orphaned.status_code == 400

        rotated = authenticated_client.post(f"{url}/{peer['id']}/rotate-keys").json()
        assert rotated["public_key"] != peer["public_key"]
        assert rotated["address"] == peer["address"]

        assert authenticated_client.delete(f"{url}/{peer['id']}").status_code == 204
        assert authenticated_client.get(f"{url}/{peer['id']}").status_code == 404
        assert authenticated_client.get(url).json()["total"] == 0

    def test_deleting_the_project_removes_its_peers(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        url = _peers_url(created_project)
        peer = authenticated_client.post(url, json={"name": "tv"}).json()
        runtime = authenticated_client.app.state.wireguard_runtime
        assert runtime.peers.get(peer["id"]) is not None
        _delete_project(authenticated_client, created_project["id"])
        assert authenticated_client.get(url).status_code == 404
        # The directory follows the cascade at once, not on the next reload.
        assert runtime.peers.get(peer["id"]) is None
