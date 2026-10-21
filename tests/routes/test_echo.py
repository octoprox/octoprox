# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``/echo`` on an api process: the caller's IP, placed with the api's own IP databases.

The echo role serves the same endpoint on its own (tests/roles/test_app.py);
on the api it answers from the databases the api keeps open, config files
and stored ones alike, and ``geo.echo.enabled`` can turn it off.
"""

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from starlette.testclient import TestClient

from api.core.roles import Role
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb


class TestEcho:
    def test_places_the_caller_with_the_api_databases(self, build_app: Callable[..., FastAPI], tmp_path: Path) -> None:
        db = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
        app = build_app(Role.API, geo_databases=[{"path": str(db)}], geo_echo_trusted_proxies=["127.0.0.0/8"])
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            # Behind a trusted balancer the client is the X-Forwarded-For hop it saw.
            forwarded = client.get("/echo", params={"nonce": "n1"}, headers={"X-Forwarded-For": "81.2.69.160"})
            assert forwarded.status_code == 200
            body = forwarded.json()
            assert (body["ip"], body["country"], body["city"], body["nonce"]) == ("81.2.69.160", "GB", "London", "n1")
            assert body["databases"]
            # Without the header the peer itself is the caller; no database knows it.
            direct = client.get("/echo").json()
            assert direct["ip"] == "127.0.0.1" and "country" not in direct

    def test_an_untrusted_forwarded_for_is_ignored(self, build_app: Callable[..., FastAPI]) -> None:
        app = build_app(Role.API, geo_echo_trusted_proxies=[])
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            assert client.get("/echo", headers={"X-Forwarded-For": "81.2.69.160"}).json()["ip"] == "127.0.0.1"

    def test_needs_no_sign_in_and_a_peer_address(self, async_client: TestClient) -> None:
        # The test client's peer is "testclient", not an address.
        assert async_client.get("/echo").status_code == 400

    def test_can_be_turned_off(self, build_app: Callable[..., FastAPI]) -> None:
        with TestClient(build_app(Role.API, geo_echo_enabled=False), client=("127.0.0.1", 50000)) as client:
            assert client.get("/echo").status_code == 404
