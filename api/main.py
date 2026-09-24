# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Main entry point for Octoprox API server."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
import uvicorn
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api import __version__
from api.core import utc_now
from api.core.auth import require_auth
from api.core.config import settings
from api.core.logging import setup_logging
from api.core.mitm import MitmHandler
from api.core.proxy_manager import ProxyManager
from api.core.proxy_server import ProxyServer
from api.core.request_context import REQUEST_ID_HEADER, RequestContextMiddleware
from api.core.seed import seed_admin_user
from api.core.signals import geo_database_changed, geo_settings_changed
from api.core.system_stats import build_instance_snapshot
from api.core.tls_cert_manager import TLSCertManager
from api.db.migrations import run_migrations
from api.db.redis import get_redis_client
from api.db.session import get_async_session_factory
from api.geo.runtime import GeoRuntime
from api.geo.verifier import ExitVerifier
from api.routes import (
    auth,
    backup,
    connectors,
    credentials,
    echo,
    geo,
    health,
    metrics,
    mitm,
    projects,
    providers,
    proxies,
    system,
    users,
)

# Configure logging before getting the logger
setup_logging(settings.log_level, settings.log_format, settings.instance_id)
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    logger.info("Starting Octoprox", version=__version__)
    app.state.started_at = utc_now()

    # Run database migrations (uses sync URL)
    logger.info("Running database migrations")
    run_migrations(settings.database_url_sync)

    # Create session factory (uses async URL)
    session_factory = get_async_session_factory(
        settings.database_url,
        settings.db_application_name,
        settings.debug,
    )

    # Seed initial admin user if no users exist
    await seed_admin_user(session_factory, settings)

    # Create and connect Redis client
    logger.info("Connecting to Redis")
    redis_client = get_redis_client(settings.redis_url)
    await redis_client.connect()

    # Store redis client for cleanup
    app.state.redis_client = redis_client

    # IP attribution: databases, settings, observation pipeline, preflight.
    geo_runtime = GeoRuntime(settings, session_factory, redis_client)
    app.state.geo_runtime = geo_runtime
    await geo_runtime.start()

    # Initialize proxy manager with dependencies. Attribution joins the
    # cross-instance change feed and the periodic reload here, explicitly,
    # rather than the manager knowing what it is wiring.
    proxy_manager = ProxyManager(
        session_factory=session_factory,
        redis_client=redis_client,
        settings=settings,
        health_check_extraction_rules=geo_runtime.extraction_rules,
        cross_instance_handlers={
            geo_database_changed: geo_runtime.reload_database,
            geo_settings_changed: geo_runtime.reload_settings,
        },
        reload_hooks=[geo_runtime.resync],
    )
    app.state.proxy_manager = proxy_manager

    # The attributor writes attribution onto proxies as sightings arrive; the
    # manager is its proxy store.
    await geo_runtime.proxy_attributor.start(proxy_manager)

    # Let the heartbeat publish what this instance sees, for the admin system
    # view's per-instance cards. Wired here rather than inside ProxyManager
    # because the snapshot spans components the manager does not own - the
    # proxy server, the geo lookup, the CA context cache - and read off
    # app.state rather than captured, because those components are built after
    # the heartbeat has already started beating. Each is optional, so the first
    # beat carries a partial snapshot rather than none at all.
    proxy_manager.snapshot_provider = lambda: build_instance_snapshot(
        settings,
        app.state.started_at,
        proxy_manager=proxy_manager,
        proxy_server=getattr(app.state, "proxy_server", None),
        cert_manager=getattr(app.state, "cert_manager", None),
        geo_runtime=geo_runtime,
    )

    # Start background tasks (loads from DB, hydrates from Redis)
    await proxy_manager.start()

    # Initialize TLS MITM certificate manager
    cert_manager = TLSCertManager(
        ca_cert_path=Path(settings.tls_mitm_ca_cert_path),
        ca_key_path=Path(settings.tls_mitm_ca_key_path),
    )
    await cert_manager.bootstrap(redis_client, settings.instance_id)
    mitm_handler = MitmHandler(cert_manager, redis_client=redis_client)
    app.state.cert_manager = cert_manager

    # Start the HTTP proxy server
    # Preflight: the manager is the verifier's proxy selector.
    exit_verifier = ExitVerifier(geo_runtime.preflight_checker, proxy_manager)
    proxy_server = ProxyServer(proxy_manager, mitm_handler=mitm_handler, exit_verifier=exit_verifier)
    await proxy_server.start()
    app.state.proxy_server = proxy_server

    yield

    # Cleanup
    logger.info("Shutting down Octoprox")
    await proxy_server.stop()
    await proxy_manager.stop()
    await geo_runtime.stop()
    await redis_client.close()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Octoprox",
        description="A dynamic and flexible proxy manager",
        version=__version__,
        lifespan=lifespan,
    )

    # Configure CORS. X-Request-ID is exposed so a cross-origin web UI (the
    # Vite dev server) can read the ID off error responses.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
    )
    # Added after CORS so it wraps it: every response, preflight included,
    # carries a request ID and every log line during the request has it bound.
    app.add_middleware(RequestContextMiddleware)

    # Include routers
    # Health check (public)
    app.include_router(health.router, tags=["Health"])
    # Echo endpoint (public by nature: the caller is a proxy exit)
    app.include_router(echo.router, tags=["Echo"])

    # Protected routes - require auth when enabled
    auth_dependency = [Depends(require_auth)]

    # All API routes under /api/v1
    app.include_router(auth.router, prefix="/api/v1", tags=["Auth"])
    app.include_router(
        projects.router, prefix="/api/v1", tags=["Projects"], dependencies=auth_dependency
    )
    app.include_router(
        credentials.router, prefix="/api/v1", tags=["Credentials"], dependencies=auth_dependency
    )
    app.include_router(
        connectors.router, prefix="/api/v1", tags=["Connectors"], dependencies=auth_dependency
    )
    app.include_router(
        connectors.options_router,
        prefix="/api/v1",
        tags=["Connectors"],
        dependencies=auth_dependency,
    )
    app.include_router(
        proxies.router, prefix="/api/v1", tags=["Proxies"], dependencies=auth_dependency
    )
    app.include_router(
        metrics.router, prefix="/api/v1", tags=["Metrics"], dependencies=auth_dependency
    )
    app.include_router(
        providers.router, prefix="/api/v1", tags=["Providers"], dependencies=auth_dependency
    )
    app.include_router(mitm.router, prefix="/api/v1", tags=["MITM"], dependencies=auth_dependency)
    app.include_router(
        users.router, prefix="/api/v1", tags=["Users"], dependencies=auth_dependency
    )
    app.include_router(
        backup.router, prefix="/api/v1", tags=["Backup"], dependencies=auth_dependency
    )
    app.include_router(
        system.router, prefix="/api/v1", tags=["System"], dependencies=auth_dependency
    )
    app.include_router(
        geo.router, prefix="/api/v1", tags=["IP attribution"], dependencies=auth_dependency
    )

    # Serve frontend static files in production (when web/dist exists)
    static_dir = Path(__file__).parent.parent / "web" / "dist"
    if static_dir.exists():
        # Serve static assets (JS, CSS, images)
        assets_dir = static_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        # SPA fallback - serve index.html for all non-API routes
        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str) -> FileResponse:
            # If it's a file that exists in dist, serve it (e.g., favicon, robots.txt)
            file_path = static_dir / full_path
            if file_path.is_file():
                return FileResponse(file_path)
            # Otherwise serve index.html for SPA routing
            return FileResponse(static_dir / "index.html")

    return app


app = create_app()


def run() -> None:
    """Run the application using uvicorn."""
    uvicorn.run(
        "api.main:app",
        host=settings.host,
        port=settings.api_port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
        log_config=None,
    )


if __name__ == "__main__":
    run()
