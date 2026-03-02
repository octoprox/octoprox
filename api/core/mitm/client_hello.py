# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""TLS ClientHello parser with JA3/JA4 fingerprint computation.

Parses raw TLS ClientHello handshake bytes captured via ``ssl.SSLContext._msg_callback``
and computes JA3 (MD5-based) and JA4 (SHA256-based) fingerprints.

Uses ``dpkt`` for the heavy-lifting of binary ClientHello parsing.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Any

import dpkt.ssl  # type: ignore[import-untyped]

# ---------------------------------------------------------------------------
# GREASE values (RFC 8701) — filtered out when computing fingerprints
# ---------------------------------------------------------------------------

GREASE_VALUES: frozenset[int] = frozenset(
    {
        0x0A0A, 0x1A1A, 0x2A2A, 0x3A3A, 0x4A4A, 0x5A5A,
        0x6A6A, 0x7A7A, 0x8A8A, 0x9A9A, 0xAAAA, 0xBABA,
        0xCACA, 0xDADA, 0xEAEA, 0xFAFA,
    }
)

# ---------------------------------------------------------------------------
# TLS extension type IDs
# ---------------------------------------------------------------------------

_EXT_SERVER_NAME = 0x0000
_EXT_SUPPORTED_GROUPS = 0x000A
_EXT_EC_POINT_FORMATS = 0x000B
_EXT_SIGNATURE_ALGORITHMS = 0x000D
_EXT_ALPN = 0x0010
_EXT_SUPPORTED_VERSIONS = 0x002B

# ---------------------------------------------------------------------------
# Human-readable name lookup tables
# ---------------------------------------------------------------------------

EXTENSION_NAMES: dict[int, str] = {
    0x0000: "server_name",
    0x0001: "max_fragment_length",
    0x0002: "client_certificate_url",
    0x0003: "trusted_ca_keys",
    0x0004: "truncated_hmac",
    0x0005: "status_request",
    0x000A: "supported_groups",
    0x000B: "ec_point_formats",
    0x000D: "signature_algorithms",
    0x000E: "use_srtp",
    0x000F: "heartbeat",
    0x0010: "application_layer_protocol_negotiation",
    0x0012: "signed_certificate_timestamp",
    0x0015: "padding",
    0x0016: "encrypt_then_mac",
    0x0017: "extended_master_secret",
    0x001C: "record_size_limit",
    0x0023: "session_ticket",
    0x0029: "pre_shared_key",
    0x002A: "early_data",
    0x002B: "supported_versions",
    0x002C: "cookie",
    0x002D: "psk_key_exchange_modes",
    0x0031: "certificate_authorities",
    0x0032: "oid_filters",
    0x0033: "key_share",
    0x0039: "compress_certificate",
    0x4469: "application_settings",
    0xFE0D: "encrypted_client_hello",
    0xFF01: "renegotiation_info",
}

SUPPORTED_GROUP_NAMES: dict[int, str] = {
    0x0017: "secp256r1",
    0x0018: "secp384r1",
    0x0019: "secp521r1",
    0x001D: "x25519",
    0x001E: "x448",
    0x0100: "ffdhe2048",
    0x0101: "ffdhe3072",
    0x0102: "ffdhe4096",
    0x0103: "ffdhe6144",
    0x0104: "ffdhe8192",
    # Post-quantum / hybrid
    0x6399: "X25519Kyber768Draft00",
    0x4588: "X25519MLKEM768",
    0x0200: "secp256r1MLKEM768",
}

SIGNATURE_ALGORITHM_NAMES: dict[int, str] = {
    0x0201: "rsa_pkcs1_sha1",
    0x0301: "ecdsa_sha1",
    0x0401: "rsa_pkcs1_sha256",  # legacy
    0x0403: "ecdsa_secp256r1_sha256",
    0x0501: "rsa_pkcs1_sha384",  # legacy
    0x0503: "ecdsa_secp384r1_sha384",
    0x0601: "rsa_pkcs1_sha512",  # legacy
    0x0603: "ecdsa_secp521r1_sha512",
    0x0804: "rsa_pss_rsae_sha256",
    0x0805: "rsa_pss_rsae_sha384",
    0x0806: "rsa_pss_rsae_sha512",
    0x0807: "ed25519",
    0x0808: "ed448",
    0x0809: "rsa_pss_pss_sha256",
    0x080A: "rsa_pss_pss_sha384",
    0x080B: "rsa_pss_pss_sha512",
}

TLS_VERSION_NAMES: dict[int, str] = {
    0x0301: "TLS 1.0",
    0x0302: "TLS 1.1",
    0x0303: "TLS 1.2",
    0x0304: "TLS 1.3",
}


# ---------------------------------------------------------------------------
# ClientHelloInfo dataclass
# ---------------------------------------------------------------------------


@dataclass
class ClientHelloInfo:
    """Parsed TLS ClientHello fields."""

    version: int  # ClientHello protocol version (e.g. 0x0303 for TLS 1.2)
    cipher_suites: list[int] = field(default_factory=list)  # 2-byte IDs
    cipher_suite_names: dict[int, str] = field(default_factory=dict)  # code → name from dpkt
    extensions: list[int] = field(default_factory=list)  # extension type IDs (ordered)
    supported_groups: list[int] = field(default_factory=list)  # elliptic curves
    ec_point_formats: list[int] = field(default_factory=list)
    signature_algorithms: list[int] = field(default_factory=list)
    supported_versions: list[int] = field(default_factory=list)
    sni: str = ""
    alpn: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Extension parsing helpers
# ---------------------------------------------------------------------------


def _parse_sni(data: bytes) -> str:
    """Parse server_name extension data → hostname string."""
    if len(data) < 5:
        return ""
    # SNI list length (2 bytes), then entries: type (1) + name length (2) + name
    offset = 2  # skip list length
    if offset >= len(data):
        return ""
    name_type = data[offset]
    if name_type != 0:  # host_name
        return ""
    offset += 1
    if offset + 2 > len(data):
        return ""
    name_len = struct.unpack("!H", data[offset : offset + 2])[0]
    offset += 2
    if offset + name_len > len(data):
        return ""
    return data[offset : offset + name_len].decode("ascii", errors="replace")


def _parse_supported_groups(data: bytes) -> list[int]:
    """Parse supported_groups extension → list of group IDs."""
    if len(data) < 2:
        return []
    list_len = struct.unpack("!H", data[:2])[0]
    groups: list[int] = []
    offset = 2
    while offset + 2 <= min(2 + list_len, len(data)):
        groups.append(struct.unpack("!H", data[offset : offset + 2])[0])
        offset += 2
    return groups


def _parse_ec_point_formats(data: bytes) -> list[int]:
    """Parse ec_point_formats extension → list of format IDs."""
    if len(data) < 1:
        return []
    list_len = data[0]
    return list(data[1 : 1 + list_len])


def _parse_signature_algorithms(data: bytes) -> list[int]:
    """Parse signature_algorithms extension → list of 2-byte algorithm IDs."""
    if len(data) < 2:
        return []
    list_len = struct.unpack("!H", data[:2])[0]
    algs: list[int] = []
    offset = 2
    while offset + 2 <= min(2 + list_len, len(data)):
        algs.append(struct.unpack("!H", data[offset : offset + 2])[0])
        offset += 2
    return algs


def _parse_alpn(data: bytes) -> list[str]:
    """Parse ALPN extension → list of protocol name strings."""
    if len(data) < 2:
        return []
    list_len = struct.unpack("!H", data[:2])[0]
    protocols: list[str] = []
    offset = 2
    while offset < min(2 + list_len, len(data)):
        proto_len = data[offset]
        offset += 1
        if offset + proto_len > len(data):
            break
        protocols.append(data[offset : offset + proto_len].decode("ascii", errors="replace"))
        offset += proto_len
    return protocols


def _parse_supported_versions(data: bytes) -> list[int]:
    """Parse supported_versions extension (ClientHello variant) → list of version IDs."""
    if len(data) < 1:
        return []
    list_len = data[0]
    versions: list[int] = []
    offset = 1
    while offset + 2 <= min(1 + list_len, len(data)):
        versions.append(struct.unpack("!H", data[offset : offset + 2])[0])
        offset += 2
    return versions


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


def parse_client_hello(data: bytes) -> ClientHelloInfo:
    """Parse raw ClientHello handshake message bytes into structured info.

    ``data`` should be the bytes received from ``ssl.SSLContext._msg_callback``
    for a ClientHello message (content_type=22, msg_type=1).  This typically
    includes the 4-byte handshake header (type + 3-byte length).
    """
    # dpkt.ssl.TLSHandshake expects the full handshake message (type + length + body).
    # The _msg_callback passes the full record body which starts with the handshake
    # type byte.
    handshake = dpkt.ssl.TLSHandshake(data)
    ch = handshake.data  # dpkt.ssl.TLSClientHello

    info = ClientHelloInfo(version=ch.version)

    # --- Cipher suites ---
    # dpkt parses cipher suites into CipherSuite objects with .code (int) and .name (str).
    for cs in getattr(ch, "ciphersuites", []):
        info.cipher_suites.append(cs.code)
        info.cipher_suite_names[cs.code] = cs.name

    # --- Extensions ---
    extensions = getattr(ch, "extensions", None)
    if extensions:
        for ext_type, ext_data in extensions:
            info.extensions.append(ext_type)

            if ext_type == _EXT_SERVER_NAME:
                info.sni = _parse_sni(ext_data)
            elif ext_type == _EXT_SUPPORTED_GROUPS:
                info.supported_groups = _parse_supported_groups(ext_data)
            elif ext_type == _EXT_EC_POINT_FORMATS:
                info.ec_point_formats = _parse_ec_point_formats(ext_data)
            elif ext_type == _EXT_SIGNATURE_ALGORITHMS:
                info.signature_algorithms = _parse_signature_algorithms(ext_data)
            elif ext_type == _EXT_ALPN:
                info.alpn = _parse_alpn(ext_data)
            elif ext_type == _EXT_SUPPORTED_VERSIONS:
                info.supported_versions = _parse_supported_versions(ext_data)

    return info


# ---------------------------------------------------------------------------
# JA3 fingerprint
# ---------------------------------------------------------------------------

def _is_grease(val: int) -> bool:
    return val in GREASE_VALUES


def compute_ja3(info: ClientHelloInfo) -> tuple[str, str]:
    """Compute JA3 fingerprint from parsed ClientHello.

    Returns ``(ja3_hash, ja3_full_string)``.

    JA3 string format: ``version,ciphers,extensions,elliptic_curves,ec_point_formats``
    where each sub-field is dash-separated and GREASE values are filtered out.
    """
    version = info.version

    ciphers = "-".join(str(c) for c in info.cipher_suites if not _is_grease(c))
    extensions_str = "-".join(str(e) for e in info.extensions if not _is_grease(e))
    groups = "-".join(str(g) for g in info.supported_groups if not _is_grease(g))
    formats = "-".join(str(f) for f in info.ec_point_formats)

    ja3_full = f"{version},{ciphers},{extensions_str},{groups},{formats}"
    ja3_hash = hashlib.md5(ja3_full.encode()).hexdigest()  # noqa: S324

    return ja3_hash, ja3_full


# ---------------------------------------------------------------------------
# JA4 fingerprint
# ---------------------------------------------------------------------------


def compute_ja4(info: ClientHelloInfo) -> str:
    """Compute JA4 TLS fingerprint from parsed ClientHello.

    Returns the JA4 string in ``a_b_c`` format.

    Reference: https://github.com/FoxIO-LLC/ja4/blob/main/technical_details/JA4.md
    """
    # --- Part a: protocol + version + SNI flag + counts + ALPN ---
    proto = "t"  # TCP (we don't handle QUIC)

    # Determine TLS version: prefer supported_versions extension for TLS 1.3
    if info.supported_versions:
        non_grease = [v for v in info.supported_versions if not _is_grease(v)]
        max_ver = max(non_grease) if non_grease else info.version
    else:
        max_ver = info.version

    ver_map = {0x0304: "13", 0x0303: "12", 0x0302: "11", 0x0301: "10"}
    ver_str = ver_map.get(max_ver, f"{(max_ver >> 8) & 0xFF}{max_ver & 0xFF}")

    sni_flag = "d" if info.sni else "i"

    cipher_count = min(len([c for c in info.cipher_suites if not _is_grease(c)]), 99)
    ext_count = min(len([e for e in info.extensions if not _is_grease(e)]), 99)

    alpn_str = "00"
    if info.alpn:
        first_alpn = info.alpn[0]
        # JA4 spec: first and last alphanumeric characters of the ALPN value
        alpn_str = first_alpn[0] + first_alpn[-1] if first_alpn else "00"

    part_a = f"{proto}{ver_str}{sni_flag}{cipher_count:02d}{ext_count:02d}{alpn_str}"

    # --- Part b: sorted cipher suites (GREASE filtered) ---
    sorted_ciphers = sorted(c for c in info.cipher_suites if not _is_grease(c))
    ciphers_str = ",".join(f"{c:04x}" for c in sorted_ciphers)
    part_b = hashlib.sha256(ciphers_str.encode()).hexdigest()[:12]

    # --- Part c: sorted extensions (GREASE, SNI, and ALPN filtered) + signature algorithms ---
    sorted_exts = sorted(
        e for e in info.extensions
        if not _is_grease(e) and e != _EXT_SERVER_NAME and e != _EXT_ALPN
    )
    exts_str = ",".join(f"{e:04x}" for e in sorted_exts)

    sig_algs_str = ",".join(
        f"{a:04x}" for a in info.signature_algorithms if not _is_grease(a)
    )

    combined = exts_str + "_" + sig_algs_str if sig_algs_str else exts_str
    part_c = hashlib.sha256(combined.encode()).hexdigest()[:12]

    return f"{part_a}_{part_b}_{part_c}"


# ---------------------------------------------------------------------------
# JA4_r fingerprint (raw / unhashed variant)
# ---------------------------------------------------------------------------


def compute_ja4_r(info: ClientHelloInfo) -> str:
    """Compute JA4_r (raw) TLS fingerprint from parsed ClientHello.

    JA4_r is identical to JA4 but outputs the raw sorted values instead of
    SHA-256 truncated hashes.  Format: ``a_b_c_d``

    - **a**: same prefix as JA4
    - **b**: sorted cipher suites as 4-char hex, comma-delimited (GREASE filtered)
    - **c**: sorted extensions as 4-char hex, comma-delimited (GREASE/SNI/ALPN filtered)
    - **d**: signature algorithms in original order, comma-delimited

    Reference: https://github.com/FoxIO-LLC/ja4/blob/main/technical_details/JA4.md
    """
    # --- Part a: reuse JA4's prefix logic ---
    proto = "t"
    if info.supported_versions:
        non_grease = [v for v in info.supported_versions if not _is_grease(v)]
        max_ver = max(non_grease) if non_grease else info.version
    else:
        max_ver = info.version
    ver_map = {0x0304: "13", 0x0303: "12", 0x0302: "11", 0x0301: "10"}
    ver_str = ver_map.get(max_ver, f"{(max_ver >> 8) & 0xFF}{max_ver & 0xFF}")
    sni_flag = "d" if info.sni else "i"
    cipher_count = min(len([c for c in info.cipher_suites if not _is_grease(c)]), 99)
    ext_count = min(len([e for e in info.extensions if not _is_grease(e)]), 99)
    alpn_str = "00"
    if info.alpn:
        first_alpn = info.alpn[0]
        alpn_str = first_alpn[0] + first_alpn[-1] if first_alpn else "00"
    part_a = f"{proto}{ver_str}{sni_flag}{cipher_count:02d}{ext_count:02d}{alpn_str}"

    # --- Part b: raw sorted cipher suites ---
    sorted_ciphers = sorted(c for c in info.cipher_suites if not _is_grease(c))
    part_b = ",".join(f"{c:04x}" for c in sorted_ciphers)

    # --- Part c: raw sorted extensions (GREASE/SNI/ALPN excluded) ---
    sorted_exts = sorted(
        e for e in info.extensions
        if not _is_grease(e) and e != _EXT_SERVER_NAME and e != _EXT_ALPN
    )
    part_c = ",".join(f"{e:04x}" for e in sorted_exts)

    # --- Part d: signature algorithms in original order ---
    part_d = ",".join(
        f"{a:04x}" for a in info.signature_algorithms if not _is_grease(a)
    )

    return f"{part_a}_{part_b}_{part_c}_{part_d}"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def client_hello_to_dict(
    info: ClientHelloInfo,
    ja3_hash: str,
    ja3_full: str,
    ja4: str,
    ja4_r: str,
) -> dict[str, Any]:
    """Serialize parsed ClientHello + fingerprints to a JSON-compatible dict."""
    return {
        "version": TLS_VERSION_NAMES.get(info.version, f"0x{info.version:04X}"),
        "supported_versions": [
            TLS_VERSION_NAMES.get(v, f"0x{v:04X}")
            for v in info.supported_versions
            if not _is_grease(v)
        ],
        "cipher_suites": [
            {
                "id": f"0x{cs:04X}",
                "name": info.cipher_suite_names.get(cs, f"Unknown (0x{cs:04X})"),
            }
            for cs in info.cipher_suites
            if not _is_grease(cs)
        ],
        "extensions": [
            {
                "id": ext,
                "name": EXTENSION_NAMES.get(ext, f"Unknown (0x{ext:04X})"),
            }
            for ext in info.extensions
            if not _is_grease(ext)
        ],
        "sni": info.sni,
        "alpn": info.alpn,
        "supported_groups": [
            {
                "id": g,
                "name": SUPPORTED_GROUP_NAMES.get(g, f"Unknown (0x{g:04X})"),
            }
            for g in info.supported_groups
            if not _is_grease(g)
        ],
        "signature_algorithms": [
            {
                "id": f"0x{a:04X}",
                "name": SIGNATURE_ALGORITHM_NAMES.get(a, f"Unknown (0x{a:04X})"),
            }
            for a in info.signature_algorithms
            if not _is_grease(a)
        ],
        "ec_point_formats": info.ec_point_formats,
        "ja3": ja3_hash,
        "ja3_full": ja3_full,
        "ja4": ja4,
        "ja4_r": ja4_r,
    }
