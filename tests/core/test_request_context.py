# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for per-request correlation IDs and log context binding."""

from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from starlette.testclient import TestClient

from api.core.request_context import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    accept_request_id,
    get_request_id,
)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ctx")
    async def ctx() -> dict[str, object]:
        return {
            "bound": structlog.contextvars.get_contextvars(),
            "var": get_request_id(),
        }

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("unhandled")

    return app


def _scope(headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {"type": "http", "method": "GET", "path": "/", "headers": headers or []}


async def _no_receive() -> dict[str, Any]:  # pragma: no cover - never awaited by these apps
    return {"type": "http.request"}


async def _call_direct(app: Any, headers: list[tuple[bytes, bytes]] | None = None) -> list[dict[str, Any]]:
    """Run the middleware in *this* task's context and collect what it sent."""
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(dict(message))

    await RequestContextMiddleware(app)(_scope(headers), _no_receive, send)
    return sent


_LAST_SENT: list[dict[str, Any]] = []


async def _call_direct_capturing(
    app: Any, headers: list[tuple[bytes, bytes]] | None = None
) -> list[dict[str, Any]]:
    """Like ``_call_direct`` but what was sent survives the app raising."""
    _LAST_SENT.clear()

    async def send(message: dict[str, Any]) -> None:
        _LAST_SENT.append(dict(message))

    await RequestContextMiddleware(app)(_scope(headers), _no_receive, send)
    return _LAST_SENT


class TestAcceptRequestId:
    def test_accepts_plain_ids(self) -> None:
        assert accept_request_id("abc-123_x.y:z") == "abc-123_x.y:z"

    def test_rejects_empty_and_none(self) -> None:
        assert accept_request_id("") is None
        assert accept_request_id(None) is None

    def test_rejects_too_long(self) -> None:
        assert accept_request_id("a" * 129) is None
        assert accept_request_id("a" * 128) == "a" * 128

    def test_rejects_unsafe_characters(self) -> None:
        assert accept_request_id("bad id") is None
        assert accept_request_id("new\nline") is None
        assert accept_request_id("<script>") is None


class TestRequestContextMiddleware:
    def test_generates_id_and_echoes_it(self) -> None:
        with TestClient(_app()) as client:
            response = client.get("/ctx")

        assert response.status_code == 200
        request_id = response.headers[REQUEST_ID_HEADER]
        assert len(request_id) == 32
        body = response.json()
        assert body["bound"] == {"request_id": request_id}
        assert body["var"] == request_id

    def test_honours_caller_supplied_id(self) -> None:
        with TestClient(_app()) as client:
            response = client.get("/ctx", headers={REQUEST_ID_HEADER: "client-abc-1"})

        assert response.headers[REQUEST_ID_HEADER] == "client-abc-1"
        assert response.json()["bound"]["request_id"] == "client-abc-1"

    def test_replaces_unsafe_caller_id(self) -> None:
        with TestClient(_app()) as client:
            response = client.get("/ctx", headers={REQUEST_ID_HEADER: "x" * 200})

        assert response.headers[REQUEST_ID_HEADER] != "x" * 200
        assert len(response.headers[REQUEST_ID_HEADER]) == 32

    async def test_context_does_not_leak_between_requests(self) -> None:
        """Run two requests in one context, as a reused connection context would."""
        seen: list[dict[str, Any]] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            seen.append(dict(structlog.contextvars.get_contextvars()))
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        structlog.contextvars.bind_contextvars(stale="from-before", user_id="old-user")
        try:
            await _call_direct(app, [(b"x-request-id", b"first")])
            await _call_direct(app)
        finally:
            structlog.contextvars.clear_contextvars()

        assert seen[0] == {"request_id": "first"}
        assert set(seen[1]) == {"request_id"}
        assert seen[1]["request_id"] != "first"

    async def test_unhandled_exception_gets_500_with_header_and_keeps_context(self) -> None:
        """The server logs the traceback after we return, so context must still be bound then."""

        async def app(scope: Any, receive: Any, send: Any) -> None:
            structlog.contextvars.bind_contextvars(user_id="u-1")
            raise RuntimeError("boom")

        try:
            with pytest.raises(RuntimeError, match="boom"):
                await _call_direct_capturing(app, [(b"x-request-id", b"trace-500")])
            sent = _LAST_SENT
            assert sent[0]["type"] == "http.response.start"
            assert sent[0]["status"] == 500
            assert (b"x-request-id", b"trace-500") in sent[0]["headers"]
            assert sent[1]["body"] == b"Internal Server Error"
            assert structlog.contextvars.get_contextvars() == {"request_id": "trace-500", "user_id": "u-1"}
            assert get_request_id() == "trace-500"
        finally:
            structlog.contextvars.clear_contextvars()

    async def test_exception_after_response_started_is_not_answered_twice(self) -> None:
        async def app(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise RuntimeError("mid-stream")

        try:
            with pytest.raises(RuntimeError, match="mid-stream"):
                await _call_direct_capturing(app)
            starts = [m for m in _LAST_SENT if m["type"] == "http.response.start"]
            assert len(starts) == 1
            assert starts[0]["status"] == 200
        finally:
            structlog.contextvars.clear_contextvars()

    def test_500_response_carries_request_id_through_full_stack(self) -> None:
        with TestClient(_app(), raise_server_exceptions=False) as client:
            response = client.get("/boom", headers={REQUEST_ID_HEADER: "trace-500"})

        assert response.status_code == 500
        assert response.headers[REQUEST_ID_HEADER] == "trace-500"


class TestAppIntegration:
    def test_every_api_response_carries_request_id(self, async_client: TestClient) -> None:
        response = async_client.get("/health")
        assert response.status_code == 200
        assert len(response.headers[REQUEST_ID_HEADER]) == 32

    def test_unauthenticated_error_carries_request_id(self, async_client: TestClient) -> None:
        response = async_client.get("/api/v1/projects", headers={REQUEST_ID_HEADER: "trace-me"})
        assert response.status_code == 401
        assert response.headers[REQUEST_ID_HEADER] == "trace-me"

    def test_cors_exposes_request_id(self, async_client: TestClient) -> None:
        response = async_client.get("/health", headers={"Origin": "http://localhost:5173"})
        exposed = response.headers.get("access-control-expose-headers", "")
        assert REQUEST_ID_HEADER.lower() in exposed.lower()
