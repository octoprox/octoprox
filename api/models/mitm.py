# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What the Inspector page shows: the requests the intercepting gateways captured, each as its stream entry holds it."""

import json
from typing import Any

from pydantic import BaseModel


class TlsClientHello(BaseModel):
    """Parsed TLS ClientHello handshake details."""

    version: str
    supported_versions: list[str]
    cipher_suites: list[dict[str, str]]
    extensions: list[dict[str, Any]]
    sni: str
    alpn: list[str]
    supported_groups: list[dict[str, Any]]
    signature_algorithms: list[dict[str, str]]
    ec_point_formats: list[int]
    compression_methods: list[dict[str, Any]] = []
    session_id_length: int = 0
    record_layer_version: str = ""
    key_share_groups: list[dict[str, Any]] = []
    compress_certificate: list[dict[str, Any]] = []
    alps_protocols: list[str] = []
    psk_key_exchange_modes: list[dict[str, Any]] = []
    ja3: str
    ja3_full: str
    ja4: str
    ja4_r: str


class MitmRequestRecord(BaseModel):
    """A single MITM-intercepted request record."""

    id: str
    timestamp: str
    method: str
    url: str
    request_headers: list[list[str]]
    upstream_headers: list[list[str]]
    request_body_size: int
    request_content_type: str
    status_code: int
    response_headers: list[list[str]]
    response_body_size: int
    response_content_type: str
    target_host: str
    proxy_url: str
    mitm_mode: str
    mitm_engine: str
    mitm_browser: str
    latency_ms: float
    tls_version: str
    tls_cipher: str
    tls_key_bits: int
    tls_shared_ciphers: list[str]
    tls_client_hello: TlsClientHello | None = None

    @classmethod
    def of(cls, raw: dict[str, str]) -> "MitmRequestRecord":
        """A capture from its stream entry's fields and ``id``: JSON lists and numbers parsed, what an older gateway
        left out empty."""
        return cls(
            id=raw["id"],
            timestamp=raw["timestamp"],
            method=raw["method"],
            url=raw["url"],
            request_headers=json.loads(raw["request_headers"]),
            upstream_headers=json.loads(raw.get("upstream_headers", "[]")),
            request_body_size=int(raw["request_body_size"]),
            request_content_type=raw.get("request_content_type", ""),
            status_code=int(raw["status_code"]),
            response_headers=json.loads(raw["response_headers"]),
            response_body_size=int(raw["response_body_size"]),
            response_content_type=raw.get("response_content_type", ""),
            target_host=raw["target_host"],
            proxy_url=raw["proxy_url"],
            mitm_mode=raw.get("mitm_mode", ""),
            mitm_engine=raw.get("mitm_engine", ""),
            mitm_browser=raw.get("mitm_browser", ""),
            latency_ms=float(raw["latency_ms"]),
            tls_version=raw.get("tls_version", ""),
            tls_cipher=raw.get("tls_cipher", ""),
            tls_key_bits=int(raw["tls_key_bits"]) if raw.get("tls_key_bits") else 0,
            tls_shared_ciphers=raw["tls_shared_ciphers"].split(",")
            if raw.get("tls_shared_ciphers")
            else [],
            tls_client_hello=json.loads(raw["tls_client_hello"])
            if raw.get("tls_client_hello")
            else None,
        )


class MitmRequestsResponse(BaseModel):
    """Paginated list of MITM request records."""

    records: list[MitmRequestRecord]
    next_cursor: str | None = None
