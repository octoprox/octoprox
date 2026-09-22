# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Public ``/echo`` endpoint served by every Octoprox instance (see api.geo.echo)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from api.core.config import settings
from api.geo.echo import EchoResponse, build_echo, client_ip, parse_trusted

router = APIRouter()


@router.get("/echo", response_model=EchoResponse, response_model_exclude_none=True)
async def echo(request: Request, nonce: str | None = None) -> EchoResponse:
    """Report the caller's IP and, with a database loaded, where it is.

    Requested through a proxy this returns the proxy's exit. Set
    ``geo.echo.trusted_proxies`` to the load balancer's addresses so the
    client IP is read from ``X-Forwarded-For`` instead of the balancer's.
    """
    if not settings.geo_echo_enabled:
        raise HTTPException(status_code=404, detail="Echo endpoint is disabled")
    peer = request.client.host if request.client else None
    ip = client_ip(peer, request.headers.get("x-forwarded-for"), parse_trusted(settings.geo_echo_trusted_proxies))
    if ip is None:
        raise HTTPException(status_code=400, detail="Could not determine the client address")
    return build_echo(ip, request.app.state.geo_runtime.database_store, nonce)
