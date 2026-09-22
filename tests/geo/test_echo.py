# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the echo endpoint helpers and the standalone echo app."""

from pathlib import Path

from fastapi.testclient import TestClient

from api.echo_main import create_echo_app
from api.geo.echo import build_echo, client_ip, parse_trusted
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb


class TestClientIp:
    def test_peer_by_default(self) -> None:
        assert client_ip("203.0.113.7", "198.51.100.1", []) == "203.0.113.7"

    def test_forwarded_only_from_trusted_peer(self) -> None:
        trusted = parse_trusted(["10.0.0.0/8", "bad", ""])
        assert client_ip("10.1.2.3", "198.51.100.1, 10.1.2.3", trusted) == "198.51.100.1"
        assert client_ip("203.0.113.7", "198.51.100.1", trusted) == "203.0.113.7"
        assert client_ip("10.1.2.3", "garbage", trusted) == "10.1.2.3"

    def test_rightmost_untrusted_hop_is_the_exit(self) -> None:
        """A proxy that appends the requester's address must not be echoed back as the exit."""
        trusted = parse_trusted(["10.0.0.0/8"])
        # Octoprox (203.0.113.7) -> vendor proxy adds it -> exit 198.51.100.1 -> HAProxy appends the exit.
        assert client_ip("10.1.2.3", "203.0.113.7, 198.51.100.1", trusted) == "198.51.100.1"
        # Trusted hops to the right (a second balancer) are skipped; junk is skipped.
        assert client_ip("10.1.2.3", "198.51.100.1, 10.9.9.9", trusted) == "198.51.100.1"
        assert client_ip("10.1.2.3", "198.51.100.1, junk", trusted) == "198.51.100.1"
        # Nothing but trusted hops: fall back to the peer rather than inventing an exit.
        assert client_ip("10.1.2.3", "10.5.5.5", trusted) == "10.1.2.3"

    def test_invalid_peer(self) -> None:
        assert client_ip(None, None, []) is None
        assert client_ip("localhost", None, []) is None


class TestBuildEcho:
    def test_without_store(self) -> None:
        response = build_echo("203.0.113.7", None, nonce="abc")
        assert response.ip == "203.0.113.7" and response.country is None and response.nonce == "abc"
        assert response.databases == []


class TestStandaloneApp:
    def test_echo_and_health(self, tmp_path: Path) -> None:
        db = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
        # Every balancer hop is listed as trusted; the rightmost hop outside them is the exit.
        app = create_echo_app([str(db)], ["127.0.0.0/8", "10.0.0.0/8"])
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            health = client.get("/health").json()
            assert health["status"] == "healthy" and health["databases"] == ["city.mmdb"]

            plain = client.get("/echo?nonce=n1").json()
            assert plain["ip"] == "127.0.0.1" and plain["nonce"] == "n1" and "country" not in plain

            forwarded = client.get("/echo", headers={"X-Forwarded-For": "81.2.69.160, 10.0.0.1"}).json()
            assert forwarded["ip"] == "81.2.69.160"
            # A proxy that prepends the requester's address: the exit is still the hop the balancer saw.
            via_proxy = client.get("/echo", headers={"X-Forwarded-For": "203.0.113.7, 81.2.69.160"}).json()
            assert via_proxy["ip"] == "81.2.69.160"
            assert forwarded["country"] == "GB" and forwarded["city"] == "London"
            assert forwarded["databases"] and forwarded["asn"] == 12345

        untrusted = create_echo_app([str(db)], [])
        with TestClient(untrusted, client=("127.0.0.1", 50000)) as client:
            assert client.get("/echo", headers={"X-Forwarded-For": "81.2.69.160"}).json()["ip"] == "127.0.0.1"
