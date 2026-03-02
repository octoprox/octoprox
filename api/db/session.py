# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Database session management."""

from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from api.core.config import settings


def get_async_engine(
    database_url: str,
    application_name: str = "octoprox",
    debug: bool = False,
) -> AsyncEngine:
    """Create an async database engine.

    Args:
        database_url: Database connection URL.
        application_name: Application name for PostgreSQL connection.
        debug: Whether to echo SQL statements (for debugging).

    Returns:
        Configured async engine.
    """
    return create_async_engine(
        database_url,
        echo=debug,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        connect_args={"server_settings": {"application_name": application_name}},
    )


@lru_cache
def get_async_session_factory(
    database_url: str,
    application_name: str = "octoprox",
    debug: bool = False,
) -> async_sessionmaker[AsyncSession]:
    """Get or create an async session factory.

    Uses lru_cache to ensure we reuse the same engine/factory for the same URL.

    Args:
        database_url: Database connection URL.
        application_name: Application name for PostgreSQL connection.
        debug: Whether to echo SQL statements (for debugging).

    Returns:
        Async session factory.
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
    )
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

