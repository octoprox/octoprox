# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Narrower observation log rows, and each project's own say over its log.

* ``ip_observations.candidates`` and ``ip_observations.instance_id`` are
  dropped. Every row carried each source's answer as JSON, with its key names,
  and the name of the process that saw it: roughly a third to a half of a
  row, and nothing showed either. A drop only changes the catalogue: the
  existing rows keep their bytes until the retention deletes them, and the
  narrower new rows reuse that space, so the table stops growing at once but
  its file shrinks only when the table is rewritten.
* ``projects.observation_log``: true for every existing project. Off, the
  project's sightings get no log row and still feed the exit table.
* ``projects.observation_retention_days``: null for every existing project,
  which inherits ``geo_settings.observation_retention_days``; 0 keeps forever.

The table takes every insert and the hourly retention delete, so each
statement waits at most half a second for its lock and is tried again after
a pause, as in 048: one waiting longer would hold every insert behind it.

Revision ID: 053
Revises: 052
Create Date: 2026-10-30
"""
import random
import time
from collections.abc import Sequence

import sqlalchemy as sa
import structlog
from alembic import op
from sqlalchemy import Connection, TextClause, text
from sqlalchemy.exc import DBAPIError

revision: str = "053"
down_revision: str | None = "052"
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

_DROP_COLUMNS = text("ALTER TABLE ip_observations DROP COLUMN IF EXISTS candidates, DROP COLUMN IF EXISTS instance_id")
_ADD_PROJECT_COLUMNS = text(
    "ALTER TABLE projects "
    "ADD COLUMN IF NOT EXISTS observation_log boolean NOT NULL DEFAULT true, "
    "ADD COLUMN IF NOT EXISTS observation_retention_days integer"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(_SET_LOCK_TIMEOUT)
        run_statement(bind, "ip_observations", _DROP_COLUMNS)
        run_statement(bind, "projects", _ADD_PROJECT_COLUMNS)
        bind.execute(_RESET_LOCK_TIMEOUT)


def downgrade() -> None:
    op.drop_column("projects", "observation_retention_days")
    op.drop_column("projects", "observation_log")
    # The rows written meanwhile come back with no answers and no process name.
    op.add_column("ip_observations", sa.Column("candidates", sa.JSON(), nullable=False, server_default="[]"))
    op.alter_column("ip_observations", "candidates", server_default=None)
    op.add_column("ip_observations", sa.Column("instance_id", sa.String(64), nullable=False, server_default=""))


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
