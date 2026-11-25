# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Keep the history of exit reputation, for the Over time chart of a place, and how long each project keeps it with its
records.

* ``connector_factor_moves``: one row per (project, site, requested place level, connector, decision time) whose stored
  factor moved, written by the metrics flush in the transaction that moves it, with the factor before and after (null
  while not judged) and the record's lifetime requests and blocked reports once it decided.
* ``exit_ip_avoid_moves``: one row per (project, site, exit IP, time a batch of reports was applied) whose avoid's end
  changed, written by the report ingest in the transaction that changes it, with the end before and after (null for
  none). An avoid that simply runs out has no row: its end says when.
* ``projects.exit_reputation_retention_days``: how many days a project keeps its exit reputation data after it was
  last used ("Keep data for"): a site's records after the day they were last seen, and the moves of both after the time
  they were made. 30 for every existing project, the install-wide default it replaces; 0 keeps them forever.

A project's moves go with it. There is no foreign key to the connector or to the exit IP's record: a deleted
connector's rows and a swept record's rows age out with the project's setting, and reads keep what the project has now.
Nothing is backfilled: history starts with this release.

Rows are only inserted and deleted, never updated. Besides the key, whose order serves the chart's read of one site
for a few connectors or exit IPs over a range of time, each table has an index on ``(project_id, at)``: the hourly sweep
reads each project's rows older than its own setting from it, so it never reads the history a project keeps, and it
gives the time of a project's first move kept.

The new tables' foreign keys and the new column each take a lock on ``projects``, which waits behind every reader of
the table, and every statement on the table would queue behind it. The column's constant default makes its add a
change to the catalogue alone. So each waits at most half a second at a time and is tried again after a pause, as in 061. The
indexes are made on the new, empty tables, where nothing waits on them. Each step is safe to run again.

Revision ID: 064
Revises: 063
Create Date: 2026-11-25
"""
import random
import time
from collections.abc import Sequence

import structlog
from alembic import op
from sqlalchemy import Connection, TextClause, text
from sqlalchemy.exc import DBAPIError

revision: str = "064"
down_revision: str | None = "063"
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

_CREATE_MOVES = text("""
CREATE TABLE IF NOT EXISTS connector_factor_moves (
    project_id varchar(36) NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    site varchar(253) NOT NULL,
    place varchar(128) NOT NULL,
    connector_id varchar(36) NOT NULL,
    at timestamp NOT NULL,
    old_factor double precision,
    new_factor double precision,
    requests bigint NOT NULL,
    blocked_reports integer NOT NULL,
    PRIMARY KEY (project_id, site, place, connector_id, at)
)""")
_CREATE_MOVES_AT_INDEX = text(
    "CREATE INDEX IF NOT EXISTS ix_connector_factor_moves_project_at ON connector_factor_moves (project_id, at)"
)
_CREATE_AVOID_MOVES = text("""
CREATE TABLE IF NOT EXISTS exit_ip_avoid_moves (
    project_id varchar(36) NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    site varchar(253) NOT NULL,
    ip varchar(45) NOT NULL,
    at timestamp NOT NULL,
    old_avoid_until timestamp,
    new_avoid_until timestamp,
    PRIMARY KEY (project_id, site, ip, at)
)""")
_CREATE_AVOID_MOVES_AT_INDEX = text(
    "CREATE INDEX IF NOT EXISTS ix_exit_ip_avoid_moves_project_at ON exit_ip_avoid_moves (project_id, at)"
)
_ADD_RETENTION_DAYS = text(
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS exit_reputation_retention_days integer NOT NULL DEFAULT 30"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(_SET_LOCK_TIMEOUT)
        run_statement(bind, "projects", _CREATE_MOVES)
        run_statement(bind, "connector_factor_moves", _CREATE_MOVES_AT_INDEX)
        run_statement(bind, "projects", _CREATE_AVOID_MOVES)
        run_statement(bind, "exit_ip_avoid_moves", _CREATE_AVOID_MOVES_AT_INDEX)
        run_statement(bind, "projects", _ADD_RETENTION_DAYS)
        bind.execute(_RESET_LOCK_TIMEOUT)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS exit_ip_avoid_moves")
    op.execute("DROP TABLE IF EXISTS connector_factor_moves")
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS exit_reputation_retention_days")


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
