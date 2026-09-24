# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Per-request correlation: a request ID on every API call and every log line.

``RequestContextMiddleware`` gives each HTTP request an ID, taken from an
inbound ``X-Request-ID`` header when the caller sent a usable one and
generated otherwise. The ID is bound into structlog's context variables so
every log line emitted while handling the request carries ``request_id``,
and it is echoed back in the ``X-Request-ID`` response header so a user
report ("this failed") can be matched to the server logs.

``get_current_user`` adds ``user_id`` and ``username`` to the same context
once a token is verified, so log lines are attributable to a person without
each call site having to pass the user along.

This is a pure ASGI middleware rather than ``BaseHTTPMiddleware``: it runs
in the same task as the endpoint, which is what makes context variables set
here visible to route handlers and dependencies, and it does not buffer
streaming responses.

Context is cleared at the start of a request, not at the end. Each request
runs in its own task with a copy of the connection's context, so nothing
leaks between requests either way, but an unhandled exception is logged by
the server *after* this middleware has returned, and that traceback is the
one line an operator most needs to carry ``request_id`` and ``user_id``.
For the same reason the middleware sends the 500 itself when the response
has not started, so the request ID header is present on error responses
too, then re-raises so the server still records the exception.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

REQUEST_ID_HEADER = "X-Request-ID"

# Inbound IDs are logged verbatim, so only accept a conservative charset and
# a bounded length. Anything else is replaced with a generated ID; the caller
# still sees the ID we used in the response header.
_MAX_REQUEST_ID_LENGTH = 128
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

_INTERNAL_ERROR_BODY = b"Internal Server Error"

# Readable from anywhere in the request's task, e.g. to include the ID in an
# error response body. Empty outside a request.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def new_request_id() -> str:
    """Generate a request ID: a 32-character hex UUID, no dashes."""
    return uuid.uuid4().hex


def accept_request_id(value: str | None) -> str | None:
    """Return ``value`` if it is safe to log and echo, otherwise ``None``."""
    if not value:
        return None
    if len(value) > _MAX_REQUEST_ID_LENGTH:
        return None
    if not _REQUEST_ID_RE.match(value):
        return None
    return value


def get_request_id() -> str:
    """The current request's ID, or an empty string outside a request."""
    return request_id_var.get()


class RequestContextMiddleware:
    """Assign a request ID, bind it for logging, and echo it on the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header_name = REQUEST_ID_HEADER.lower().encode("latin-1")
        inbound: str | None = None
        for name, value in scope.get("headers", []):
            if name == header_name:
                inbound = value.decode("latin-1", errors="replace")
                break

        request_id = accept_request_id(inbound) or new_request_id()

        # Start from a clean slate: the task's context is a copy of the
        # connection's, which may carry whatever the previous owner bound.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id

        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = list(message.get("headers", []))
                headers.append((header_name, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            # Starlette's ServerErrorMiddleware sits outside this one and
            # would answer with the bare `send`, losing the header. Answer
            # here instead when nothing has been sent yet; it skips its own
            # response once one has started and re-raises for the server log.
            if not response_started:
                await send_with_request_id(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [
                            (b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(_INTERNAL_ERROR_BODY)).encode("latin-1")),
                        ],
                    }
                )
                await send_with_request_id({"type": "http.response.body", "body": _INTERNAL_ERROR_BODY})
            raise
