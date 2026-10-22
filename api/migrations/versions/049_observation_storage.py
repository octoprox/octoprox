# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Smaller indexes on the observation tables, and a vacuum that keeps pace with the observation retention.

``ip_observations``: the log's project, connector, proxy and IP filters each
get an index on the column alone in place of (filter, observed_at). A
single-column b-tree stores a repeated value once, so these are about a tenth
of the size, while the composites, too wide for Postgres to pack rows that
arrive in time order, split their pages in half and settled near twice their
built size. Autovacuum runs once 2% of the rows have changed rather than 20%,
so the space the hourly retention delete frees is reused ten times sooner:
within about an hour and a half instead of fourteen at a 3-day retention.

``connector_exit_ips``: last_seen loses both its indexes and new pages keep a
fifth free, so the ingest's updates, which move last_seen on every batch,
rewrite a row in its page with no index writes. The table is not rewritten:
its pages already hold the space vacuum freed between churned row versions,
so the updates go HOT at once, and a rewrite would wait for an exclusive lock
with the ingest queued behind it.

The observation indexes are built CONCURRENTLY, as in migration 046 and under
a lock of their own: the ingest keeps writing while they build, a start-up
killed part way keeps what it finished, and the next try waits for a build
left running on the server, drops an index left invalid and carries on.

Revision ID: 049
Revises: 048
Create Date: 2026-10-22
"""
import time
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "049"
down_revision: str | None = "048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW = {
    "ix_ip_observations_project_id": "(project_id)",
    "ix_ip_observations_connector_id": "(connector_id)",
    "ix_ip_observations_proxy_id": "(proxy_id)",
    "ix_ip_observations_ip": "(ip)",
}
_REPLACED = {
    "ix_ip_observations_project_observed": "(project_id, observed_at)",
    "ix_ip_observations_connector_observed": "(connector_id, observed_at)",
    "ix_ip_observations_proxy_observed": "(proxy_id, observed_at)",
    "ix_ip_observations_ip_observed": "(ip, observed_at)",
}
_EXIT_LAST_SEEN = {
    "ix_connector_exit_ips_last_seen": "(last_seen)",
    "ix_connector_exit_ips_connector_last_seen": "(connector_id, last_seen)",
}
_AUTOVACUUM = ("autovacuum_vacuum_scale_factor", "autovacuum_vacuum_insert_scale_factor")
# One builder at a time, whichever instance or attempt it is: a session lock,
# which a killed start-up's backend holds until the build it was running ends.
# Polled, never waited on, for the reason migration 046 gives.
_TRY_LOCK = sa.text("SELECT pg_try_advisory_lock(hashtext('octoprox.049_observation_storage'))")
_UNLOCK = sa.text("SELECT pg_advisory_unlock(hashtext('octoprox.049_observation_storage'))")
_INVALID = sa.text(
    "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :name AND NOT i.indisvalid"
)

_LOCK_POLL_SECONDS = 1.0


def _lock() -> None:
    """Take the builders' lock, idle between tries so no snapshot stalls the build holding it."""
    bind = op.get_bind()
    while not bind.execute(_TRY_LOCK).scalar():
        time.sleep(_LOCK_POLL_SECONDS)


def _build(table: str, indexes: dict[str, str]) -> None:
    bind = op.get_bind()
    for name, definition in indexes.items():
        # A build cut short leaves an invalid index that IF NOT EXISTS would keep.
        if bind.execute(_INVALID, {"name": name}).first():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {definition}")


def _drop(names: Sequence[str]) -> None:
    for name in names:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        _lock()
        try:
            _drop(list(_EXIT_LAST_SEEN))
            op.execute("ALTER TABLE connector_exit_ips SET (fillfactor = 80)")
            op.execute(f"ALTER TABLE ip_observations SET ({', '.join(f'{name} = 0.02' for name in _AUTOVACUUM)})")
            _build("ip_observations", _NEW)
            _drop(list(_REPLACED))
        finally:
            op.execute(_UNLOCK)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _lock()
        try:
            _build("ip_observations", _REPLACED)
            _drop(list(_NEW))
            op.execute(f"ALTER TABLE ip_observations RESET ({', '.join(_AUTOVACUUM)})")
            op.execute("ALTER TABLE connector_exit_ips RESET (fillfactor)")
            _build("connector_exit_ips", _EXIT_LAST_SEEN)
        finally:
            op.execute(_UNLOCK)
