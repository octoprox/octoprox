# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Alerts with history, each project's alert cut-offs, and how long the install's resolved alerts are kept.

* ``alerts``: one row per alert, a line of a project's Needs attention card or of the install's (no project), from when
  it opened until it resolved; the alerts job opens, refreshes, resolves and reopens them, and the api only reads them.
  A project's go with it. ``uq_alerts_open`` holds one open alert per scope and key, ``ix_alerts_resolved`` finds the
  resolved alert a returning condition reopens, and ``ix_alerts_project_opened`` serves a project's history.
* ``projects.alert_settings``: ``{}`` for every existing project, which keeps the default cut-offs. It holds the values
  a project sets apart from the defaults; the routing feed never reads it.
* ``system_settings.install_alert_retention_days``: 90 on the install's settings row, when there is one; days the
  install's resolved alerts are kept, 0 forever.

Constant defaults make both adds a change to the catalogue alone, but each still waits for its lock behind every reader
of its table, and every statement on the table would queue behind it; the new table's foreign key takes a lock on
``projects`` too. So each waits at most half a second at a time and is tried again after a pause, as in 061. The
indexes are made on the new, empty table, where nothing waits on them.

Revision ID: 062
Revises: 061
Create Date: 2026-11-18
"""
import random
import time
from collections.abc import Sequence

import structlog
from alembic import op
from sqlalchemy import Connection, TextClause, text
from sqlalchemy.exc import DBAPIError

revision: str = "062"
down_revision: str | None = "061"
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

_ADD_ALERT_SETTINGS = text(
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS alert_settings jsonb NOT NULL DEFAULT '{}'::jsonb"
)
_ADD_INSTALL_RETENTION = text(
    "ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS install_alert_retention_days integer NOT NULL DEFAULT 90"
)
_CREATE_ALERTS = text("""
CREATE TABLE IF NOT EXISTS alerts (
    id varchar(36) PRIMARY KEY,
    project_id varchar(36) REFERENCES projects(id) ON DELETE CASCADE,
    key varchar(300) NOT NULL,
    kind varchar(48) NOT NULL,
    severity varchar(10) NOT NULL,
    worst_severity varchar(10) NOT NULL,
    lead text NOT NULL DEFAULT '',
    subject text NOT NULL DEFAULT '',
    body text NOT NULL,
    detail text,
    when_kind varchar(16),
    when_at timestamp,
    link varchar(300) NOT NULL DEFAULT '',
    action varchar(64) NOT NULL DEFAULT '',
    sort_name varchar(255) NOT NULL DEFAULT '',
    hidden_by varchar(300) NOT NULL DEFAULT '',
    opened_at timestamp NOT NULL,
    last_seen_at timestamp NOT NULL,
    resolved_at timestamp,
    reopened_at timestamp,
    reopened_count integer NOT NULL DEFAULT 0
)""")
_CREATE_OPEN_INDEX = text(
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_open ON alerts ((coalesce(project_id, '')), key) "
    "WHERE resolved_at IS NULL"
)
_CREATE_RESOLVED_INDEX = text(
    "CREATE INDEX IF NOT EXISTS ix_alerts_resolved ON alerts ((coalesce(project_id, '')), key, resolved_at) "
    "WHERE resolved_at IS NOT NULL"
)
_CREATE_LIST_INDEX = text("CREATE INDEX IF NOT EXISTS ix_alerts_project_opened ON alerts (project_id, opened_at)")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(_SET_LOCK_TIMEOUT)
        run_statement(bind, "projects", _ADD_ALERT_SETTINGS)
        run_statement(bind, "system_settings", _ADD_INSTALL_RETENTION)
        run_statement(bind, "projects", _CREATE_ALERTS)
        for index in (_CREATE_OPEN_INDEX, _CREATE_RESOLVED_INDEX, _CREATE_LIST_INDEX):
            run_statement(bind, "alerts", index)
        bind.execute(_RESET_LOCK_TIMEOUT)


def downgrade() -> None:
    op.drop_table("alerts")
    op.drop_column("system_settings", "install_alert_retention_days")
    op.drop_column("projects", "alert_settings")


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
