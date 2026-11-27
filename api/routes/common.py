# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What every route module reaches the process through: typed accessors over ``app.state``, and the error mapping.

The domain errors answer with their status, and a store that does not answer with 503 and ``Retry-After``.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

import asyncpg.exceptions  # type: ignore[import-untyped]
import redis.exceptions
import sqlalchemy.exc
import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from api.core.logging import error_label, error_text
from api.db.repository import ProjectRepository
from api.models.tunnel import TunnelAddressError, TunnelValidationError
from api.services.errors import (
    ConflictError,
    ForbiddenError,
    HostConfirmationError,
    InputRejectedError,
    InvalidConfigError,
    NotFoundError,
    TimedOutError,
    UnavailableError,
    UpstreamError,
    WriteRejectedError,
)

if TYPE_CHECKING:
    from api.attention.history import AlertHistory
    from api.attention.service import AttentionService, InstallAttentionService
    from api.attention.settings import AlertSettingsService
    from api.core.audit import Actor, AuditLog
    from api.core.auth import CurrentUser
    from api.core.config import Settings
    from api.core.extension import Extension
    from api.db.session import SessionFactory
    from api.geo.databases import GeoDatabaseService
    from api.geo.history import ObservationHistory
    from api.geo.reattribution import ReattributionService
    from api.geo.settings import GeoSettingsService
    from api.geo.status import GeoStatusService
    from api.geo.store import GeoDatabaseStore
    from api.models.project import Project
    from api.openvpn.service import OpenVpnService
    from api.reputation.ip_service import ExitIpService
    from api.reputation.reports import ExitReportService
    from api.reputation.site_service import SiteReputationService
    from api.roles.api import ApiRuntime
    from api.roles.node import Node
    from api.services.api_tokens import ApiTokenService
    from api.services.audit import AuditService
    from api.services.backup import BackupService
    from api.services.connect import ConnectService
    from api.services.connectors import ConnectorService
    from api.services.countries import CountryService
    from api.services.credentials import CredentialService
    from api.services.detect import DetectService
    from api.services.failures import FailureLogService
    from api.services.hosts import HostMetricsService
    from api.services.metrics import MetricsService
    from api.services.mitm import MitmService
    from api.services.projects import ProjectService
    from api.services.provider_calls import ProviderCallService
    from api.services.providers import ProviderService
    from api.services.proxies import ProxyService
    from api.services.read_model import RuntimeView
    from api.services.releases import ReleaseService
    from api.services.system import SystemService
    from api.services.system_settings import SystemSettingsService
    from api.services.traffic_split import TrafficSplitService
    from api.services.users import UserService
    from api.sso import SsoFlow
    from api.sso.providers import SsoProviderService
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
    # A read's argument names nothing it can read (400).
    InputRejectedError: 400,
    # The caller may not do this with these arguments (403).
    ForbiddenError: 403,
    # The write cannot apply to the entity as it stands (409); HostConfirmationError has its own handler.
    ConflictError: 409,
    # A vendor or a download the request relies on refused or failed (502).
    UpstreamError: 502,
    # A read took longer than it may (504).
    TimedOutError: 504,
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
    ``detail``, an :class:`UnavailableError` a 503 with ``Retry-After``, and a
    :class:`HostConfirmationError` its hosts too; each of
    :data:`UNAVAILABLE_ERRORS`, and a Postgres connection refused or lost
    mid-request, a 503 (:func:`_unavailable`).
    """
    for error, status in ERROR_STATUS.items():
        app.add_exception_handler(error, _answer_with(status))
    app.add_exception_handler(UnavailableError, _answer_retry_later)
    app.add_exception_handler(HostConfirmationError, _answer_host_confirmation)
    for error, store in UNAVAILABLE_ERRORS.items():
        app.add_exception_handler(error, _answer_unavailable(store))
    app.add_exception_handler(sqlalchemy.exc.DBAPIError, _answer_lost_connection)
    app.add_exception_handler(RequestValidationError, _answer_invalid)


class _AsciiJSONResponse(JSONResponse):
    """JSON escaped to ASCII: a lone surrogate a client sent, which no UTF-8 can carry, comes back as its escape."""

    def render(self, content: object) -> bytes:
        return json.dumps(content, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")


async def _answer_invalid(request: Request, exc: Exception) -> Response:
    """FastAPI's 422 for a request that breaks its model, its errors echoing what was sent, written so whatever was sent
    can be: FastAPI's own answer fails with a 500 on a lone surrogate."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    return _AsciiJSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


def _answer_with(status: int) -> Callable[[Request, Exception], Awaitable[Response]]:
    """A handler answering ``status`` with the error's message, shaped like an ``HTTPException``'s body."""

    async def answer(request: Request, exc: Exception) -> Response:
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    return answer


async def _answer_retry_later(request: Request, exc: Exception) -> Response:
    """503 with the error's message and ``Retry-After``; logs nothing, as the service that raised it knows what failed."""
    return JSONResponse(
        status_code=503,
        content={"detail": str(exc)},
        headers={"Retry-After": str(UNAVAILABLE_RETRY_AFTER_SECONDS)},
    )


async def _answer_host_confirmation(request: Request, exc: Exception) -> Response:
    """409 with the message and the provider's egress hosts, every one and those still to confirm."""
    hosts = cast("HostConfirmationError", exc)
    detail = {"detail": str(hosts), "egress_hosts": hosts.egress_hosts, "unconfirmed_hosts": hosts.unconfirmed_hosts}
    return JSONResponse(status_code=409, content={"detail": detail})


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


# --- Edition and audit log --------------------------------------------------------------------


def extension_of(request: Request) -> Extension:
    """The edition this api process loaded at start (``Extension.installed``)."""
    return cast("Extension", request.app.state.extension)


def audit_log_of(request: Request) -> AuditLog:
    """Where this api process records audit entries; its extension built it."""
    return cast("AuditLog", request.app.state.audit_log)


def actor_of(request: Request, user: CurrentUser) -> Actor:
    """The signed-in user as the actor of this request's changes, with the request's id.

    A caller with a personal access token acts through it, and each of its entries names it (``auth_token`` and
    ``auth_token_id``).
    """
    details = {"auth_token": user.token_name, "auth_token_id": user.token_id} if user.auth_method == "token" else {}
    request_id = getattr(request.state, "request_id", None)
    return audit_log_of(request).actor(user.id, user.username, request_id, details=details)


# --- Services, read model, previews -----------------------------------------------------------


def project_service_of(request: Request) -> ProjectService:
    """Project writes: create, edit, delete."""
    return api_runtime_of(request).project_service


def credential_service_of(request: Request) -> CredentialService:
    """A project's credentials: reads, and writes validated by the provider registry."""
    return api_runtime_of(request).credential_service


def connector_service_of(request: Request) -> ConnectorService:
    """Connector writes, traffic resets and the jobs they request."""
    return api_runtime_of(request).connector_service


def provider_service_of(request: Request) -> ProviderService:
    """The provider catalog, the authoring dry run, and custom descriptor writes."""
    return api_runtime_of(request).provider_service


def provider_call_service_of(request: Request) -> ProviderCallService:
    """Live vendor calls through a descriptor: dynamic select options and the builder's test panel."""
    return api_runtime_of(request).provider_call_service


def proxy_service_of(request: Request) -> ProxyService:
    """Proxy writes: add, upload, edit, remove, unquarantine."""
    return api_runtime_of(request).proxy_service


def detect_service_of(request: Request) -> DetectService:
    """Detect: a proxy's exit looked up through it from this process, and recorded."""
    return api_runtime_of(request).detect_service


def metrics_service_of(request: Request) -> MetricsService:
    """A project's pool, counters, scaling and history, its connectors' history, and their Prometheus export."""
    return api_runtime_of(request).metrics_service


def traffic_split_service_of(request: Request) -> TrafficSplitService:
    """How a project's traffic divides between its connectors, and under exit reputation for a site."""
    return api_runtime_of(request).traffic_split_service


def host_metrics_service_of(request: Request) -> HostMetricsService:
    """Where a project's traffic went, by destination host and connector (the Hosts page)."""
    return api_runtime_of(request).host_metrics_service


def failure_log_service_of(request: Request) -> FailureLogService:
    """A project's failure examples and counted failures (the Failures page)."""
    return api_runtime_of(request).failure_log_service


def connect_service_of(request: Request) -> ConnectService:
    """The Connect page: the proxy endpoint, the username for a place and session, and where it would route."""
    return api_runtime_of(request).connect_service


def country_service_of(request: Request) -> CountryService:
    """The Countries page: a project's connectors by country, and where a request for each would go now."""
    return api_runtime_of(request).country_service


def mitm_service_of(request: Request) -> MitmService:
    """A project's MITM captures, paged and cleared, and the CA certificate the gateways generated."""
    return api_runtime_of(request).mitm_service


def exit_report_service_of(request: Request) -> ExitReportService:
    """Exit reports taken without Postgres, the project from this process's project map, onto the reports stream."""
    return api_runtime_of(request).exit_report_service


def exit_ip_service_of(request: Request) -> ExitIpService:
    """The exit IPs view: a project's exit IP records paged and filtered, one IP's, and per-IP totals."""
    return api_runtime_of(request).exit_ip_service


def site_reputation_service_of(request: Request) -> SiteReputationService:
    """One site of a project across its places, a place's history, and what a draft Strength would do."""
    return api_runtime_of(request).site_reputation_service


def runtime_view_of(request: Request) -> RuntimeView:
    """The read model: definitions joined with the soft state other roles keep in Redis."""
    return api_runtime_of(request).runtime_view


def attention_of(request: Request) -> AttentionService:
    """Needs attention: a project's lines, every project's problem count, and the mutes editors set."""
    return api_runtime_of(request).attention


def install_attention_of(request: Request) -> InstallAttentionService:
    """The install-wide Needs attention lines of the Projects page, from the install's stored alerts."""
    return api_runtime_of(request).install_attention


def alert_history_of(request: Request) -> AlertHistory:
    """A project's alerts, or the install's, open and resolved."""
    return api_runtime_of(request).alert_history


def alert_settings_of(request: Request) -> AlertSettingsService:
    """A project's alert cut-offs, read and replaced."""
    return api_runtime_of(request).alert_settings


def system_settings_service_of(request: Request) -> SystemSettingsService:
    """The install's settings, and how long its resolved alerts and the audit log's entries are kept."""
    return api_runtime_of(request).system_settings_service


def audit_service_of(request: Request) -> AuditService:
    """An entity's History, the install's entities' for admins only."""
    return api_runtime_of(request).audit_service


def system_service_of(request: Request) -> SystemService:
    """The admin System view: the cluster from the heartbeats, the stores, and the gauge history."""
    return api_runtime_of(request).system_service


def release_service_of(request: Request) -> ReleaseService:
    """What's New: the bundled notes, what the user has not seen, and for admins a newer release."""
    return api_runtime_of(request).release_service


def backup_service_of(request: Request) -> BackupService:
    """The full setup exported as an encrypted file, and restored from one."""
    return api_runtime_of(request).backup_service


async def require_project(request: Request, project_id: str) -> Project:
    """The project a nested route names, read from Postgres; 404 when there is none."""
    async with session_factory_of(request)() as session:
        project = await ProjectRepository(session).get_by_id(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


# --- Attribution ------------------------------------------------------------------------------


def geo_database_store_of(request: Request) -> GeoDatabaseStore:
    """The IP databases this process has open, and their files (``blob_file``)."""
    return api_runtime_of(request).geo_database_store


def geo_settings_service_of(request: Request) -> GeoSettingsService:
    """The attribution settings, read and replaced, and a lookup of one IP under them."""
    return api_runtime_of(request).geo_settings_service


def geo_database_service_of(request: Request) -> GeoDatabaseService:
    """The IP databases: listed with where each is open, uploaded, registered, edited, downloaded, removed, inspected."""
    return api_runtime_of(request).geo_database_service


def reattribution_service_of(request: Request) -> ReattributionService:
    """Re-judging every proxy, or one connector's, asked of the workers."""
    return api_runtime_of(request).reattribution_service


def observation_history_of(request: Request) -> ObservationHistory:
    """The observation log and each connector's exit statistics: accuracy, recurrence, sharing and exit IPs."""
    return api_runtime_of(request).observation_history


def geo_status_service_of(request: Request) -> GeoStatusService:
    """The attribution pipeline's health: this api process, the observations stream and every live gateway."""
    return api_runtime_of(request).geo_status_service


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


def sso_provider_service_of(request: Request) -> SsoProviderService:
    """The admin's sign-in provider settings: each provider's OAuth client, read and replaced."""
    return api_runtime_of(request).sso_provider_service


def user_service_of(request: Request) -> UserService:
    """User accounts, people and service accounts, and the provider accounts linked to each."""
    return api_runtime_of(request).user_service


def api_token_service_of(request: Request) -> ApiTokenService:
    """Users' personal access tokens: listed, issued and revoked."""
    return api_runtime_of(request).api_token_service


def external_base_url(request: Request) -> str:
    """The URL this install is reached at, for links handed to browsers; never ending in a slash.

    ``OCTOPROX_PUBLIC_URL`` when set, since behind a load balancer the request's own base URL names the balancer's
    internal face; otherwise the request's.
    """
    public_url = settings_of(request).public_url
    if public_url:
        return public_url.rstrip("/")
    return str(request.base_url).rstrip("/")

