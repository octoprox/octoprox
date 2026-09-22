# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Standalone echo service: the ``/echo`` endpoint without the rest of Octoprox.

The echo request travels out through the vendor's proxy and back in from the
public internet, so the endpoint must be publicly reachable. Installs that keep
Octoprox on a private network run this process on a public host instead (the
same image, ``octoprox-echo``), and point ``echo_url`` in the attribution
policy at it. It needs no Postgres or Redis; databases are plain files.

Environment:

* ``OCTOPROX_ECHO_DATABASES``: comma-separated mmdb/BIN paths to attribute with (optional).
* ``OCTOPROX_ECHO_TRUSTED_PROXIES``: comma-separated CIDRs whose X-Forwarded-For is trusted.
* ``OCTOPROX_ECHO_HOST`` / ``OCTOPROX_ECHO_PORT``: bind address (default 0.0.0.0:8090).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request

from api import __version__
from api.core.logging import setup_logging
from api.geo.echo import EchoResponse, build_echo, client_ip, parse_trusted
from api.geo.store import GeoDatabaseStore

logger = structlog.get_logger()


def _split(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def create_echo_app(
    database_paths: list[str] | None = None,
    trusted_proxies: list[str] | None = None,
) -> FastAPI:
    """Build the standalone app. Arguments default to the environment."""
    paths = database_paths if database_paths is not None else _split(os.getenv("OCTOPROX_ECHO_DATABASES"))
    trusted = parse_trusted(
        trusted_proxies if trusted_proxies is not None else _split(os.getenv("OCTOPROX_ECHO_TRUSTED_PROXIES"))
    )
    store = GeoDatabaseStore(None, os.getenv("OCTOPROX_ECHO_CACHE_DIR", "data/geo"), [{"path": p} for p in paths])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        loaded = await store.sync_all()
        logger.info("Echo service started", version=__version__, databases=loaded, trusted_proxies=len(trusted))
        yield
        store.close_all()

    app = FastAPI(title="Octoprox Echo", version=__version__, lifespan=lifespan)
    app.state.geo_store = store

    @app.get("/echo", response_model=EchoResponse, response_model_exclude_none=True)
    async def echo(request: Request, nonce: str | None = None) -> EchoResponse:
        peer = request.client.host if request.client else None
        ip = client_ip(peer, request.headers.get("x-forwarded-for"), trusted)
        if ip is None:
            raise HTTPException(status_code=400, detail="Could not determine the client address")
        return build_echo(ip, store, nonce)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "healthy", "version": __version__, "databases": [d.name for d in store.loaded]}

    return app


app = create_echo_app()


def run() -> None:
    """Run the standalone echo service with uvicorn."""
    setup_logging(os.getenv("OCTOPROX_LOG_LEVEL", "INFO"))
    uvicorn.run(
        "api.echo_main:app",
        host=os.getenv("OCTOPROX_ECHO_HOST", "0.0.0.0"),
        port=int(os.getenv("OCTOPROX_ECHO_PORT", "8090")),
        log_config=None,
    )


if __name__ == "__main__":
    run()
