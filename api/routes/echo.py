# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The public ``/echo`` endpoint of api and all processes (see :mod:`api.geo.echo`).

It answers from the IP databases the process has open, touching neither
Postgres nor Redis per request, unless ``geo.echo.enabled`` turns it off; an
install whose api is private serves it from a process of role ``echo``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from api.geo.echo import EchoResponse, build_echo, client_ip, parse_trusted
from api.routes.common import geo_database_store_of, settings_of

router = APIRouter()


@router.get("/echo", response_model=EchoResponse, response_model_exclude_none=True)
async def echo(request: Request, nonce: str | None = None) -> EchoResponse:
    """Report the caller's IP and, with a database loaded, where it is.

    Requested through a proxy this returns the proxy's exit. Set
    ``geo.echo.trusted_proxies`` to the load balancer's addresses so the
    client IP is read from ``X-Forwarded-For`` instead of the balancer's.
    """
    settings = settings_of(request)
    if not settings.geo_echo_enabled:
        raise HTTPException(status_code=404, detail="Echo endpoint is disabled")
    peer = request.client.host if request.client else None
    ip = client_ip(peer, request.headers.get("x-forwarded-for"), parse_trusted(settings.geo_echo_trusted_proxies))
    if ip is None:
        raise HTTPException(status_code=400, detail="Could not determine the client address")
    return build_echo(ip, geo_database_store_of(request), nonce)
