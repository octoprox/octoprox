# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Database migrations: one process at a time brings the schema to head, recording its revision in ``alembic_version``."""

import time
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, create_engine, pool, text

logger = structlog.get_logger()

# Path to migrations directory
MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"

# The lock every migrating process takes, a session lock on a connection of
# its own. Its key differs from migration 046's index-build lock.
_TRY_LOCK = text("SELECT pg_try_advisory_lock(hashtext('octoprox.migrations'))")
_UNLOCK = text("SELECT pg_advisory_unlock(hashtext('octoprox.migrations'))")
# Who holds it, for the log line while waiting. pg_locks shows a bigint key
# as two halves: classid the high one, objid the low one.
_HOLDER = text(
    "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted AND objsubid = 1 "
    "AND ((classid::bigint << 32) | objid::bigint) = hashtext('octoprox.migrations')"
)

# Seconds between tries for the lock. A wait lasts as long as another
# process's migrations, seconds to minutes, so a second keeps it short at
# the cost of one trivial query a second.
LOCK_POLL_SECONDS = 1.0


def run_migrations(database_url_sync: str) -> bool:
    """Bring the schema to head once no other process is migrating it; whether this call applied a revision.

    Args:
        database_url_sync: a sync URL (``postgresql+psycopg2://``, not ``postgresql+asyncpg://``).
    """
    return _SchemaMigration(database_url_sync).run()


def alembic_config(database_url_sync: str) -> Config:
    """Alembic pointed at this package's migrations and the database."""
    config = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # The option goes through ConfigParser interpolation, and a password
    # quoted into the URL carries percent signs.
    config.set_main_option("sqlalchemy.url", database_url_sync.replace("%", "%%"))
    return config


def current_revision(connection: Connection) -> str | None:
    """The revision the database records; None when ``alembic_version`` is missing or empty."""
    return MigrationContext.configure(connection).get_current_revision()


class _SchemaMigration:
    """One process's turn at the schema: it waits for the migration lock, then brings the database to head."""

    def __init__(self, database_url_sync: str) -> None:
        self._url = database_url_sync

    def run(self) -> bool:
        """Migrate once the lock is taken, and release it whatever happens; whether a revision was applied."""
        engine = create_engine(self._url, poolclass=pool.NullPool, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as connection:
                self._take_lock(connection)
                try:
                    return self._to_head(connection)
                finally:
                    connection.execute(_UNLOCK)
        finally:
            engine.dispose()

    def _to_head(self, connection: Connection) -> bool:
        """Upgrade the database to head; whether it moved."""
        before = current_revision(connection)
        logger.info("Running database migrations", revision=before)
        command.upgrade(alembic_config(self._url), "head")
        after = current_revision(connection)
        logger.info("Database migrations complete", revision=after, previous=before)
        return after != before

    @staticmethod
    def _take_lock(connection: Connection) -> None:
        """Take the migration lock, polling idle in between, so a waiter holds no snapshot; logs each holder found."""
        holder: int | None = None
        while not connection.execute(_TRY_LOCK).scalar():
            current = connection.execute(_HOLDER).scalar()
            if current != holder:
                logger.info("Waiting for the migration lock", holder_pid=current)
                holder = current
            time.sleep(LOCK_POLL_SECONDS)
