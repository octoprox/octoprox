# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""MITM traffic inspection endpoints."""

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter(prefix="/projects/{project_id}/mitm")


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


class MitmRequestsResponse(BaseModel):
    """Paginated list of MITM request records."""

    records: list[MitmRequestRecord]
    next_cursor: str | None = None


@router.get("/requests", response_model=MitmRequestsResponse)
async def list_mitm_requests(
    request: Request,
    project_id: str,
    count: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, description="Stream entry ID for cursor pagination"),
) -> MitmRequestsResponse:
    """List recent MITM-intercepted requests for a project (newest first)."""
    proxy_manager = request.app.state.proxy_manager
    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    redis_client = request.app.state.redis_client

    # Fetch count+1 to determine if there's a next page
    raw_records = await redis_client.get_mitm_requests(
        project_id,
        count=count + 1,
        before_id=cursor,
    )

    has_more = len(raw_records) > count
    records_to_return = raw_records[:count]

    records = [
        MitmRequestRecord(
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
            tls_shared_ciphers=raw["tls_shared_ciphers"].split(",") if raw.get("tls_shared_ciphers") else [],
            tls_client_hello=json.loads(raw["tls_client_hello"]) if raw.get("tls_client_hello") else None,
        )
        for raw in records_to_return
    ]

    next_cursor = records_to_return[-1]["id"] if has_more and records_to_return else None

    return MitmRequestsResponse(records=records, next_cursor=next_cursor)


@router.delete("/requests")
async def clear_mitm_requests(
    request: Request,
    project_id: str,
) -> dict[str, str]:
    """Clear all MITM request records for a project."""
    proxy_manager = request.app.state.proxy_manager
    project = proxy_manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    redis_client = request.app.state.redis_client
    await redis_client.clear_mitm_requests(project_id)
    return {"status": "ok"}
