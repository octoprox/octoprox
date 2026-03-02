# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for TLS ClientHello parsing, JA3/JA4 fingerprinting, and serialization."""

import hashlib

import dpkt.dpkt
import pytest

from api.core.mitm.client_hello import (
    GREASE_VALUES,
    ClientHelloInfo,
    client_hello_to_dict,
    compute_ja3,
    compute_ja4,
    compute_ja4_r,
    parse_client_hello,
)

# ---------------------------------------------------------------------------
# Test handshake payloads (built with dpkt-compatible binary encoding)
# ---------------------------------------------------------------------------

# Full ClientHello: TLS 1.2 record version, 3 cipher suites (0x1301, 0x1302, 0xC02F),
# extensions: SNI=example.com, supported_groups=[x25519, secp256r1],
# ec_point_formats=[0], sig_algs=[ecdsa_secp256r1_sha256, rsa_pss_rsae_sha256],
# ALPN=[h2, http/1.1], supported_versions=[TLS 1.3, TLS 1.2]
_BASIC_HANDSHAKE = bytes.fromhex(
    "01000078030300000000000000000000000000000000000000000000000000000000000000000000"
    "0613011302c02f0100004900000010000e00000b6578616d706c652e636f6d000a00060004001d00"
    "17000b00020100000d00060004040308040010000e000c02683208687474702f312e31002b000504"
    "03040303"
)

# Same structure but includes GREASE values in cipher suites (0x0A0A, 0x1A1A),
# extensions (0x2A2A), supported_groups (0x4A4A), supported_versions (0x3A3A).
# SNI=test.example.org, ALPN=[h2], sig_algs=[0x0403, 0x0804, 0x0805]
_GREASE_HANDSHAKE = bytes.fromhex(
    "01000082030300000000000000000000000000000000000000000000000000000000000000000000"
    "0a0a0a13011a1a1302c02f0100004f2a2a0000000000150013000010746573742e6578616d706c65"
    "2e6f7267000a000800064a4a001d0017000b00020100000d00080006040308040805001000050003"
    "026832002b0007063a3a03040303"
)

# Minimal ClientHello: TLS 1.0, single cipher (0x002F), no extensions at all
_MINIMAL_HANDSHAKE = bytes.fromhex(
    "01000029030100000000000000000000000000000000000000000000000000000000000000000000"
    "02002f0100"
)


# ---------------------------------------------------------------------------
# Parsing tests
# ---------------------------------------------------------------------------


class TestParseClientHello:
    """Tests for parse_client_hello()."""

    def test_basic_version(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        assert info.version == 0x0303

    def test_basic_cipher_suites(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        assert info.cipher_suites == [0x1301, 0x1302, 0xC02F]

    def test_basic_cipher_suite_names_from_dpkt(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        assert info.cipher_suite_names[0x1301] == "TLS_AES_128_GCM_SHA256"
        assert info.cipher_suite_names[0x1302] == "TLS_AES_256_GCM_SHA384"
        assert info.cipher_suite_names[0xC02F] == "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256"

    def test_basic_sni(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        assert info.sni == "example.com"

    def test_basic_extensions(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        assert info.extensions == [0x0000, 0x000A, 0x000B, 0x000D, 0x0010, 0x002B]

    def test_basic_supported_groups(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        assert info.supported_groups == [0x001D, 0x0017]

    def test_basic_ec_point_formats(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        assert info.ec_point_formats == [0]

    def test_basic_signature_algorithms(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        assert info.signature_algorithms == [0x0403, 0x0804]

    def test_basic_alpn(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        assert info.alpn == ["h2", "http/1.1"]

    def test_basic_supported_versions(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        assert info.supported_versions == [0x0304, 0x0303]

    def test_grease_cipher_suites_preserved_in_list(self) -> None:
        """GREASE values should be present in raw parsed data (filtering is at JA3/JA4/dict level)."""
        info = parse_client_hello(_GREASE_HANDSHAKE)
        assert 0x0A0A in info.cipher_suites
        assert 0x1A1A in info.cipher_suites

    def test_grease_extensions_preserved(self) -> None:
        info = parse_client_hello(_GREASE_HANDSHAKE)
        assert 0x2A2A in info.extensions

    def test_grease_supported_groups_preserved(self) -> None:
        info = parse_client_hello(_GREASE_HANDSHAKE)
        assert 0x4A4A in info.supported_groups

    def test_grease_supported_versions_preserved(self) -> None:
        info = parse_client_hello(_GREASE_HANDSHAKE)
        assert 0x3A3A in info.supported_versions

    def test_grease_sni(self) -> None:
        info = parse_client_hello(_GREASE_HANDSHAKE)
        assert info.sni == "test.example.org"

    def test_minimal_version(self) -> None:
        info = parse_client_hello(_MINIMAL_HANDSHAKE)
        assert info.version == 0x0301

    def test_minimal_single_cipher(self) -> None:
        info = parse_client_hello(_MINIMAL_HANDSHAKE)
        assert info.cipher_suites == [0x002F]

    def test_minimal_no_extensions(self) -> None:
        info = parse_client_hello(_MINIMAL_HANDSHAKE)
        assert info.extensions == []
        assert info.sni == ""
        assert info.alpn == []
        assert info.supported_groups == []
        assert info.supported_versions == []

    def test_invalid_data_raises(self) -> None:
        with pytest.raises(dpkt.dpkt.NeedData):
            parse_client_hello(b"\x00\x00\x00")


# ---------------------------------------------------------------------------
# JA3 tests
# ---------------------------------------------------------------------------


class TestComputeJA3:
    """Tests for compute_ja3()."""

    def test_basic_ja3_full_string(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        _, ja3_full = compute_ja3(info)
        # version=771(0x0303), ciphers=4865-4866-49199, exts=0-10-11-13-16-43,
        # groups=29-23, ec_formats=0
        assert ja3_full == "771,4865-4866-49199,0-10-11-13-16-43,29-23,0"

    def test_basic_ja3_hash(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        ja3_hash, ja3_full = compute_ja3(info)
        expected = hashlib.md5(ja3_full.encode()).hexdigest()  # noqa: S324
        assert ja3_hash == expected
        assert ja3_hash == "c9e264cb3675678ee364e81f3b6da7ad"

    def test_grease_filtered_from_ja3(self) -> None:
        """GREASE values in ciphers, extensions, and groups should be filtered out."""
        info = parse_client_hello(_GREASE_HANDSHAKE)
        _, ja3_full = compute_ja3(info)
        # Same non-GREASE values as basic → same JA3 string
        assert ja3_full == "771,4865-4866-49199,0-10-11-13-16-43,29-23,0"

    def test_minimal_ja3(self) -> None:
        info = parse_client_hello(_MINIMAL_HANDSHAKE)
        ja3_hash, ja3_full = compute_ja3(info)
        assert ja3_full == "769,47,,,"

    def test_ja3_from_dataclass_directly(self) -> None:
        """compute_ja3 should work with a manually constructed ClientHelloInfo."""
        info = ClientHelloInfo(
            version=0x0303,
            cipher_suites=[0x1301, 0x1302],
            extensions=[0x0000, 0x000D],
            supported_groups=[0x001D],
            ec_point_formats=[0],
        )
        _, ja3_full = compute_ja3(info)
        assert ja3_full == "771,4865-4866,0-13,29,0"


# ---------------------------------------------------------------------------
# JA4 tests
# ---------------------------------------------------------------------------


class TestComputeJA4:
    """Tests for compute_ja4()."""

    def test_basic_ja4_part_a(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        ja4 = compute_ja4(info)
        part_a = ja4.split("_")[0]
        # t=TCP, 13=TLS1.3 (from supported_versions), d=has SNI,
        # 03=3 ciphers, 06=6 extensions, h2=first ALPN
        assert part_a == "t13d0306h2"

    def test_basic_ja4_format(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        ja4 = compute_ja4(info)
        parts = ja4.split("_")
        assert len(parts) == 3
        assert len(parts[1]) == 12  # sha256 truncated to 12 hex chars
        assert len(parts[2]) == 12

    def test_basic_ja4_full(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        ja4 = compute_ja4(info)
        assert ja4 == "t13d0306h2_40b44b994229_fb71836bce29"

    def test_grease_filtered_from_ja4_counts(self) -> None:
        """GREASE values should NOT be counted in cipher/extension counts."""
        info = parse_client_hello(_GREASE_HANDSHAKE)
        ja4 = compute_ja4(info)
        part_a = ja4.split("_")[0]
        # Same 3 non-GREASE ciphers, 6 non-GREASE extensions
        assert part_a == "t13d0306h2"

    def test_grease_same_cipher_hash(self) -> None:
        """GREASE handshake should produce same part_b as basic (same non-GREASE ciphers)."""
        basic_ja4 = compute_ja4(parse_client_hello(_BASIC_HANDSHAKE))
        grease_ja4 = compute_ja4(parse_client_hello(_GREASE_HANDSHAKE))
        assert basic_ja4.split("_")[1] == grease_ja4.split("_")[1]

    def test_grease_different_part_c(self) -> None:
        """Part c differs because GREASE handshake has 3 sig_algs vs basic's 2."""
        basic_ja4 = compute_ja4(parse_client_hello(_BASIC_HANDSHAKE))
        grease_ja4 = compute_ja4(parse_client_hello(_GREASE_HANDSHAKE))
        assert basic_ja4.split("_")[2] != grease_ja4.split("_")[2]

    def test_minimal_ja4_no_sni_no_alpn(self) -> None:
        info = parse_client_hello(_MINIMAL_HANDSHAKE)
        ja4 = compute_ja4(info)
        part_a = ja4.split("_")[0]
        # t=TCP, 10=TLS1.0 (version 0x0301), i=no SNI, 1=1 cipher, 0=0 extensions, 00=no ALPN
        assert part_a == "t10i010000"

    def test_ja4_h1_alpn(self) -> None:
        """http/1.1 ALPN should produce 'h1' in JA4 part_a."""
        info = ClientHelloInfo(
            version=0x0303,
            cipher_suites=[0x1301],
            extensions=[0x0010],
            alpn=["http/1.1"],
            sni="example.com",
        )
        ja4 = compute_ja4(info)
        assert ja4.split("_")[0].endswith("h1")

    def test_ja4_no_alpn(self) -> None:
        """No ALPN should produce '00' in JA4 part_a."""
        info = ClientHelloInfo(
            version=0x0303,
            cipher_suites=[0x1301],
            extensions=[],
        )
        ja4 = compute_ja4(info)
        assert ja4.split("_")[0].endswith("00")

    def test_ja4_alpn_first_last_char(self) -> None:
        """ALPN string should be first + last character of the value."""
        info = ClientHelloInfo(
            version=0x0303,
            cipher_suites=[0x1301],
            extensions=[],
            alpn=["spdy/3.1"],
            sni="example.com",
        )
        ja4 = compute_ja4(info)
        # first='s', last='1' → "s1"
        assert ja4.split("_")[0].endswith("s1")

    def test_ja4_sni_and_alpn_excluded_from_part_c(self) -> None:
        """SNI and ALPN extensions should be excluded from part_c hash."""
        # With SNI + ALPN + another extension
        info_with = ClientHelloInfo(
            version=0x0303,
            cipher_suites=[0x1301],
            extensions=[0x0000, 0x000D, 0x0010],  # SNI, sig_algs, ALPN
            signature_algorithms=[0x0403],
            sni="example.com",
            alpn=["h2"],
        )
        # Without SNI and ALPN
        info_without = ClientHelloInfo(
            version=0x0303,
            cipher_suites=[0x1301],
            extensions=[0x000D],  # just sig_algs
            signature_algorithms=[0x0403],
        )
        ja4_with = compute_ja4(info_with)
        ja4_without = compute_ja4(info_without)
        # part_c should be identical since SNI and ALPN are excluded
        assert ja4_with.split("_")[2] == ja4_without.split("_")[2]


# ---------------------------------------------------------------------------
# JA4_r tests
# ---------------------------------------------------------------------------


class TestComputeJA4r:
    """Tests for compute_ja4_r()."""

    def test_basic_ja4_r_part_a_matches_ja4(self) -> None:
        """Part a should be identical to JA4."""
        info = parse_client_hello(_BASIC_HANDSHAKE)
        ja4 = compute_ja4(info)
        ja4_r = compute_ja4_r(info)
        assert ja4_r.split("_")[0] == ja4.split("_")[0]

    def test_basic_ja4_r_has_four_parts(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        ja4_r = compute_ja4_r(info)
        parts = ja4_r.split("_")
        assert len(parts) == 4

    def test_basic_ja4_r_part_b_raw_sorted_ciphers(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        ja4_r = compute_ja4_r(info)
        part_b = ja4_r.split("_")[1]
        # 3 ciphers sorted: 0x1301, 0x1302, 0xC02F → 1301,1302,c02f
        assert part_b == "1301,1302,c02f"

    def test_basic_ja4_r_part_c_raw_sorted_exts(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        ja4_r = compute_ja4_r(info)
        part_c = ja4_r.split("_")[2]
        # Extensions sorted, SNI (0x0000) and ALPN (0x0010) excluded:
        # 0x000A, 0x000B, 0x000D, 0x002B → 000a,000b,000d,002b
        assert part_c == "000a,000b,000d,002b"

    def test_basic_ja4_r_part_d_sig_algs_original_order(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        ja4_r = compute_ja4_r(info)
        part_d = ja4_r.split("_")[3]
        # sig_algs in original order: 0x0403, 0x0804
        assert part_d == "0403,0804"

    def test_basic_ja4_r_full(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        ja4_r = compute_ja4_r(info)
        assert ja4_r == "t13d0306h2_1301,1302,c02f_000a,000b,000d,002b_0403,0804"

    def test_grease_filtered_from_ja4_r(self) -> None:
        """GREASE values should NOT appear in any part of JA4_r."""
        info = parse_client_hello(_GREASE_HANDSHAKE)
        ja4_r = compute_ja4_r(info)
        for part in ja4_r.split("_"):
            assert "0a0a" not in part
            assert "1a1a" not in part
            assert "2a2a" not in part
            assert "4a4a" not in part

    def test_grease_ja4_r_same_ciphers_as_basic(self) -> None:
        """GREASE handshake should produce same part_b as basic (same non-GREASE ciphers)."""
        basic = compute_ja4_r(parse_client_hello(_BASIC_HANDSHAKE))
        grease = compute_ja4_r(parse_client_hello(_GREASE_HANDSHAKE))
        assert basic.split("_")[1] == grease.split("_")[1]

    def test_minimal_ja4_r_empty_parts(self) -> None:
        info = parse_client_hello(_MINIMAL_HANDSHAKE)
        ja4_r = compute_ja4_r(info)
        parts = ja4_r.split("_")
        assert parts[0] == "t10i010000"
        assert parts[1] == "002f"  # single cipher
        assert parts[2] == ""  # no extensions
        assert parts[3] == ""  # no sig_algs


# ---------------------------------------------------------------------------
# Serialization tests
# ---------------------------------------------------------------------------


class TestClientHelloToDict:
    """Tests for client_hello_to_dict()."""

    def test_basic_structure(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        ja3_hash, ja3_full = compute_ja3(info)
        ja4 = compute_ja4(info)
        ja4_r = compute_ja4_r(info)
        d = client_hello_to_dict(info, ja3_hash, ja3_full, ja4, ja4_r)

        assert d["version"] == "TLS 1.2"
        assert d["sni"] == "example.com"
        assert d["alpn"] == ["h2", "http/1.1"]
        assert d["ja3"] == ja3_hash
        assert d["ja3_full"] == ja3_full
        assert d["ja4"] == ja4
        assert d["ja4_r"] == ja4_r

    def test_supported_versions_human_readable(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        d = client_hello_to_dict(info, "", "", "", "")
        assert d["supported_versions"] == ["TLS 1.3", "TLS 1.2"]

    def test_cipher_suites_have_id_and_name(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        d = client_hello_to_dict(info, "", "", "", "")
        cs = d["cipher_suites"]
        assert len(cs) == 3
        assert cs[0] == {"id": "0x1301", "name": "TLS_AES_128_GCM_SHA256"}
        assert cs[2] == {"id": "0xC02F", "name": "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256"}

    def test_cipher_suite_names_from_dpkt(self) -> None:
        """Cipher suite names should come from dpkt, not a hardcoded table."""
        info = parse_client_hello(_BASIC_HANDSHAKE)
        d = client_hello_to_dict(info, "", "", "", "")
        for cs in d["cipher_suites"]:
            assert "Unknown" not in cs["name"]

    def test_extensions_have_id_and_name(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        d = client_hello_to_dict(info, "", "", "", "")
        ext_names = [e["name"] for e in d["extensions"]]
        assert "server_name" in ext_names
        assert "supported_groups" in ext_names
        assert "supported_versions" in ext_names

    def test_supported_groups_have_id_and_name(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        d = client_hello_to_dict(info, "", "", "", "")
        groups = d["supported_groups"]
        assert len(groups) == 2
        assert groups[0] == {"id": 29, "name": "x25519"}
        assert groups[1] == {"id": 23, "name": "secp256r1"}

    def test_signature_algorithms_have_id_and_name(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        d = client_hello_to_dict(info, "", "", "", "")
        sig_algs = d["signature_algorithms"]
        assert len(sig_algs) == 2
        assert sig_algs[0] == {"id": "0x0403", "name": "ecdsa_secp256r1_sha256"}

    def test_grease_filtered_from_dict(self) -> None:
        """GREASE values should NOT appear in serialized output."""
        info = parse_client_hello(_GREASE_HANDSHAKE)
        d = client_hello_to_dict(info, "", "", "", "")

        cipher_ids = [cs["id"] for cs in d["cipher_suites"]]
        assert "0x0A0A" not in cipher_ids
        assert "0x1A1A" not in cipher_ids

        ext_ids = [e["id"] for e in d["extensions"]]
        assert 0x2A2A not in ext_ids

        group_ids = [g["id"] for g in d["supported_groups"]]
        assert 0x4A4A not in group_ids

    def test_grease_filtered_from_supported_versions(self) -> None:
        info = parse_client_hello(_GREASE_HANDSHAKE)
        d = client_hello_to_dict(info, "", "", "", "")
        assert d["supported_versions"] == ["TLS 1.3", "TLS 1.2"]

    def test_minimal_empty_lists(self) -> None:
        info = parse_client_hello(_MINIMAL_HANDSHAKE)
        d = client_hello_to_dict(info, "", "", "", "")
        assert d["version"] == "TLS 1.0"
        assert d["supported_versions"] == []
        assert d["extensions"] == []
        assert d["supported_groups"] == []
        assert d["signature_algorithms"] == []
        assert d["alpn"] == []
        assert d["sni"] == ""

    def test_ec_point_formats_passthrough(self) -> None:
        info = parse_client_hello(_BASIC_HANDSHAKE)
        d = client_hello_to_dict(info, "", "", "", "")
        assert d["ec_point_formats"] == [0]


# ---------------------------------------------------------------------------
# GREASE utility tests
# ---------------------------------------------------------------------------


class TestGREASE:
    """Tests for GREASE value detection."""

    def test_all_grease_values_present(self) -> None:
        """Verify all 16 GREASE values per RFC 8701 are in the set."""
        assert len(GREASE_VALUES) == 16
        for val in [0x0A0A, 0x1A1A, 0x2A2A, 0x3A3A, 0x4A4A, 0x5A5A,
                    0x6A6A, 0x7A7A, 0x8A8A, 0x9A9A, 0xAAAA, 0xBABA,
                    0xCACA, 0xDADA, 0xEAEA, 0xFAFA]:
            assert val in GREASE_VALUES

    def test_non_grease_not_matched(self) -> None:
        assert 0x1301 not in GREASE_VALUES
        assert 0x0000 not in GREASE_VALUES
        assert 0xFFFF not in GREASE_VALUES
