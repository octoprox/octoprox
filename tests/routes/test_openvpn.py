# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the OpenVPN API: server settings and per-project devices, on an api process.

The same shape as the WireGuard tests: Postgres rows the gateways running
the endpoint follow through the feed, with each gateway's endpoint read from
its heartbeat and each device's session from what the gateways publish.
"""

import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from api.core.stats import TunnelPeerMetricDelta
from api.db.openvpn_repository import OpenVpnPeerRepository
from api.db.redis import TUNNEL_PEER_METRICS_KEY, TUNNEL_PEER_WINDOWS
from api.db.repository import MetricsRepository
from api.models.openvpn import OpenVpnPeer, OpenVpnSessionReading
from tests.conftest import api_runtime, on_app_loop
from tests.routes.gateway_heartbeats import advertise_gateway, withdraw_gateway

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


def _stored(client: TestClient, peer_id: str) -> OpenVpnPeer | None:
    async def read() -> OpenVpnPeer | None:
        async with api_runtime(client).session_factory() as session:
            return await OpenVpnPeerRepository(session).get_by_id(peer_id)

    return on_app_loop(client, read)


def _count_device(
    client: TestClient, peer_id: str, *, flushed: TunnelPeerMetricDelta, window: TunnelPeerMetricDelta
) -> None:
    """The device's totals as a worker's metrics flush adds them, and its window as gateways leave it in Redis."""
    runtime = api_runtime(client)

    async def count() -> None:
        async with runtime.session_factory() as session:
            await MetricsRepository(session).upsert_totals(TUNNEL_PEER_WINDOWS, {peer_id: flushed})
            await session.commit()
        key = TUNNEL_PEER_METRICS_KEY.format(peer_id=peer_id)
        await runtime.redis_client.client.hset(key, mapping=window.to_dict())

    on_app_loop(client, count)


@pytest.fixture
def gateways(authenticated_client: TestClient) -> Iterator[Callable[..., None]]:
    """Advertise live gateways with the given heartbeat fields; they leave when the test ends."""
    advertised: list[str] = []

    def advertise(instance_id: str, **fields: Any) -> None:
        advertised.append(instance_id)
        advertise_gateway(authenticated_client, instance_id, **fields)

    yield advertise
    for instance_id in advertised:
        withdraw_gateway(authenticated_client, instance_id)


class TestAccess:
    def test_unauthenticated(self, async_client: TestClient) -> None:
        assert async_client.get(SETTINGS).status_code == 401

    def test_viewer_reads_but_cannot_write(self, viewer_client: TestClient) -> None:
        assert viewer_client.get(SETTINGS).status_code == 200
        assert viewer_client.get("/api/v1/projects/nope/openvpn/peers").status_code == 404
        assert viewer_client.post("/api/v1/projects/nope/openvpn/peers", json={"name": "tv"}).status_code == 403
        assert viewer_client.put(SETTINGS, json={"endpoint_host": "x"}).status_code == 403
        assert viewer_client.post(f"{SETTINGS}/rotate-identity").status_code == 403
        # A profile carries the device's private key: editors only.
        assert viewer_client.get("/api/v1/projects/nope/openvpn/peers/x/config").status_code == 403

    def test_editor_manages_peers_but_not_the_server(self, editor_client: TestClient, sample_project_data: dict[str, Any]) -> None:
        project = editor_client.post("/api/v1/projects", json=sample_project_data).json()
        created = editor_client.post(_peers_url(project), json={"name": "tv"})
        assert created.status_code == 201
        assert editor_client.get(f"{_peers_url(project)}/{created.json()['id']}/config").status_code == 200
        assert editor_client.put(SETTINGS, json={"endpoint_host": "x"}).status_code == 403
        assert editor_client.post(f"{SETTINGS}/rotate-identity").status_code == 403


class TestServerSettings:
    def test_fresh_install_has_an_identity_and_no_endpoint(self, authenticated_client: TestClient) -> None:
        _reset(authenticated_client)
        body = authenticated_client.get(SETTINGS).json()
        assert len(body["ca_fingerprint"].split(":")) == 32
        assert body["configured"] is False and body["protocol"] == "udp"
        assert body["gateway"] == "10.67.0.1" and body["endpoint_port"] == 1194
        assert body["status"]["gateways"] == [] and body["status"]["peers_total"] == 0
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

    def test_subnet_must_not_share_addresses_with_wireguard(self, authenticated_client: TestClient) -> None:
        _reset(authenticated_client)
        wireguard = authenticated_client.get("/api/v1/wireguard/settings").json()["subnet"]
        clash = authenticated_client.put(SETTINGS, json={"subnet": wireguard})
        assert clash.status_code == 400 and "overlaps the WireGuard subnet" in clash.json()["detail"]


class TestStatus:
    def test_lists_every_gateway_running_the_endpoint(
        self, authenticated_client: TestClient, gateways: Callable[..., None]
    ) -> None:
        gateways(
            "gw-test-a",
            tunnels=[
                {"protocol": "openvpn", "instance_id": "gw-test-a", "state": "running", "listen_port": 1194,
                 "restarts": 1, "denied": 3, "daemon_version": "OpenVPN 2.6.12"},
                {"protocol": "wireguard", "instance_id": "gw-test-a", "state": "running", "listen_port": 51820},
            ],
        )
        status = authenticated_client.get(SETTINGS).json()["status"]
        assert all(gateway["protocol"] == "openvpn" for gateway in status["gateways"])
        ours = [gateway for gateway in status["gateways"] if gateway["instance_id"].startswith("gw-test-")]
        assert [(g["instance_id"], g["state"], g["restarts"], g["denied"]) for g in ours] == [
            ("gw-test-a", "running", 1, 3)
        ]
        assert ours[0]["daemon_version"] == "OpenVPN 2.6.12"

    def test_devices_stand_as_the_gateways_last_saw_them(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        _reset(authenticated_client)
        url = _peers_url(created_project)
        tv = authenticated_client.post(url, json={"name": "tv"}).json()
        authenticated_client.post(url, json={"name": "router", "enabled": False})
        redis = api_runtime(authenticated_client).redis_client
        session = OpenVpnSessionReading(
            connected_since=int(time.time()) - 60, rx_bytes=500, tx_bytes=700, endpoint="198.51.100.9:40000"
        )
        on_app_loop(
            authenticated_client, redis.set_tunnel_peer_status, "openvpn", "gw-test-a",
            {tv["id"]: session.model_dump_json()},
        )
        try:
            listed = authenticated_client.get(url).json()["peers"]
            status = {peer["name"]: peer["status"] for peer in listed}
            assert status["tv"]["online"] is True and status["tv"]["live"] is True
            assert status["tv"]["endpoint"] == "198.51.100.9:40000" and status["tv"]["tx_bytes"] == 700
            assert status["router"]["online"] is False and status["router"]["live"] is False

            summary = authenticated_client.get(SETTINGS).json()["status"]
            assert (summary["peers_total"], summary["peers_enabled"], summary["peers_online"]) == (2, 1, 1)
        finally:
            on_app_loop(authenticated_client, redis.set_tunnel_peer_status, "openvpn", "gw-test-a", {})
        _reset(authenticated_client)


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

    def test_a_device_shows_its_flushed_totals_plus_its_window(
        self, authenticated_client: TestClient, created_project: dict[str, Any]
    ) -> None:
        """Every view of a device sums the totals a worker flushed and the window the gateways have not."""
        url = _peers_url(created_project)
        _reset(authenticated_client)
        peer = authenticated_client.post(url, json={"name": "tv"}).json()
        _count_device(
            authenticated_client,
            peer["id"],
            flushed=TunnelPeerMetricDelta(
                request_count=5, success_count=4, failure_count=1, latency_sum_ms=500.0,
                bytes_sent=100, bytes_received=1000, by_address=2, encrypted_dns_blocked=1,
            ),
            window=TunnelPeerMetricDelta(
                request_count=2, success_count=2, latency_sum_ms=100.0, bytes_sent=10, bytes_received=20, by_address=1,
            ),
        )
        counted = {
            "request_count": 7, "success_count": 6, "failure_count": 1, "avg_latency_ms": round(600 / 7, 2),
            "bytes_sent": 110, "bytes_received": 1020, "connections_by_address": 3, "encrypted_dns_blocked": 1,
        }

        assert authenticated_client.get(url).json()["peers"][0]["metrics"] == counted
        assert authenticated_client.get(f"{url}/{peer['id']}").json()["metrics"] == counted
        status = authenticated_client.get(SETTINGS).json()["status"]
        assert (status["connections_by_address"], status["encrypted_dns_blocked"]) == (3, 1)
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
        assert authenticated_client.delete(f"{url}/{peer['id']}").status_code == 404
        assert authenticated_client.get(url).json()["total"] == 0

    def test_another_projects_device_is_not_found(
        self, authenticated_client: TestClient, created_project: dict[str, Any], sample_project_data: dict[str, Any]
    ) -> None:
        peer = authenticated_client.post(_peers_url(created_project), json={"name": "tv"}).json()
        other = authenticated_client.post(
            "/api/v1/projects",
            json={
                **sample_project_data,
                "name": f"{sample_project_data['name']} 2",
                "username": f"{sample_project_data['username']}2",
            },
        ).json()
        url = f"{_peers_url(other)}/{peer['id']}"
        assert authenticated_client.get(url).status_code == 404
        assert authenticated_client.patch(url, json={"name": "mine now"}).status_code == 404
        assert authenticated_client.post(f"{url}/rotate-certificate").status_code == 404
        assert authenticated_client.get(f"{url}/config").status_code == 404
        assert authenticated_client.get(f"{url}/metrics/history").status_code == 404
        assert authenticated_client.delete(url).status_code == 404
        assert authenticated_client.get(f"{_peers_url(created_project)}/{peer['id']}").json()["serial"] == peer["serial"]

    def test_deleting_the_project_removes_its_peers(self, authenticated_client: TestClient, created_project: dict[str, Any]) -> None:
        url = _peers_url(created_project)
        peer = authenticated_client.post(url, json={"name": "tv"}).json()
        assert _stored(authenticated_client, peer["id"]) is not None
        _delete_project(authenticated_client, created_project["id"])
        assert authenticated_client.get(url).status_code == 404
        # The rows went with the project; every gateway ends the device's session as the feed carries that.
        assert _stored(authenticated_client, peer["id"]) is None
