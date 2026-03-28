# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for HTTP/2 fingerprint extraction and Akamai hash computation."""

import hashlib

from api.core.mitm.client_hello import (
    Http2Fingerprint,
    compute_akamai,
    h2_fingerprint_to_dict,
)


class TestComputeAkamai:
    """Tests for compute_akamai()."""

    def test_basic_fingerprint(self) -> None:
        """Known SETTINGS + WINDOW_UPDATE + pseudo-header order produces correct Akamai string."""
        fp = Http2Fingerprint(
            settings=[(4, 16777216)],
            window_update=16711681,
            has_priority=False,
            pseudo_header_order=[":method", ":path", ":authority", ":scheme"],
            frames=[],
        )

        akamai_hash, akamai_full = compute_akamai(fp)

        assert akamai_full == "4:16777216|16711681|0|m,p,a,s"
        expected_hash = hashlib.md5(akamai_full.encode()).hexdigest()
        assert akamai_hash == expected_hash

    def test_multiple_settings(self) -> None:
        """Multiple SETTINGS parameters are joined with semicolons."""
        fp = Http2Fingerprint(
            settings=[
                (1, 65536),
                (3, 1000),
                (4, 6291456),
            ],
            window_update=15663105,
            has_priority=True,
            pseudo_header_order=[":method", ":authority", ":scheme", ":path"],
            frames=[],
        )

        akamai_hash, akamai_full = compute_akamai(fp)

        assert akamai_full == "1:65536;3:1000;4:6291456|15663105|1|m,a,s,p"
        expected_hash = hashlib.md5(akamai_full.encode()).hexdigest()
        assert akamai_hash == expected_hash

    def test_no_window_update(self) -> None:
        """When no WINDOW_UPDATE is sent, the increment should be 0."""
        fp = Http2Fingerprint(
            settings=[(4, 16777216)],
            window_update=0,
            has_priority=False,
            pseudo_header_order=[":method", ":path", ":authority", ":scheme"],
            frames=[],
        )

        _hash, akamai_full = compute_akamai(fp)

        assert akamai_full == "4:16777216|0|0|m,p,a,s"

    def test_empty_settings(self) -> None:
        """Empty SETTINGS frame produces empty settings part."""
        fp = Http2Fingerprint(
            settings=[],
            window_update=0,
            has_priority=False,
            pseudo_header_order=[":method", ":path", ":authority", ":scheme"],
            frames=[],
        )

        _hash, akamai_full = compute_akamai(fp)

        assert akamai_full == "|0|0|m,p,a,s"

    def test_empty_pseudo_headers(self) -> None:
        """Missing pseudo-headers produces empty order part."""
        fp = Http2Fingerprint(
            settings=[(4, 16777216)],
            window_update=0,
            has_priority=False,
            pseudo_header_order=[],
            frames=[],
        )

        _hash, akamai_full = compute_akamai(fp)

        assert akamai_full == "4:16777216|0|0|"

    def test_priority_flag_set(self) -> None:
        """Priority flag is '1' when has_priority is True."""
        fp = Http2Fingerprint(
            settings=[(4, 16777216)],
            window_update=0,
            has_priority=True,
            pseudo_header_order=[":method", ":path", ":authority", ":scheme"],
            frames=[],
        )

        _hash, akamai_full = compute_akamai(fp)

        assert "|1|" in akamai_full


class TestH2FingerprintToDict:
    """Tests for h2_fingerprint_to_dict()."""

    def test_serialization(self) -> None:
        """Fingerprint serializes to dict with human-readable setting names."""
        fp = Http2Fingerprint(
            settings=[
                (1, 65536),
                (4, 16777216),
            ],
            window_update=16711681,
            has_priority=False,
            pseudo_header_order=[":method", ":path", ":authority", ":scheme"],
            frames=[
                {
                    "type": "SETTINGS",
                    "stream_id": 0,
                    "settings": {
                        "HEADER_TABLE_SIZE": 65536,
                        "INITIAL_WINDOW_SIZE": 16777216,
                    },
                },
                {
                    "type": "WINDOW_UPDATE",
                    "stream_id": 0,
                    "delta": 16711681,
                },
            ],
        )

        result = h2_fingerprint_to_dict(fp)

        assert result["akamai_hash"] == hashlib.md5(
            b"1:65536;4:16777216|16711681|0|m,p,a,s"
        ).hexdigest()
        assert result["akamai"] == "1:65536;4:16777216|16711681|0|m,p,a,s"
        assert result["window_update"] == 16711681
        assert result["has_priority"] is False
        assert result["pseudo_header_order"] == [":method", ":path", ":authority", ":scheme"]
        assert len(result["settings"]) == 2
        assert result["settings"][0]["id"] == 1
        assert result["settings"][0]["name"] == "HEADER_TABLE_SIZE"
        assert result["settings"][0]["value"] == 65536
        assert len(result["frames"]) == 2
