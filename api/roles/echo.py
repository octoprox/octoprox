# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The echo role's app: ``/echo`` without the rest of Octoprox.

An install that keeps Octoprox on a private network runs it on a public host
and points ``echo_url`` at it; it reads the settings the api's ``/echo`` reads
and needs no Postgres, no Redis and no secret.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request

from api import __version__
from api.geo.echo import EchoResponse, build_echo, client_ip, parse_trusted

if TYPE_CHECKING:
    from api.core.config import Settings
    from api.geo.store import GeoDatabaseStore
    from api.roles.node import Node

# What the app's lifespan is: the node's start and stop around serving.
Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_echo_app(settings: Settings, *, lifespan: Lifespan) -> FastAPI:
    """The app of a process of role ``echo``: ``/echo`` over the node's configured IP databases."""
    trusted = parse_trusted(settings.geo_echo_trusted_proxies)
    app = FastAPI(title="Octoprox Echo", version=__version__, lifespan=lifespan)

    @app.get("/echo", response_model=EchoResponse, response_model_exclude_none=True, tags=["Echo"])
    async def echo(request: Request, nonce: str | None = None) -> EchoResponse:
        """Report the caller's IP and, with a database loaded, where it is.

        Requested through a proxy this returns the proxy's exit; behind a load
        balancer, list its addresses in ``geo.echo.trusted_proxies`` so the client
        IP is read from ``X-Forwarded-For`` instead of the balancer's.
        """
        peer = request.client.host if request.client else None
        ip = client_ip(peer, request.headers.get("x-forwarded-for"), trusted)
        if ip is None:
            raise HTTPException(status_code=400, detail="Could not determine the client address")
        return build_echo(ip, _store_of(request), nonce)

    return app


def _store_of(request: Request) -> GeoDatabaseStore:
    """The echo node's database store, which its lifespan built and synced before serving."""
    node: Node = request.app.state.node
    store = node.echo_store
    if store is None:
        raise RuntimeError("the echo app runs on a node of role echo")
    return store
