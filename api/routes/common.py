# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What every route module reaches the process through: typed accessors over ``app.state``, and the error mapping.

The domain errors answer with their status, and a store that does not answer with 503 and ``Retry-After``.
"""

from __future__ import annotations

import socket
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

import asyncpg.exceptions  # type: ignore[import-untyped]
import redis.exceptions
import sqlalchemy.exc
import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from api.core.logging import error_label, error_text
from api.db.repository import ProjectRepository
from api.models.tunnel import TunnelAddressError, TunnelValidationError
from api.services.errors import InvalidConfigError, NotFoundError, WriteRejectedError

if TYPE_CHECKING:
    from api.core.config import Settings
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog
    from api.geo.observations import ObservationRecorder
    from api.geo.service import GeoService
    from api.geo.settings import GeoSettingsStore
    from api.geo.store import GeoDatabaseStore
    from api.geo.updater import GeoDatabaseUpdater
    from api.jobs.attribution import Attribution
    from api.jobs.queue import JobQueue
    from api.models.project import Project
    from api.openvpn.service import OpenVpnService
    from api.providers.registry import ProviderRegistry
    from api.providers.store import ProviderStore
    from api.roles.api import ApiRuntime
    from api.roles.node import Node
    from api.services.connectors import ConnectorService
    from api.services.credentials import CredentialService
    from api.services.projects import ProjectService
    from api.services.proxies import ProxyService
    from api.services.read_model import RuntimeView
    from api.sso import SsoFlow
    from api.wireguard.service import WireGuardService

logger = structlog.get_logger()

# What each domain error answers with. Lookup follows the error's classes, so
# NameConflictError, a WriteRejectedError, answers 400 too.
ERROR_STATUS: dict[type[Exception], int] = {
    # The entity a write or read names does not exist, or not in the project named (404).
    NotFoundError: 404,
    # The write breaks a rule, or a name is taken (400).
    WriteRejectedError: 400,
    # A config failed its validator (422).
    InvalidConfigError: 422,
    # A tunnel device or settings edit that cannot stand, such as a name in use (400).
    TunnelValidationError: 400,
    # A tunnel address taken concurrently or a full subnet: retry or widen (409).
    TunnelAddressError: 409,
}

# The errors that mean a store did not answer, not that the request or the
# code is wrong, by the store the answer names. While the pool connects the
# socket's own errors come as they are; a server that refuses the connection
# comes wrapped in a DBAPIError (POSTGRES_REFUSALS), as does a connection lost
# during a request (see _answer_lost_connection). redis-py wraps its own
# socket errors, so the builtin ones are Postgres's.
UNAVAILABLE_ERRORS: dict[type[Exception], str] = {
    ConnectionError: "Postgres",
    TimeoutError: "Postgres",
    # The database host's name did not resolve: a DNS outage, or Postgres's address moving.
    socket.gaierror: "Postgres",
    # No pooled connection came free within the pool's timeout.
    sqlalchemy.exc.TimeoutError: "Postgres",
    redis.exceptions.ConnectionError: "Redis",
    redis.exceptions.TimeoutError: "Redis",
}
# What asyncpg raises when the server refuses a connection: it is starting up
# or shutting down, has no connection slot left, or answers the login with a
# connection error (SQLSTATE class 08, as a pooler does). SQLAlchemy wraps it
# in a DBAPIError without a statement, under the dialect's emulated error (see
# _driver_error). A statement on a live connection can meet class 08 too, as a
# protocol violation: a bug, not a refusal, so only an error without a
# statement counts.
POSTGRES_REFUSALS = (
    asyncpg.exceptions.PostgresConnectionError,
    asyncpg.exceptions.CannotConnectNowError,
    asyncpg.exceptions.TooManyConnectionsError,
)
# Seconds a client is told to wait before it tries again after a 503: about
# what a restarting Redis or a pool reconnecting to a new primary takes.
UNAVAILABLE_RETRY_AFTER_SECONDS = 5


def install_error_handlers(app: FastAPI) -> None:
    """Answer the domain errors with their status, and a store that does not answer with 503.

    Each error of :data:`ERROR_STATUS` gets its status and its message as
    ``detail``; each of :data:`UNAVAILABLE_ERRORS`, and a Postgres
    connection refused or lost mid-request, a 503 (:func:`_unavailable`).
    """
    for error, status in ERROR_STATUS.items():
        app.add_exception_handler(error, _answer_with(status))
    for error, store in UNAVAILABLE_ERRORS.items():
        app.add_exception_handler(error, _answer_unavailable(store))
    app.add_exception_handler(sqlalchemy.exc.DBAPIError, _answer_lost_connection)


def _answer_with(status: int) -> Callable[[Request, Exception], Awaitable[Response]]:
    """A handler answering ``status`` with the error's message, shaped like an ``HTTPException``'s body."""

    async def answer(request: Request, exc: Exception) -> Response:
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    return answer


def _answer_unavailable(store: str) -> Callable[[Request, Exception], Awaitable[Response]]:
    """A handler answering 503 for an error of ``store``; see :func:`_unavailable`."""

    async def answer(request: Request, exc: Exception) -> Response:
        return _unavailable(request, store, exc)

    return answer


async def _answer_lost_connection(request: Request, exc: Exception) -> Response:
    """503 for a Postgres connection that was refused or broke during the request; any other database error stays a 500.

    A refusal comes while the pool connects, where SQLAlchemy's error carries no statement (:data:`POSTGRES_REFUSALS`).
    """
    if isinstance(exc, sqlalchemy.exc.DBAPIError) and (
        exc.connection_invalidated
        or (exc.statement is None and isinstance(_driver_error(exc), POSTGRES_REFUSALS))
    ):
        return _unavailable(request, "Postgres", exc)
    raise exc


def _driver_error(exc: sqlalchemy.exc.DBAPIError) -> Exception | None:
    """The driver's own error under ``exc``: asyncpg's, under the dialect's emulated one; None when there is none.

    Not ``exc.driver_exception``, which raises when there is none, as for a cached statement gone stale.
    """
    orig = exc.orig
    return orig.orig if isinstance(orig, sqlalchemy.exc.EmulatedDBAPIException) else orig


def _unavailable(request: Request, store: str, exc: Exception) -> Response:
    """503 with ``Retry-After``, naming the store, never the error; logged once, without a traceback."""
    if isinstance(exc, sqlalchemy.exc.DBAPIError) and exc.orig is not None:
        # The driver's message: the statement and its parameters may hold secrets.
        driver = _driver_error(exc) or exc.orig
        error = error_label(driver)
    else:
        error = error_text(exc)
    logger.warning(
        "Request refused: a store cannot be reached",
        store=store,
        method=request.method,
        path=request.url.path,
        error=error,
    )
    return JSONResponse(
        status_code=503,
        content={"detail": f"{store} cannot be reached; try again shortly"},
        headers={"Retry-After": str(UNAVAILABLE_RETRY_AFTER_SECONDS)},
    )


# --- The process -----------------------------------------------------------------------------


def settings_of(request: Request) -> Settings:
    """The process's settings, as ``create_app`` was given them."""
    return cast("Settings", request.app.state.settings)


def node_of(request: Request) -> Node:
    """The process's node, which the lifespan built and started."""
    return cast("Node", request.app.state.node)


def api_runtime_of(request: Request) -> ApiRuntime:
    """The process's api runtime; only api and all apps mount routes that ask for it."""
    runtime = node_of(request).api
    if runtime is None:
        raise RuntimeError("this process runs no api runtime")
    return runtime


def session_factory_of(request: Request) -> SessionFactory:
    """The process's Postgres sessions, on the engine ``get_db`` uses too."""
    return api_runtime_of(request).session_factory


def redis_client_of(request: Request) -> RedisClient:
    """The process's Redis client."""
    return api_runtime_of(request).redis_client


def change_log_of(request: Request) -> ChangeLog:
    """The process's change log, for routes that write definitions themselves: each announces its commit there.

    The descriptor, attribution settings, IP database and import routes append their entry after the commit, as the
    services do for theirs.
    """
    return api_runtime_of(request).changes


def jobs_of(request: Request) -> JobQueue:
    """The job streams, for routes that ask for a run (a re-attribution)."""
    return api_runtime_of(request).jobs


def provider_registry_of(request: Request) -> ProviderRegistry:
    """The process's provider registry, with the custom descriptors as stored."""
    return api_runtime_of(request).provider_registry


def provider_store_of(request: Request) -> ProviderStore:
    """What applies the stored custom descriptors to the registry (``sync_all`` after a descriptor write)."""
    return api_runtime_of(request).provider_store


# --- Write services, read model, previews -----------------------------------------------------


def project_service_of(request: Request) -> ProjectService:
    """Project writes: create, edit, delete."""
    return api_runtime_of(request).project_service


def credential_service_of(request: Request) -> CredentialService:
    """Credential writes, validated by the provider registry."""
    return api_runtime_of(request).credential_service


def connector_service_of(request: Request) -> ConnectorService:
    """Connector writes, traffic resets and the jobs they request."""
    return api_runtime_of(request).connector_service


def proxy_service_of(request: Request) -> ProxyService:
    """Proxy writes: add, upload, edit, remove, unquarantine."""
    return api_runtime_of(request).proxy_service


def runtime_view_of(request: Request) -> RuntimeView:
    """The read model: definitions joined with the soft state other roles keep in Redis."""
    return api_runtime_of(request).runtime_view


async def require_project(request: Request, project_id: str) -> Project:
    """The project a nested route names, read from Postgres; 404 when there is none."""
    async with session_factory_of(request)() as session:
        project = await ProjectRepository(session).get_by_id(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


# --- Attribution ------------------------------------------------------------------------------


def geo_service_of(request: Request) -> GeoService:
    """Attribution: lookups, judgements, the settings in force."""
    return api_runtime_of(request).geo_service


def geo_settings_store_of(request: Request) -> GeoSettingsStore:
    """The attribution settings (``save`` writes the row and adopts it)."""
    return api_runtime_of(request).geo_settings_store


def geo_database_store_of(request: Request) -> GeoDatabaseStore:
    """The IP databases this process has open, and their files (``blob_file``)."""
    return api_runtime_of(request).geo_database_store


def observation_recorder_of(request: Request) -> ObservationRecorder:
    """This process's observation buffer (published by the node's observation publisher)."""
    return api_runtime_of(request).observation_recorder


def attribution_of(request: Request) -> Attribution:
    """The attributor with a Postgres-backed store per call (``store()``), for Detect."""
    return api_runtime_of(request).attribution


def geo_updater_of(request: Request) -> GeoDatabaseUpdater:
    """The IP database updater without its loop, for manual refresh and from-url."""
    return api_runtime_of(request).geo_updater


# --- Tunnels and sign-in ----------------------------------------------------------------------


def wireguard_service_of(request: Request) -> WireGuardService:
    """WireGuard settings and devices over Postgres, with each gateway's state from the heartbeats."""
    return api_runtime_of(request).wireguard_service


def openvpn_service_of(request: Request) -> OpenVpnService:
    """OpenVPN settings, CA and devices over Postgres, with each gateway's state from the heartbeats."""
    return api_runtime_of(request).openvpn_service


def sso_flow_of(request: Request) -> SsoFlow:
    """The sign-in round trip through identity providers; its per-login state is in Redis."""
    return api_runtime_of(request).sso_flow


def external_base_url(request: Request) -> str:
    """The URL this install is reached at, for links handed to browsers; never ending in a slash.

    ``OCTOPROX_PUBLIC_URL`` when set, since behind a load balancer the request's own base URL names the balancer's
    internal face; otherwise the request's.
    """
    public_url = settings_of(request).public_url
    if public_url:
        return public_url.rstrip("/")
    return str(request.base_url).rstrip("/")

