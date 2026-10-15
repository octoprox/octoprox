# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The per-project failure log: why requests failed, kept for reading on the Failures page.

The request metrics count failures; this says what they were. Every request
the proxy listener or a tunnel device could not complete is described as a
:class:`RequestFailure` (what kind of failure, what the client was answered,
the vendor's own answer when there was one, which connector and proxy) and
published on the ``request_failed`` signal. :class:`FailureLog` collects
them in memory and writes them to one capped Redis stream per project every
few seconds, the same shape as the IP observation pipeline: nothing on the
request path waits for Redis.

Volume is bounded three ways, because a broken upstream fails every request
sent to it:

- Within one window, failures that differ only in time (and session) are
  folded into one entry with a count, so ten thousand identical timeouts
  are one line saying x10000.
- Each project gets at most ``max_per_window`` distinct entries per window on
  each instance. Past that, failures are only tallied by kind, and the window
  writes one ``suppressed`` entry carrying the tally.
- The stream keeps ``max_entries`` entries and ``retention_hours`` of them.

The log is best effort. A window that cannot reach Redis is dropped, not
retried: it describes failures the metrics have already counted.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
from collections import Counter
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import structlog
from python_socks import ProxyConnectionError, ProxyError

from api.core import utc_now
from api.core.job_stats import job_stats
from api.core.signals import request_failed
from api.core.workers import WorkerName
from api.models.proxy import ProxyProtocol

if TYPE_CHECKING:
    from api.core.config import Settings
    from api.db.redis import RedisClient
    from api.models.proxy import Proxy

logger = structlog.get_logger()

# How often each instance writes its window to Redis, and so the span over
# which identical failures are folded together.
FAILURE_LOG_INTERVAL_SECONDS = 5.0
# Long exception texts (a vendor's HTML error page) are cut here.
MAX_MESSAGE_CHARS = 500
# The stream entry kind that carries a window's tally of failures not logged one by one.
SUPPRESSED = "suppressed"
# The query of any URL an error text quotes (rnet names the full request URL):
# it is where tokens travel, so it never reaches the log.
_URL_QUERY = re.compile(r"(https?://[^\s\"'?#]*)\?[^\s\"']*")
# python-socks' texts for a SOCKS5 upstream refusing the credentials, which
# carry no reply code: the SOCKS counterpart of an HTTP 407.
_SOCKS_AUTH_REFUSALS = ("authentication failure", "no acceptable authentication methods")


class FailureKind(StrEnum):
    """What went wrong, from the client's side of the proxy.

    The first group is what the request metrics count as failures: a proxy
    was chosen and the request through it did not get going. The second is
    requests Octoprox refused before using any proxy (the metrics count
    those nowhere). The last is a request inside an intercepted connection,
    whose connection the metrics already counted as a success.
    """

    # Counted as failures by the request metrics.
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_UNREACHABLE = "upstream_unreachable"
    UPSTREAM_REJECTED = "upstream_rejected"
    UPSTREAM_ERROR = "upstream_error"
    CLIENT_CLOSED = "client_closed"
    INTERRUPTED = "interrupted"
    # Refused before a proxy was used (traffic_limit_exceeded can also cut a
    # request that already had one, and is then counted).
    NO_PROXY_AVAILABLE = "no_proxy_available"
    ALL_PROXIES_QUARANTINED = "all_proxies_quarantined"
    TRAFFIC_LIMIT_EXCEEDED = "traffic_limit_exceeded"
    PREFLIGHT_MISMATCH = "preflight_mismatch"
    INVALID_LOCATION = "invalid_location"
    # One request inside an intercepted (MITM) connection.
    INTERCEPTED_REQUEST_FAILED = "intercepted_request_failed"


@dataclass(frozen=True)
class RequestInfo:
    """What the client asked for, set once per connection for the failures it may produce.

    Held in a context variable rather than passed down: each client
    connection runs in its own task, and the places a request can fail are
    many layers below where its target, session and location are known.
    Descriptive only; nothing routes on it.
    """

    target: str
    method: str | None = None  # None for a tunnel connection, which has no HTTP request
    session_id: str | None = None
    location: str | None = None  # LocationTarget.key
    peer_id: str | None = None
    peer_name: str | None = None


_request_info: ContextVar[RequestInfo | None] = ContextVar("octoprox_request_info", default=None)


def set_request_info(info: RequestInfo) -> None:
    """Describe the request the current connection is serving."""
    _request_info.set(info)


@dataclass
class RequestFailure:
    """One failed request, as published on ``request_failed``."""

    project_id: str
    kind: FailureKind
    # What the client was answered. A tunnel device is never answered (its
    # connection is closed); the status is then the one a proxy client gets.
    status: int
    message: str
    error_type: str | None = None
    method: str | None = None
    target: str | None = None
    session_id: str | None = None
    location: str | None = None
    peer_id: str | None = None
    peer_name: str | None = None
    proxy_id: str | None = None
    connector_id: str | None = None
    # host:port Octoprox dials for the upstream, never its credentials
    proxy: str | None = None
    # The vendor's HTTP status when an HTTP upstream refused the request.
    upstream_status: int | None = None
    latency_ms: float | None = None
    at: datetime = field(default_factory=utc_now)

    def coalesce_key(self) -> tuple[Any, ...]:
        """Failures equal in everything but time and session are folded into one entry."""
        return (
            self.project_id, self.kind, self.status, self.upstream_status, self.error_type, self.message,
            self.method, self.target, self.location, self.peer_id, self.proxy_id,
        )


def build_failure(
    project_id: str,
    kind: FailureKind,
    status: int,
    message: str,
    *,
    proxy: Proxy | None = None,
    exc: BaseException | None = None,
    latency_ms: float | None = None,
    upstream_status: int | None = None,
    method: str | None = None,
    target: str | None = None,
) -> RequestFailure:
    """A failure filled in from the current connection's :class:`RequestInfo`.

    ``method`` and ``target`` override the connection's, for a request inside
    an intercepted connection. The proxy's credentials are scrubbed from the
    message, exception texts sometimes carry the upstream URL, and so are the
    queries of any URLs it quotes.
    """
    info = _request_info.get()
    if proxy is not None:
        message = _scrub(message, proxy)
    message = _URL_QUERY.sub(r"\1", message)
    return RequestFailure(
        project_id=project_id,
        kind=kind,
        status=status,
        message=message[:MAX_MESSAGE_CHARS],
        error_type=type(exc).__name__ if exc is not None else None,
        method=method or (info.method if info else None),
        target=target or (info.target if info else None),
        session_id=info.session_id if info else None,
        location=info.location if info else None,
        peer_id=info.peer_id if info else None,
        peer_name=info.peer_name if info else None,
        proxy_id=proxy.id if proxy else None,
        connector_id=proxy.connector_id if proxy else None,
        # The address dialled, as error texts name it; not the discovered exit IP.
        proxy=f"{proxy.host}:{proxy.port}" if proxy else None,
        upstream_status=upstream_status,
        latency_ms=round(latency_ms, 1) if latency_ms is not None else None,
    )


def classify_upstream_error(exc: BaseException, proxy: Proxy) -> tuple[FailureKind, int | None]:
    """The kind of a failure on the upstream leg, and the vendor's HTTP status if it answered with one.

    python-socks raises ProxyError when the upstream answered the handshake
    with a refusal: an HTTP status for an HTTP upstream (407 bad
    credentials, 502 no exit available...), a reply code for SOCKS. Only
    the HTTP status is reported as one.
    """
    if isinstance(exc, ProxyError):
        code = exc.error_code if isinstance(exc.error_code, int) else None
        if code is None:
            refused = any(text in str(exc).lower() for text in _SOCKS_AUTH_REFUSALS)
            return (FailureKind.UPSTREAM_REJECTED if refused else FailureKind.UPSTREAM_ERROR), None
        is_http = proxy.protocol in (ProxyProtocol.HTTP, ProxyProtocol.HTTPS)
        return FailureKind.UPSTREAM_REJECTED, code if is_http else None
    if isinstance(exc, TimeoutError):
        return FailureKind.UPSTREAM_TIMEOUT, None
    # A bare OSError is what asyncio raises for no route to the host, or for
    # every address of a name failing ("Multiple exceptions"); gaierror is a
    # name that does not resolve. python-socks wraps the same as ProxyConnectionError.
    if isinstance(exc, ConnectionRefusedError | ProxyConnectionError | socket.gaierror) or type(exc) is OSError:
        return FailureKind.UPSTREAM_UNREACHABLE, None
    return FailureKind.UPSTREAM_ERROR, None


def _scrub(message: str, proxy: Proxy) -> str:
    if proxy.username and proxy.password:
        message = message.replace(proxy.url, f"{proxy.protocol.value}://{proxy.host}:{proxy.port}")
    if proxy.password and len(proxy.password) >= 3:
        message = message.replace(proxy.password, "****")
        # URL parsers quote the password when they print the URL back.
        message = message.replace(quote(proxy.password, safe=""), "****")
    return message


@dataclass
class _Entry:
    """A window's failures that share a coalesce key."""

    first: RequestFailure
    count: int = 1
    last_at: datetime | None = None
    latency_total: float = 0.0
    latency_n: int = 0

    def __post_init__(self) -> None:
        self.last_at = self.first.at
        self._add_latency(self.first)

    def add(self, failure: RequestFailure) -> None:
        self.count += 1
        self.last_at = max(self.last_at or failure.at, failure.at)
        self._add_latency(failure)

    def _add_latency(self, failure: RequestFailure) -> None:
        if failure.latency_ms is not None:
            self.latency_total += failure.latency_ms
            self.latency_n += 1

    def fields(self, connector_name: str | None) -> dict[str, str]:
        f = self.first
        latency = self.latency_total / self.latency_n if self.latency_n else None
        values: dict[str, Any] = {
            "kind": f.kind.value,
            "status": f.status,
            "message": f.message,
            "error_type": f.error_type,
            "method": f.method,
            "target": f.target,
            "session_id": f.session_id,
            "location": f.location,
            "peer_id": f.peer_id,
            "peer_name": f.peer_name,
            "proxy_id": f.proxy_id,
            "connector_id": f.connector_id,
            "connector_name": connector_name,
            "proxy": f.proxy,
            "upstream_status": f.upstream_status,
            "latency_ms": round(latency, 1) if latency is not None else None,
            "count": self.count,
            "first_at": f.at.isoformat(),
            "last_at": (self.last_at or f.at).isoformat(),
        }
        return {k: str(v) for k, v in values.items() if v is not None}


class FailureLog:
    """Per-instance window of failures, and the loop that writes it to the projects' streams."""

    def __init__(
        self,
        redis_client: RedisClient | None,
        settings: Settings,
        *,
        connector_name: Callable[[str], str | None] = lambda _connector_id: None,
        interval: float = FAILURE_LOG_INTERVAL_SECONDS,
    ) -> None:
        self._redis = redis_client
        self.enabled = settings.failure_log_enabled
        self._max_entries = settings.failure_log_max_entries
        self._retention_seconds = settings.failure_log_retention_hours * 3600
        self._max_per_window = settings.failure_log_max_per_window
        self._connector_name = connector_name
        self._interval = interval
        self._pending: dict[tuple[Any, ...], _Entry] = {}
        self._distinct: Counter[str] = Counter()
        self._suppressed: dict[str, Counter[str]] = {}
        self._suppressed_window: dict[str, tuple[datetime, datetime]] = {}
        self._running = False
        # Entries lost because Redis could not be reached, since start.
        self.dropped = 0

    def subscribe_to_signals(self) -> None:
        if self.enabled:
            request_failed.connect(self._on_request_failed)

    async def _on_request_failed(self, sender: object, failure: RequestFailure) -> None:
        self.record(failure)

    def record(self, failure: RequestFailure) -> None:
        """Add one failure to the window. Never blocks."""
        key = failure.coalesce_key()
        entry = self._pending.get(key)
        if entry is not None:
            entry.add(failure)
            return
        project_id = failure.project_id
        if self._distinct[project_id] >= self._max_per_window:
            self._suppressed.setdefault(project_id, Counter())[failure.kind.value] += 1
            first, _ = self._suppressed_window.get(project_id, (failure.at, failure.at))
            self._suppressed_window[project_id] = (first, failure.at)
            return
        self._pending[key] = _Entry(failure)
        self._distinct[project_id] += 1

    async def publish(self) -> int:
        """Write the window to Redis. Returns the number of stream entries written."""
        if self._redis is None or (not self._pending and not self._suppressed):
            return 0
        pending, self._pending = self._pending, {}
        suppressed, self._suppressed = self._suppressed, {}
        windows, self._suppressed_window = self._suppressed_window, {}
        self._distinct = Counter()

        # Oldest first, so the stream (newest last) reads in the order things happened.
        ordered = sorted(pending.values(), key=lambda e: e.last_at or e.first.at)
        entries = [
            (e.first.project_id, e.fields(self._connector_name(e.first.connector_id) if e.first.connector_id else None))
            for e in ordered
        ]
        for project_id, tally in suppressed.items():
            first_at, last_at = windows[project_id]
            entries.append((project_id, {
                "kind": SUPPRESSED,
                "status": "0",
                "message": (
                    f"{sum(tally.values())} more failures in this window were tallied but not logged one by one "
                    f"(over {self._max_per_window} distinct failures)"
                ),
                "count": str(sum(tally.values())),
                "breakdown": json.dumps(dict(tally)),
                "first_at": first_at.isoformat(),
                "last_at": last_at.isoformat(),
            }))
        try:
            await self._redis.append_failures(
                entries, max_entries=self._max_entries, retention_seconds=self._retention_seconds
            )
        except Exception as exc:
            self.dropped += len(entries)
            logger.warning("Writing the failure log failed", error=str(exc), entries=len(entries))
            return 0
        return len(entries)

    async def run(self) -> None:
        """Publisher loop: every instance runs one."""
        self._running = True
        job_stats.declare_interval(WorkerName.FAILURE_LOG_PUBLISHER, self._interval)
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                with job_stats.track(WorkerName.FAILURE_LOG_PUBLISHER) as run:
                    if await self.publish() == 0:
                        run.idle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Failure log publisher error", error=str(exc))

    def forget(self, project_id: str) -> None:
        """Drop what the window holds for a project that is gone, so the next write does not recreate its log."""
        self._pending = {key: e for key, e in self._pending.items() if e.first.project_id != project_id}
        self._distinct.pop(project_id, None)
        self._suppressed.pop(project_id, None)
        self._suppressed_window.pop(project_id, None)

    def stop(self) -> None:
        self._running = False
