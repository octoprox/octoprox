# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The entry point of every Octoprox process: the HTTP app of its role, on port 8000.

``OCTOPROX_ROLE`` decides what :func:`create_app` builds (the admin API, the
web UI and the internal endpoints for api and all, ``/echo`` for those and
echo, the ops endpoints for every role), and importing this module builds the
app object only: its lifespan starts the role's runtimes.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api import __version__
from api.core.config import Settings, get_settings
from api.core.logging import setup_logging
from api.core.request_context import REQUEST_ID_HEADER, RequestContextMiddleware
from api.core.roles import Role
from api.roles import Node
from api.roles.echo import Lifespan, create_echo_app
from api.routes import health

# The built web UI the api serves, when the image (or a local build) has it.
WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
# The roles whose app serves the admin API, the web UI and the internal endpoints.
API_ROLES = frozenset({Role.API, Role.ALL})
# Names the running release on every api response, so an open web UI notices
# a new one and offers to load it (web/src/utils/appVersion.ts).
VERSION_HEADER = "X-Octoprox-Version"


class _VersionHeader:
    """Adds the running release to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.header = (VERSION_HEADER.lower().encode("latin-1"), __version__.encode("latin-1"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_version(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), self.header]
            await send(message)

        await self.app(scope, receive, send_with_version)


class _HashedAssets(StaticFiles):
    """The build's assets: a file's name changes with its content, so a browser may keep it for good."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def create_app(settings: Settings, *, web_dist: Path = WEB_DIST) -> FastAPI:
    """The HTTP app of a process of ``settings.role``; its lifespan runs the process's node.

    An api or all process serves the built web UI from ``web_dist`` when it
    exists, and logging is the caller's to set up, once per process (see the
    bottom of this module).
    """
    lifespan = _lifespan(settings)
    if settings.role == Role.ECHO:
        app = create_echo_app(settings, lifespan=lifespan)
    else:
        app = FastAPI(
            title="Octoprox",
            description="A dynamic and flexible proxy manager",
            version=__version__,
            lifespan=lifespan,
        )
    app.state.settings = settings
    if settings.role in API_ROLES:
        # Configure CORS. X-Request-ID is exposed so a cross-origin web UI (the
        # Vite dev server) can read the ID off error responses.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[REQUEST_ID_HEADER, VERSION_HEADER],
        )
    # Added after CORS so it wraps it: every response, preflight included,
    # carries a request ID and every log line during the request has it bound.
    app.add_middleware(RequestContextMiddleware)
    if settings.role in API_ROLES:
        # Outermost, so the 500 RequestContextMiddleware answers for an
        # unhandled error names the release too.
        app.add_middleware(_VersionHeader)
    # The ops endpoints come first, so the web UI's catch-all never shadows them.
    app.include_router(health.router, tags=["Health"])
    if settings.role in API_ROLES:
        _include_api(app)
        _serve_web_ui(app, web_dist)
    return app


def _lifespan(settings: Settings) -> Lifespan:
    """Build the process's node, start it before serving, and stop it after."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        node = Node(settings)
        app.state.node = node
        # A start that fails stops what it started before raising.
        await node.start()
        try:
            yield
        finally:
            await node.stop()

    return lifespan


def _include_api(app: FastAPI) -> None:
    """The admin API, the public echo and the internal endpoints of an api or all process.

    Imported here, so the other roles never load the route modules they do not serve.
    """
    from api.core.auth import require_auth
    from api.routes import (
        auth,
        backup,
        connectors,
        credentials,
        echo,
        failures,
        geo,
        internal,
        metrics,
        mitm,
        openvpn,
        projects,
        providers,
        proxies,
        releases,
        sso,
        system,
        users,
        wireguard,
    )
    from api.routes.common import install_error_handlers

    install_error_handlers(app)
    # Echo endpoint (public by nature: the caller is a proxy exit)
    app.include_router(echo.router, tags=["Echo"])
    # What gateways fetch from the api, behind the internal token.
    app.include_router(internal.router, tags=["Internal"], include_in_schema=False)

    # Protected routes - require auth when enabled
    auth_dependency = [Depends(require_auth)]

    # All API routes under /api/v1
    app.include_router(auth.router, prefix="/api/v1", tags=["Auth"])
    # The sign-in round trip through a provider: public, the browser has no token yet.
    app.include_router(sso.public_router, prefix="/api/v1", tags=["Auth"])
    app.include_router(
        sso.admin_router, prefix="/api/v1", tags=["Sign-in providers"], dependencies=auth_dependency
    )
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
        failures.router, prefix="/api/v1", tags=["Failures"], dependencies=auth_dependency
    )
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
        releases.router, prefix="/api/v1", tags=["Releases"], dependencies=auth_dependency
    )
    app.include_router(
        geo.router, prefix="/api/v1", tags=["IP attribution"], dependencies=auth_dependency
    )
    app.include_router(
        wireguard.server_router, prefix="/api/v1", tags=["WireGuard"], dependencies=auth_dependency
    )
    app.include_router(
        wireguard.router, prefix="/api/v1", tags=["WireGuard"], dependencies=auth_dependency
    )
    app.include_router(
        openvpn.server_router, prefix="/api/v1", tags=["OpenVPN"], dependencies=auth_dependency
    )
    app.include_router(
        openvpn.router, prefix="/api/v1", tags=["OpenVPN"], dependencies=auth_dependency
    )


def _serve_web_ui(app: FastAPI, web_dist: Path) -> None:
    """The built web UI: its assets, its other files, and ``index.html`` for every route the UI owns.

    Registered last, as a catch-all, it serves a path only when the path
    resolves to a file inside ``web_dist``, so neither an encoded ``..`` nor a
    symlink out of the build reaches another file, such as the MITM CA key.
    """
    if not web_dist.is_dir():
        return
    root = web_dist.resolve()
    index = root / "index.html"
    assets_dir = root / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", _HashedAssets(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str) -> FileResponse:
        file_path = (root / full_path).resolve()
        if not file_path.is_relative_to(root):
            raise HTTPException(status_code=404, detail="Not Found")
        if file_path.is_file() and file_path != index:
            return FileResponse(file_path)
        # Anything else is a route of the single-page app. The page names the
        # build's assets, so a browser checks it with the server on every load
        # and a new tab opens a new release at once.
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


# The uvicorn target. Logging first, once, with the role on every line: before
# anything logs, and outside create_app, which tests call once per app.
_settings = get_settings()
setup_logging(_settings.log_level, _settings.log_format, _settings.instance_id, _settings.role)
app = create_app(_settings)


def run() -> None:
    """Run the application using uvicorn."""
    settings = get_settings()
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
