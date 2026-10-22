# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The per-project failure log: why requests failed, for the Failures page, written to Redis a window at a time.

Each window counts every failure and keeps folded, capped examples per kind, so a flood of one never pushes out the rare.
In Redis every gateway's windows add up in one row per distinct failure.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
from collections import Counter
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import structlog
from python_socks import ProxyConnectionError, ProxyError

from api.core import utc_now
from api.core.publishers import BatchPublisher
from api.core.stats import FailureCountKey, FailureExample, FailureKind
from api.core.workers import WorkerName
from api.models.proxy import ProxyProtocol

if TYPE_CHECKING:
    from api.core.config import Settings
    from api.db.redis import BatchPart, RedisClient
    from api.models.proxy import Proxy

logger = structlog.get_logger()

# How often each instance writes its window to Redis, and so the span over
# which identical failures are folded together.
FAILURE_LOG_INTERVAL_SECONDS = 5.0
# Long exception texts (a vendor's HTML error page) are cut here.
MAX_MESSAGE_CHARS = 500
# The query of any URL an error text quotes (rnet names the full request URL):
# it is where tokens travel, so it never reaches the log.
_URL_QUERY = re.compile(r"(https?://[^\s\"'?#]*)\?[^\s\"']*")
# python-socks' texts for a SOCKS5 upstream refusing the credentials, which
# carry no reply code: the SOCKS counterpart of an HTTP 407.
_SOCKS_AUTH_REFUSALS = ("authentication failure", "no acceptable authentication methods")


@dataclass(frozen=True)
class RequestInfo:
    """What the client asked for, set once per connection for the failures it may produce; nothing routes on it.

    Held in a context variable, since a request can fail many layers below where its target and session are known.
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
    # The vendor's HTTP status when an HTTP upstream refused the request, and
    # what it said about it besides (its explaining headers and body text).
    upstream_status: int | None = None
    upstream_message: str | None = None
    latency_ms: float | None = None
    at: datetime = field(default_factory=utc_now)

    def coalesce_key(self) -> tuple[Any, ...]:
        """Failures equal in everything but time and session are folded into one entry."""
        return (
            self.project_id, self.kind, self.status, self.upstream_status, self.upstream_message, self.error_type,
            self.message, self.method, self.target, self.location, self.peer_id, self.proxy_id,
        )

    def fingerprint(self) -> str:
        """The coalesce key as a short hash, the same on every gateway: the row in Redis this failure adds to."""
        return hashlib.blake2b(json.dumps(self.coalesce_key()).encode(), digest_size=8).hexdigest()

    def count_key(self) -> FailureCountKey:
        """What the failure is counted under in failure_metrics."""
        return self.project_id, self.connector_id, self.kind.value, self.upstream_status


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
    upstream_message: str | None = None,
    method: str | None = None,
    target: str | None = None,
) -> RequestFailure:
    """A failure filled in from the current connection's :class:`RequestInfo`, its message scrubbed of credentials and queries.

    Args:
        method, target: the request's own, overriding the connection's, inside an intercepted connection.
    """
    info = _request_info.get()
    if proxy is not None:
        message = _scrub(message, proxy)
        upstream_message = _scrub(upstream_message, proxy) if upstream_message else upstream_message
    message = _URL_QUERY.sub(r"\1", message)
    if upstream_message:
        upstream_message = _URL_QUERY.sub(r"\1", upstream_message)[:MAX_MESSAGE_CHARS]
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
        upstream_message=upstream_message or None,
        latency_ms=round(latency_ms, 1) if latency_ms is not None else None,
    )


def classify_upstream_error(exc: BaseException, proxy: Proxy) -> tuple[FailureKind, int | None]:
    """The kind of a failure on the upstream leg, and the vendor's HTTP status if it answered with one.

    A refused handshake carries an HTTP status from an HTTP upstream and a reply code from SOCKS; only a status is reported.
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

    def example(self, connector_name: str | None) -> FailureExample:
        """The window's part of the example: the first failure's description, and how often and when."""
        f = self.first
        values: dict[str, Any] = {
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
            "upstream_message": f.upstream_message,
        }
        return FailureExample(
            project_id=f.project_id,
            kind=f.kind.value,
            fingerprint=f.fingerprint(),
            fields=json.dumps({k: v for k, v in values.items() if v is not None}),
            count=self.count,
            first_ms=_epoch_ms(f.at),
            last_ms=_epoch_ms(self.last_at or f.at),
            latency_total=round(self.latency_total, 1),
            latency_n=self.latency_n,
        )


def _epoch_ms(at: datetime) -> int:
    """A naive UTC time (``utc_now``) in epoch milliseconds."""
    return int(at.replace(tzinfo=UTC).timestamp() * 1000)


class FailureLog(BatchPublisher):
    """A gateway's window of failed requests, folded and capped, and the publisher that writes it to Redis every ``interval``.

    Args:
        redis_client: takes each window on its ``failures`` batch channel, one writer per process; None keeps it.
    """

    def __init__(
        self,
        redis_client: RedisClient | None,
        settings: Settings,
        *,
        connector_name: Callable[[str], str | None] = lambda _connector_id: None,
        interval: float = FAILURE_LOG_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(redis_client, name=WorkerName.FAILURE_LOG_PUBLISHER, interval=interval)
        self.enabled = settings.failure_log_enabled
        self._max_entries = settings.failure_log_max_entries
        self._retention_seconds = settings.failure_log_retention_hours * 3600
        self._max_per_window = settings.failure_log_max_per_window
        self._connector_name = connector_name
        self._pending: dict[tuple[Any, ...], _Entry] = {}
        # Distinct examples this window, per (project, kind).
        self._distinct: Counter[tuple[str, str]] = Counter()
        self._counts: Counter[FailureCountKey] = Counter()

    def record(self, failure: RequestFailure) -> None:
        """Count one failure and keep it as an example unless the window has enough of its kind. Never blocks."""
        self._counts[failure.count_key()] += 1
        key = failure.coalesce_key()
        entry = self._pending.get(key)
        if entry is not None:
            entry.add(failure)
            return
        slot = (failure.project_id, failure.kind.value)
        if self._distinct[slot] >= self._max_per_window:
            return
        self._pending[key] = _Entry(failure)
        self._distinct[slot] += 1

    def forget(self, project_id: str) -> None:
        """Drop what the window holds for a project that is gone, so the next write does not recreate its keys.

        A window already sent and owed goes as it was, so a project forgotten meanwhile lands once more.
        """
        self._pending = {key: e for key, e in self._pending.items() if e.first.project_id != project_id}
        self._distinct = Counter({slot: n for slot, n in self._distinct.items() if slot[0] != project_id})
        self._counts = Counter({key: n for key, n in self._counts.items() if key[0] != project_id})

    def forget_connector(self, connector_id: str) -> None:
        """Drop the window's counts for a connector that is gone, so the next write does not recreate its hashes.

        Its examples stay: they belong to the project and read as a deleted connector.
        """
        self._counts = Counter({key: n for key, n in self._counts.items() if key[1] != connector_id})

    def _windowed(self) -> int:
        """The window's examples and counts."""
        return len(self._pending) + len(self._counts)

    def _cut(self, redis_client: RedisClient) -> Sequence[BatchPart]:
        """The window as batch parts, and a new window begun."""
        pending, self._pending = self._pending, {}
        counts, self._counts = self._counts, Counter()
        self._distinct = Counter()
        examples = [
            e.example(self._connector_name(e.first.connector_id) if e.first.connector_id else None)
            for e in pending.values()
        ]
        return redis_client.failures_batch(
            examples, dict(counts), max_entries=self._max_entries, retention_seconds=self._retention_seconds
        )
