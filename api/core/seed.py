# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Seed initial admin user on first startup."""

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.core.password import hash_password
from api.db.repository import UserRepository
from api.models.user import User, UserRole

logger = structlog.get_logger()


async def seed_admin_user(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Seed the initial admin user if no users exist in the database.

    Uses auth_username and auth_password from settings.
    Only creates the admin user if the users table is empty.
    """
    if not settings.auth_password:
        logger.warning("No auth_password configured, skipping admin seed")
        return

    async with session_factory() as session:
        repo = UserRepository(session)
        user_count = await repo.count()

        if user_count > 0:
            logger.info("Users already exist, skipping admin seed", count=user_count)
            return

        admin = User(
            username=settings.auth_username,
            email="",
            password_hash=hash_password(settings.auth_password),
            role=UserRole.ADMIN,
        )

        try:
            await repo.create(admin)
            await session.commit()
            logger.info("Seeded initial admin user", username=admin.username)
        except IntegrityError:
            await session.rollback()
            logger.info("Admin user already exists (concurrent seed)")
