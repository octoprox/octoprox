# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the echo endpoint helpers: which address is the client's, and the answer without databases."""

from api.geo.echo import build_echo, client_ip, parse_trusted


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
        # Octoprox (203.0.113.7) -> vendor proxy adds it -> exit 198.51.100.1 -> the load balancer appends the exit.
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

