# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Database session management, with connections that give up on a server gone silent.

Keepalive and a user timeout on each socket fail a connection to a server that
vanished without resetting it (a Multi-AZ failover that moves the address, a
lost zone) within about twenty seconds rather than about fifteen minutes;
callers handle it as a refused connection, and the pool reconnects.
"""

import socket
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager
from functools import lru_cache
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from api.core.config import settings
from api.core.keepalive import set_keepalive

# What components take to open sessions on demand: the async_sessionmaker the
# lifespan builds, or anything else callable the same way (tests pass stubs).
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# How a connection notices a silent server. Keepalive probes start after 10 s
# without traffic and repeat every 5 s, and data or probes left unacknowledged
# for 20 s close the connection: what bounds a pre-ping or query on a dead
# connection. A healthy server's kernel answers the probes however long its
# query runs, so neither limits a slow query. Linux takes all four; macOS
# has no user timeout.
_KEEPALIVE_IDLE_SECONDS = 10
_KEEPALIVE_INTERVAL_SECONDS = 5
_KEEPALIVE_PROBES = 3
_USER_TIMEOUT_MS = 20_000
# Seconds opening a connection may take, rather than asyncpg's 60: a server
# that does not answer at all is given up on as soon as one that is slow.
_CONNECT_TIMEOUT_SECONDS = 10


def get_async_engine(
    database_url: str,
    application_name: str = "octoprox",
    debug: bool = False,
) -> AsyncEngine:
    """Create an async database engine whose connections give up on a silent server (see the module docstring).

    Args:
        debug: whether to echo SQL statements.
    """
    engine = create_async_engine(
        database_url,
        echo=debug,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        connect_args={
            "server_settings": {"application_name": application_name},
            "timeout": _CONNECT_TIMEOUT_SECONDS,
        },
    )
    event.listen(engine.sync_engine, "connect", _bound_silent_server)
    return engine


def _bound_silent_server(dbapi_connection: Any, _record: Any) -> None:
    """Keepalive and a user timeout on a new connection's socket, so a silent server fails it within seconds.

    The socket comes from asyncpg's transport, which has no option for them.
    """
    sock = dbapi_connection.driver_connection._transport.get_extra_info("socket")
    # A Unix socket has no network between it and the server.
    if sock is None or sock.family not in (socket.AF_INET, socket.AF_INET6):
        return
    set_keepalive(
        sock,
        idle=_KEEPALIVE_IDLE_SECONDS,
        interval=_KEEPALIVE_INTERVAL_SECONDS,
        probes=_KEEPALIVE_PROBES,
        user_timeout_ms=_USER_TIMEOUT_MS,
    )


@lru_cache
def get_async_session_factory(
    database_url: str,
    application_name: str,
    debug: bool,
    /,
) -> async_sessionmaker[AsyncSession]:
    """The process's session factory for a database: one engine, one pool.

    Cached on its arguments, which are positional and have no defaults so that
    every caller spells the cache key the same way and the process holds one
    engine.
    """
    engine = get_async_engine(database_url, application_name, debug)
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Get database session for dependency injection.

    Yields:
        Database session with automatic commit/rollback.
    """
    session_factory = get_async_session_factory(
        settings.database_url,
        settings.db_application_name,
        settings.debug,
    )
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

