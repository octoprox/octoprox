# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""How many minutes a dynamic connector's exit reputation evidence takes to count half at the least.

* ``projects.exit_reputation_connector_half_life_minutes``: 10 for every existing project. A connector record's
  evidence now counts half after these minutes or its "Connector memory" in requests, whichever is longer, and never
  later than "Remember for"; before, after whichever came first, and at least every hour. The stored requests stay.

A constant default makes the add a change to the catalogue alone, but the statement still waits for its lock behind
every reader of the table, and every statement on the table would queue behind it. So it waits at most half a second
at a time and is tried again after a pause, as in 061. It is safe to run again.

Revision ID: 065
Revises: 064
Create Date: 2026-11-26
"""
import random
import time
from collections.abc import Sequence

import structlog
from alembic import op
from sqlalchemy import Connection, TextClause, text
from sqlalchemy.exc import DBAPIError

revision: str = "065"
down_revision: str | None = "064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = structlog.get_logger()

_SET_LOCK_TIMEOUT = text("SET lock_timeout = '500ms'")
_RESET_LOCK_TIMEOUT = text("RESET lock_timeout")
# A statement refused its lock in time (lock_not_available), or picked to
# break a deadlock (deadlock_detected), is tried again after a pause drawn
# from this range, in seconds, until this many seconds after its first try.
RETRY_PAUSE_SECONDS = (1.0, 3.0)
RETRY_BUDGET_SECONDS = 600.0
_RETRIED = frozenset({"55P03", "40P01"})

_ADD_MINUTES = text("""
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS exit_reputation_connector_half_life_minutes integer NOT NULL DEFAULT 10
""")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(_SET_LOCK_TIMEOUT)
        run_statement(bind, "projects", _ADD_MINUTES)
        bind.execute(_RESET_LOCK_TIMEOUT)


def downgrade() -> None:
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS exit_reputation_connector_half_life_minutes")


def run_statement(bind: Connection, table: str, statement: TextClause) -> None:
    """Run one statement, tried again while its lock on ``table`` is not granted in time or a deadlock picks it."""
    started = time.monotonic()
    attempt = 1
    while True:
        try:
            bind.execute(statement)
            return
        except DBAPIError as error:
            if getattr(error.orig, "pgcode", None) not in _RETRIED or time.monotonic() - started > RETRY_BUDGET_SECONDS:
                raise
        logger.warning("Migration statement waiting for a lock; retrying", table=table, attempt=attempt)
        time.sleep(random.uniform(*RETRY_PAUSE_SECONDS))
        attempt += 1
