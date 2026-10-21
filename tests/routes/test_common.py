# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What every admin route shares: the answer when Postgres or Redis does not answer.

An unreachable store is an outage, not a bug: the route answers 503 with
``Retry-After`` and logs one line, with neither the error's text in the answer
nor the statement or its parameters in the log.
"""

from __future__ import annotations

import socket
from typing import Any

import asyncpg.exceptions
import pytest
import redis.exceptions
import sqlalchemy.exc
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from api.core.config import Settings
from api.routes.common import UNAVAILABLE_RETRY_AFTER_SECONDS
from tests.conftest import api_runtime

# A connection the server dropped mid-request, as SQLAlchemy wraps it: the
# statement and its parameters are in the error's text.
_LOST = sqlalchemy.exc.DBAPIError(
    "UPDATE users SET password_hash = $1",
    ("secret-hash",),
    Exception("connection was closed in the middle of operation"),
    connection_invalidated=True,
)


def _failing_reads(client: TestClient, monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    """Every read of the projects list raises ``error``, as a store that does not answer makes it."""

    async def projects() -> Any:
        raise error

    monkeypatch.setattr(api_runtime(client).runtime_view, "projects", projects)


class TestStoreOutages:
    @pytest.mark.parametrize(
        ("error", "store"),
        [
            (ConnectionRefusedError(61, "Connect call failed ('10.0.0.5', 5432)"), "Postgres"),
            (TimeoutError(), "Postgres"),
            (socket.gaierror(-2, "Name or service not known"), "Postgres"),
            (sqlalchemy.exc.TimeoutError("QueuePool limit of size 5 overflow 10 reached"), "Postgres"),
            (_LOST, "Postgres"),
            (redis.exceptions.ConnectionError("Error 111 connecting to redis:6379. Connection refused."), "Redis"),
            (redis.exceptions.TimeoutError("Timeout reading from redis:6379"), "Redis"),
        ],
        ids=["refused", "timeout", "dns", "pool", "lost", "redis-down", "redis-slow"],
    )
    def test_answers_503_naming_the_store_never_the_error(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch, error: Exception, store: str
    ) -> None:
        _failing_reads(authenticated_client, monkeypatch, error)

        response = authenticated_client.get("/api/v1/projects")

        assert response.status_code == 503
        assert response.headers["retry-after"] == str(UNAVAILABLE_RETRY_AFTER_SECONDS)
        assert response.json() == {"detail": f"{store} cannot be reached; try again shortly"}

    def test_logs_one_line_with_the_drivers_message_and_no_parameters(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _failing_reads(authenticated_client, monkeypatch, _LOST)

        with structlog.testing.capture_logs() as logs:
            assert authenticated_client.get("/api/v1/projects").status_code == 503

        (line,) = [log for log in logs if log["event"] == "Request refused: a store cannot be reached"]
        assert (line["log_level"], line["store"], line["method"], line["path"]) == (
            "warning", "Postgres", "GET", "/api/v1/projects",
        )
        assert line["error"] == "Exception: connection was closed in the middle of operation"
        assert "secret-hash" not in str(logs)

    def test_a_refused_connect_reaches_the_route_as_asyncpg_raises_it(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing wraps a socket error while the pool connects: the socket's own error is what the handlers see."""

        async def projects() -> Any:
            engine = create_async_engine("postgresql+asyncpg://octoprox:pw@127.0.0.1:1/octoprox")
            try:
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
            finally:
                await engine.dispose()

        monkeypatch.setattr(api_runtime(authenticated_client).runtime_view, "projects", projects)

        response = authenticated_client.get("/api/v1/projects")

        assert response.status_code == 503
        assert response.json() == {"detail": "Postgres cannot be reached; try again shortly"}

    @pytest.mark.parametrize(
        "error",
        [
            asyncpg.exceptions.CannotConnectNowError("the database system is starting up"),
            asyncpg.exceptions.TooManyConnectionsError("sorry, too many clients already"),
            # SQLSTATE class 08, as a pooler such as PgBouncer refuses a login.
            asyncpg.exceptions.ProtocolViolationError("no more connections allowed (max_client_conn)"),
        ],
        ids=["starting-up", "no-slots", "class-08"],
    )
    def test_a_server_refusing_the_connect_is_an_outage(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        """SQLAlchemy wraps the server's refusal in a DBAPIError; asyncpg's error is its driver_exception."""

        async def refuse() -> Any:
            raise error

        async def projects() -> Any:
            engine = create_async_engine("postgresql+asyncpg://octoprox:pw@127.0.0.1:1/octoprox", async_creator=refuse)
            try:
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
            finally:
                await engine.dispose()

        monkeypatch.setattr(api_runtime(authenticated_client).runtime_view, "projects", projects)

        with structlog.testing.capture_logs() as logs:
            response = authenticated_client.get("/api/v1/projects")

        assert response.status_code == 503
        assert response.headers["retry-after"] == str(UNAVAILABLE_RETRY_AFTER_SECONDS)
        assert response.json() == {"detail": "Postgres cannot be reached; try again shortly"}
        (line,) = [log for log in logs if log["event"] == "Request refused: a store cannot be reached"]
        assert line["error"] == f"{type(error).__name__}: {error}"

    def test_any_other_database_error_is_still_a_server_error(
        self, authenticated_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed statement on a live connection is a bug, raised on as before (the test client re-raises it)."""
        _failing_reads(
            authenticated_client,
            monkeypatch,
            sqlalchemy.exc.IntegrityError("INSERT INTO projects", {}, Exception("duplicate key")),
        )

        with pytest.raises(sqlalchemy.exc.IntegrityError):
            authenticated_client.get("/api/v1/projects")

    def test_a_class_08_error_on_a_live_connection_is_raised_as_itself(
        self, authenticated_client: TestClient, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Class 08 refuses a connect; met by a statement on a live connection it is a protocol violation, a bug."""

        async def projects() -> Any:
            engine = create_async_engine(test_settings.database_url)
            try:
                async with engine.connect() as connection:
                    # As Postgres answers a malformed message, such as binary
                    # data cut short: an ERROR, and the connection stays open.
                    await connection.execute(
                        text(
                            "DO $$ BEGIN RAISE EXCEPTION 'insufficient data left in message'"
                            " USING ERRCODE = '08P01'; END $$"
                        )
                    )
            finally:
                await engine.dispose()

        monkeypatch.setattr(api_runtime(authenticated_client).runtime_view, "projects", projects)

        with pytest.raises(sqlalchemy.exc.DBAPIError) as raised:
            authenticated_client.get("/api/v1/projects")
        assert isinstance(raised.value.driver_exception, asyncpg.exceptions.ProtocolViolationError)

    def test_a_cached_statement_gone_stale_is_raised_as_itself(
        self, authenticated_client: TestClient, test_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DDL from another process (a migration at its start) leaves a cached statement stale.

        SQLAlchemy's error for it carries no driver error, so a handler asking
        for one would raise in its place.
        """

        async def projects() -> Any:
            engine = create_async_engine(test_settings.database_url)
            try:
                async with engine.connect() as connection:
                    await connection.execute(text("CREATE TEMPORARY TABLE stale (id int)"))
                    await connection.execute(text("SELECT * FROM stale"))
                    # SQLAlchemy does not take a textual ALTER for DDL, so it keeps
                    # the statement it cached, as when another process alters a table.
                    await connection.execute(text("ALTER TABLE stale ADD COLUMN name text"))
                    await connection.execute(text("SELECT * FROM stale"))
            finally:
                await engine.dispose()

        monkeypatch.setattr(api_runtime(authenticated_client).runtime_view, "projects", projects)

        with pytest.raises(sqlalchemy.exc.NotSupportedError, match="cached statement plan is invalid"):
            authenticated_client.get("/api/v1/projects")
