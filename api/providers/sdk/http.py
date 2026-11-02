# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Executes declarative :class:`HttpCallSpec` requests.

Responsibilities: render templates, run a two-step auth flow when the call
asks for one (with a short-lived token cache), apply the egress policy, follow
declared pagination, wait out a vendor's rate limit, decode the body and
record a redacted trace for the admin test panel and logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog

from api.providers.sdk.descriptor import AuthFlowSpec, HttpCallSpec, ProviderDescriptor
from api.providers.sdk.egress import EgressDeniedError, EgressGuard
from api.providers.sdk.extract import ValueExtractor
from api.providers.sdk.templating import RenderContext, TemplateRenderer

logger = structlog.get_logger()

ClientFactory = Callable[[], httpx.AsyncClient]
Sleep = Callable[[float], Awaitable[None]]

# A request the vendor answers with 429 Too Many Requests is sent again, up to
# RATE_LIMIT_RETRIES more times, after the wait its Retry-After names, or after
# RATE_LIMIT_BACKOFF_SECONDS, doubling per retry, when it names none. A vendor
# that asks for longer than RATE_LIMIT_MAX_WAIT_SECONDS is not waited for, and
# one call waits RATE_LIMIT_MAX_TOTAL_WAIT_SECONDS at most across all its pages,
# so a rate-limited vendor holds neither a sync slot nor an admin request for
# long: past either, the call fails with the 429 at once (a sync asks again on
# its next run).
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = 5.0
RATE_LIMIT_MAX_WAIT_SECONDS = 60.0
RATE_LIMIT_MAX_TOTAL_WAIT_SECONDS = 120.0

# How much of a vendor's error reply a failed call keeps: enough for its reason
# ({"detail": "Invalid page."}), never a whole error page.
ERROR_EXCERPT_CHARS = 200

_SENSITIVE_HEADERS = frozenset(
    {"authorization", "x-api-key", "x-access-token", "x-auth-token", "api-key", "token", "cookie"}
)
REDACTED = "***"


class HttpCallError(RuntimeError):
    """A declarative call failed (network, policy, decoding or status)."""

    def __init__(self, message: str, trace: CallTrace | None = None) -> None:
        super().__init__(message)
        self.trace = trace


class ResponseTooLargeError(HttpCallError):
    """The response body exceeded the configured size limit."""


@dataclass
class CallTrace:
    """Redacted record of one HTTP exchange."""

    method: str
    url: str
    status: int | None = None
    elapsed_ms: float = 0.0
    error: str | None = None
    page: int = 1
    headers: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "status": self.status,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "error": self.error,
            "page": self.page,
            "headers": self.headers,
        }


@dataclass
class CallResult:
    """Decoded pages plus their traces."""

    pages: list[Any]
    statuses: list[int]
    traces: list[CallTrace]

    @property
    def data(self) -> Any:
        return self.pages[0] if self.pages else None

    @property
    def status(self) -> int | None:
        return self.statuses[0] if self.statuses else None

    @property
    def ok(self) -> bool:
        return bool(self.statuses) and all(200 <= s < 300 for s in self.statuses)

    def items(self, extractor: ValueExtractor, path: str) -> list[Any]:
        """Concatenate ``path`` from every page."""
        items: list[Any] = []
        for page in self.pages:
            items.extend(extractor.extract_list(path, page))
        return items


class AuthTokenCache:
    """In-memory cache of auth-flow tokens keyed by the rendered login request."""

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str | None:
        entry = self._tokens.get(key)
        if entry is None:
            return None
        token, expires_at = entry
        if time.monotonic() >= expires_at:
            self._tokens.pop(key, None)
            return None
        return token

    def put(self, key: str, token: str, ttl_seconds: int) -> None:
        self._tokens[key] = (token, time.monotonic() + ttl_seconds)

    def clear(self) -> None:
        self._tokens.clear()


_default_token_cache = AuthTokenCache()


def default_client_factory(timeout: float) -> ClientFactory:
    def factory() -> httpx.AsyncClient:
        # Redirects are not followed: a 3xx could otherwise steer credentials
        # to a host that never went through the egress check.
        return httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    return factory


class HttpCallExecutor:
    """Runs :class:`HttpCallSpec` calls for one descriptor."""

    def __init__(
        self,
        descriptor: ProviderDescriptor,
        *,
        egress: EgressGuard,
        renderer: TemplateRenderer | None = None,
        extractor: ValueExtractor | None = None,
        timeout_seconds: float = 60.0,
        max_response_bytes: int = 0,
        client_factory: ClientFactory | None = None,
        token_cache: AuthTokenCache | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._descriptor = descriptor
        self._egress = egress
        self._renderer = renderer or TemplateRenderer()
        self._extractor = extractor or ValueExtractor()
        self._timeout = timeout_seconds
        self._max_bytes = max_response_bytes
        self._client_factory = client_factory or default_client_factory(timeout_seconds)
        self._token_cache = token_cache or _default_token_cache
        self._sleep = sleep

    async def execute(
        self,
        call: HttpCallSpec,
        ctx: RenderContext,
        *,
        raise_for_status: bool = True,
        on_page: Callable[[Any], bool] | None = None,
    ) -> CallResult:
        """Run ``call``; with ``raise_for_status`` a non-2xx raises :class:`HttpCallError`.

        ``on_page`` sees each decoded page as it arrives and answers whether the next one is wanted, so a caller that
        has what it needs stops the pagination there.
        """
        if call.auth is not None:
            ctx = await self._authenticate(call.auth, ctx)

        url = self._renderer.render_string(call.url, ctx)
        headers = self._renderer.render_mapping(call.headers, ctx)
        params = self._renderer.render_mapping(call.params, ctx, drop_empty=True)
        body = self._renderer.render_json(call.body, ctx) if call.body is not None else None
        timeout = call.timeout_seconds or self._timeout
        secrets = ctx.secret_values()

        pages: list[Any] = []
        statuses: list[int] = []
        traces: list[CallTrace] = []
        budget = _WaitBudget(RATE_LIMIT_MAX_TOTAL_WAIT_SECONDS)
        try:
            first_host = self._egress.check_static(url)
        except EgressDeniedError as exc:
            trace = CallTrace(method=call.method, url=_redact_url(url, params, secrets), error=f"egress denied: {exc}")
            raise HttpCallError(str(trace.error), trace) from exc

        async with self._client_factory() as client:
            page_no = 1
            next_url: str | None = url
            next_params: dict[str, str] | None = params
            while next_url is not None:
                status, decoded, trace = await self._send_page(
                    client, call.method, next_url, headers, next_params, body, timeout, secrets, page_no, traces, budget
                )
                if raise_for_status and not 200 <= status < 300:
                    reason = f": {trace.error}" if trace.error else ""
                    raise HttpCallError(f"{call.method} {trace.url} returned HTTP {status}{reason}", trace)
                pages.append(decoded)
                statuses.append(status)
                wanted = on_page is None or on_page(decoded)
                next_url = None
                next_params = None
                if wanted and call.paginate is not None and page_no < call.paginate.max_pages:
                    candidate = self._extractor.extract_str(call.paginate.next_url, decoded)
                    if candidate:
                        if self._egress.check_static(candidate) != first_host:
                            raise HttpCallError(
                                f"pagination pointed at a different host ({candidate}); refusing", trace
                            )
                        next_url = candidate
                        page_no += 1
        return CallResult(pages=pages, statuses=statuses, traces=traces)

    # --- auth -----------------------------------------------------------------

    async def _authenticate(self, flow_name: str, ctx: RenderContext) -> RenderContext:
        flow = self._descriptor.auth[flow_name]
        cache_key = self._auth_cache_key(flow_name, flow, ctx)
        token = self._token_cache.get(cache_key)
        if token is None:
            result = await self.execute(flow.call, ctx, raise_for_status=True)
            token = self._extractor.extract_str(flow.token_path, result.data)
            if not token:
                raise HttpCallError(
                    f"auth flow '{flow_name}' returned no token at '{flow.token_path}'",
                    result.traces[-1] if result.traces else None,
                )
            self._token_cache.put(cache_key, token, flow.ttl_seconds)
        return ctx.with_auth({"token": token})

    def _auth_cache_key(self, flow_name: str, flow: AuthFlowSpec, ctx: RenderContext) -> str:
        rendered = {
            "url": self._renderer.render_string(flow.call.url, ctx),
            "headers": self._renderer.render_mapping(flow.call.headers, ctx),
            "params": self._renderer.render_mapping(flow.call.params, ctx, drop_empty=True),
            "body": self._renderer.render_json(flow.call.body, ctx) if flow.call.body else None,
        }
        digest = hashlib.sha256(json.dumps(rendered, sort_keys=True).encode()).hexdigest()
        return f"{self._descriptor.id}:{flow_name}:{digest}"

    # --- transport ---------------------------------------------------------------

    async def _send_page(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, str] | None,
        body: Any,
        timeout: float,
        secrets: list[str],
        page_no: int,
        traces: list[CallTrace],
        budget: _WaitBudget,
    ) -> tuple[int, Any, CallTrace]:
        """Send one page, sending it again after a 429 as the rate-limit settings and the call's ``budget`` allow.

        Each try's trace joins ``traces``.
        """
        retry = 0
        while True:
            status, decoded, trace, retry_after = await self._send(
                client, method, url, headers, params, body, timeout, secrets, page_no
            )
            traces.append(trace)
            wait = _rate_limit_wait(retry_after, retry) if status == 429 and retry < RATE_LIMIT_RETRIES else None
            if wait is None or not budget.spend(wait):
                return status, decoded, trace
            retry += 1
            logger.info(
                "Vendor rate limited the call; sending it again",
                descriptor=self._descriptor.id, url=trace.url, wait_seconds=round(wait, 1), retry=retry,
            )
            await self._sleep(wait)

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, str] | None,
        body: Any,
        timeout: float,
        secrets: list[str],
        page_no: int,
    ) -> tuple[int, Any, CallTrace, str | None]:
        """One exchange: its status, decoded body, trace and Retry-After header."""
        trace = CallTrace(
            method=method,
            url=_redact_url(url, params, secrets),
            page=page_no,
            headers=_redact_headers(headers, secrets),
        )
        started = time.monotonic()
        try:
            target = await self._egress.resolve(url)
        except EgressDeniedError as exc:
            trace.error = f"egress denied: {exc}"
            raise HttpCallError(str(trace.error), trace) from exc

        request_headers = dict(headers)
        extensions: dict[str, Any] = {}
        if target.url.host != target.hostname:
            request_headers.setdefault("Host", target.hostname)
            extensions["sni_hostname"] = target.hostname
        try:
            async with client.stream(
                method,
                target.url,
                headers=request_headers,
                params=params or None,
                json=body,
                timeout=timeout,
                extensions=extensions,
            ) as response:
                raw = await self._read_body(response, trace)
                trace.status = response.status_code
                content_type = response.headers.get("content-type", "")
                retry_after = response.headers.get("retry-after")
        except HttpCallError:
            raise
        except httpx.HTTPError as exc:
            trace.error = f"{type(exc).__name__}: {exc}"
            trace.elapsed_ms = (time.monotonic() - started) * 1000
            raise HttpCallError(f"{method} {trace.url} failed: {trace.error}", trace) from exc
        trace.elapsed_ms = (time.monotonic() - started) * 1000
        if not 200 <= response.status_code < 300:
            trace.error = _error_excerpt(raw, secrets) or None
        return response.status_code, _decode_body(raw, content_type), trace, retry_after

    async def _read_body(self, response: httpx.Response, trace: CallTrace) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if self._max_bytes and total > self._max_bytes:
                trace.error = f"response exceeded {self._max_bytes} bytes"
                raise ResponseTooLargeError(trace.error, trace)
            chunks.append(chunk)
        return b"".join(chunks)


class _WaitBudget:
    """What one call may still spend waiting out rate limits, in seconds, across all its pages."""

    def __init__(self, seconds: float) -> None:
        self.remaining = seconds

    def spend(self, seconds: float) -> bool:
        """Take ``seconds`` from the budget; False, taking nothing, when that is more than is left."""
        if seconds > self.remaining:
            return False
        self.remaining -= seconds
        return True


def _rate_limit_wait(retry_after: str | None, retry: int) -> float | None:
    """Seconds to wait before retry ``retry`` of a 429: its Retry-After, else the backoff; None past the longest wait.

    Retry-After is seconds or an HTTP date; one that is neither counts as absent.
    """
    backoff = RATE_LIMIT_BACKOFF_SECONDS * 2.0**retry
    if retry_after is None:
        return backoff
    try:
        wait = float(retry_after)
    except ValueError:
        try:
            when = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError):
            return backoff
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        wait = (when - datetime.now(UTC)).total_seconds()
    if not math.isfinite(wait):
        return backoff
    wait = max(wait, 0.0)
    return wait if wait <= RATE_LIMIT_MAX_WAIT_SECONDS else None


def _error_excerpt(raw: bytes, secrets: list[str]) -> str:
    """The start of a vendor's error reply, for the error and the trace: whitespace collapsed, secrets redacted.

    Redacted before it is cut, so a secret is never left half shown.
    """
    text = _redact_text(" ".join(raw.decode("utf-8", errors="replace").split()), secrets)
    return text if len(text) <= ERROR_EXCERPT_CHARS else text[: ERROR_EXCERPT_CHARS - 3] + "..."


def _decode_body(raw: bytes, content_type: str) -> Any:
    text = raw.decode("utf-8", errors="replace")
    stripped = text.strip()
    if "json" in content_type or stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped) if stripped else None
        except json.JSONDecodeError:
            return text
    return text


def _redact_text(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def _redact_url(url: str, params: dict[str, str] | None, secrets: list[str]) -> str:
    rendered = url
    if params:
        query = "&".join(
            f"{k}={REDACTED if v in secrets else v}" for k, v in params.items()
        )
        rendered = f"{url}{'&' if '?' in url else '?'}{query}"
    return _redact_text(rendered, secrets)


def _redact_headers(headers: dict[str, str], secrets: list[str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in _SENSITIVE_HEADERS or any(s and s in value for s in secrets):
            redacted[name] = REDACTED
        else:
            redacted[name] = value
    return redacted
