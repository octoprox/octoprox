# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Indexes the observation log can page on at any size.

The log reads newest first under one filter at a time. Only connector and
proxy had an index, on the column alone, so a project, an IP or the
contradicted rows were found by walking the whole history newest first, and
a value that matched little walked all of it. Each new index leads with its
filter and then the time, so a page is a short read wherever it sits:

* ``ix_ip_observations_{project,connector,proxy,ip}_observed``: (filter,
  observed_at). The connector and proxy ones replace the single-column
  indexes.
* ``ix_ip_observations_contradicted``: observed_at over the contradicted rows
  only, a few MB.

Built CONCURRENTLY, so the flusher keeps writing while they build, which on a
large table takes minutes. Each index commits on its own, so a start-up killed
part way (by a start-up probe, say) keeps the ones it finished. The next try
polls for the advisory lock until the build the killed one left running on the
server ends, drops an index left invalid, and carries on: repeated attempts
converge.

Revision ID: 046
Revises: 045
Create Date: 2026-10-16
"""
import time
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "046"
down_revision: str | None = "045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW = {
    "ix_ip_observations_project_observed": "(project_id, observed_at)",
    "ix_ip_observations_connector_observed": "(connector_id, observed_at)",
    "ix_ip_observations_proxy_observed": "(proxy_id, observed_at)",
    "ix_ip_observations_ip_observed": "(ip, observed_at)",
    "ix_ip_observations_contradicted": "(observed_at) WHERE country_conflict IS TRUE",
}
_REPLACED = {
    "ix_ip_observations_connector_id": "(connector_id)",
    "ix_ip_observations_proxy_id": "(proxy_id)",
}
# One builder at a time, whichever instance or attempt it is. A session lock:
# a killed start-up's backend holds it until the build it was running ends.
# Polled, never waited on: a backend blocked in pg_advisory_lock holds a
# snapshot, the running build's last CONCURRENTLY phase waits for that
# snapshot to go, and Postgres breaks the cycle by aborting the build.
_TRY_LOCK = sa.text("SELECT pg_try_advisory_lock(hashtext('octoprox.046_observation_log_indexes'))")
_UNLOCK = sa.text("SELECT pg_advisory_unlock(hashtext('octoprox.046_observation_log_indexes'))")
_INVALID = sa.text(
    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :name AND NOT i.indisvalid"
)


_LOCK_POLL_SECONDS = 1.0


def _lock() -> None:
    """Take the builders' lock, idle between tries so no snapshot stalls the build holding it."""
    bind = op.get_bind()
    while not bind.execute(_TRY_LOCK).scalar():
        time.sleep(_LOCK_POLL_SECONDS)


def _build(indexes: dict[str, str]) -> None:
    bind = op.get_bind()
    for name, definition in indexes.items():
        # A build cut short leaves an invalid index that IF NOT EXISTS would keep.
        if bind.execute(_INVALID, {"name": name}).first():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON ip_observations {definition}")


def _drop(names: Sequence[str]) -> None:
    for name in names:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        _lock()
        try:
            _build(_NEW)
            _drop(list(_REPLACED))
        finally:
            op.execute(_UNLOCK)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _lock()
        try:
            _build(_REPLACED)
            _drop(list(_NEW))
        finally:
            op.execute(_UNLOCK)
