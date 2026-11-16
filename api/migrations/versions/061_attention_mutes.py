# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Each project's muted Needs attention lines.

* ``projects.attention_mutes``: ``{}`` for every existing project, which has muted nothing. It holds the lines the
  project's editors muted, by line key, each with the severity it was muted at, until when, and who muted it. The
  routing feed never reads it.

A constant default makes the add a change to the catalogue alone, but it still waits for its lock behind every
reader of the table, and every statement on the table would queue behind it. So it waits at most half a second at a
time and is tried again after a pause, as in 048 and 053.

Revision ID: 061
Revises: 060
Create Date: 2026-11-16
"""
import random
import time
from collections.abc import Sequence

import structlog
from alembic import op
from sqlalchemy import Connection, TextClause, text
from sqlalchemy.exc import DBAPIError

revision: str = "061"
down_revision: str | None = "060"
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

_ADD_MUTES = text(
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS attention_mutes jsonb NOT NULL DEFAULT '{}'::jsonb"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(_SET_LOCK_TIMEOUT)
        run_statement(bind, "projects", _ADD_MUTES)
        bind.execute(_RESET_LOCK_TIMEOUT)


def downgrade() -> None:
    op.drop_column("projects", "attention_mutes")


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
