# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Database migration utilities."""

from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config

from api.core.config import settings

logger = structlog.get_logger()

# Path to migrations directory
MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def run_migrations(database_url_sync: str | None = None) -> None:
    """Run all pending database migrations.

    Args:
        database_url_sync: Optional sync database URL override. If not provided,
                          uses the URL from settings. Useful for tests.
                          Must be a sync URL (postgresql://, not postgresql+asyncpg://).
    """
    logger.info("Running database migrations")

    alembic_cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(MIGRATIONS_DIR))

    # Use provided URL or fall back to settings (sync URL for migrations)
    url = database_url_sync or settings.database_url_sync
    alembic_cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(alembic_cfg, "head")

    logger.info("Database migrations complete")

