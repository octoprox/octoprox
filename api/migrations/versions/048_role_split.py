# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Role split, additive and online: the proxy status, the running metric totals and the metric flushes, beside a running 5.x.

Revision ID: 048
Revises: 047
Create Date: 2026-10-17
"""
import random
import time
from collections.abc import Sequence
from typing import Any

import structlog
from alembic import op
from sqlalchemy import Connection, CursorResult, TextClause, text
from sqlalchemy.exc import DBAPIError

revision: str = "048"
down_revision: str | None = "047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = structlog.get_logger()

# Additive only, so 5.x runs on against the result: the version columns of
# projects, credentials, connectors and proxies stay, since 5.x still writes them.

# Every statement runs alone in autocommit and waits at most half a second for
# its lock, since 5.x's statements on that table queue behind a waiting one.
_SET_LOCK_TIMEOUT = text("SET lock_timeout = '500ms'")
_RESET_LOCK_TIMEOUT = text("RESET lock_timeout")
# A statement refused its lock in time (lock_not_available), or picked to
# break a deadlock (deadlock_detected), is tried again after a pause drawn
# from this range, in seconds, until this many seconds after its first try.
RETRY_PAUSE_SECONDS = (1.0, 3.0)
RETRY_BUDGET_SECONDS = 600.0
_RETRIED = frozenset({"55P03", "40P01"})
# Proxies per batch of the status fill.
STATUS_BATCH = 1000

# A constant default is kept in the catalogue, so the table is not rewritten.
_ADD_STATUS = text(
    "ALTER TABLE proxies "
    "ADD COLUMN IF NOT EXISTS status varchar(16) NOT NULL DEFAULT 'unknown', "
    "ADD COLUMN IF NOT EXISTS status_changed_at timestamp"
)
# One batch of the status fill, in id order after :last: each proxy still
# unknown gets the status its 5.x row implies, draining since its
# draining_started_at (a naive UTC ISO string), else healthy. It returns the
# batch's last id, NULL past the last proxy, so the pass ends however many rows
# 5.x inserts meanwhile; one inserted behind it stays unknown until checked.
_FILL_STATUSES = text(
    """
    WITH batch AS (SELECT id FROM proxies WHERE id > :last ORDER BY id LIMIT :size),
    filled AS (
        UPDATE proxies SET
            status = CASE WHEN metadata->>'draining_started_at' IS NULL THEN 'healthy' ELSE 'draining' END,
            status_changed_at = (metadata->>'draining_started_at')::timestamp
        WHERE id IN (SELECT id FROM batch) AND status = 'unknown'
    )
    SELECT max(id) FROM batch
    """
)
_CREATE_TOTALS = text(
    """
    CREATE TABLE IF NOT EXISTS metric_totals (
        kind varchar(16) NOT NULL,
        entity_id varchar(64) NOT NULL,
        request_count bigint NOT NULL DEFAULT 0,
        success_count bigint NOT NULL DEFAULT 0,
        failure_count bigint NOT NULL DEFAULT 0,
        latency_sum_ms double precision NOT NULL DEFAULT 0,
        bytes_sent bigint NOT NULL DEFAULT 0,
        bytes_received bigint NOT NULL DEFAULT 0,
        by_address bigint NOT NULL DEFAULT 0,
        encrypted_dns_blocked bigint NOT NULL DEFAULT 0,
        updated_at timestamp NOT NULL,
        PRIMARY KEY (kind, entity_id)
    )
    """
)
# Each entity's history summed, latency back as a weighted sum so later
# windows add to it, stamped with its newest row's time; an entity with totals
# already keeps them. The history columns are nullable (since 001) and the
# totals' are not, so a sum over nothing but NULLs counts as 0. A frozen copy:
# MetricsRepository.rebuild_totals keeps the live one.
_FILL_TOTALS = text(
    """
    INSERT INTO metric_totals (
        kind, entity_id, request_count, success_count, failure_count, latency_sum_ms,
        bytes_sent, bytes_received, by_address, encrypted_dns_blocked, updated_at
    )
    SELECT 'proxy', proxy_id, coalesce(sum(request_count), 0), coalesce(sum(success_count), 0),
           coalesce(sum(failure_count), 0), coalesce(sum(avg_latency_ms * request_count), 0),
           coalesce(sum(bytes_sent), 0), coalesce(sum(bytes_received), 0), 0, 0,
           coalesce(max(timestamp), timezone('UTC', now()))
    FROM proxy_metrics GROUP BY proxy_id
    UNION ALL
    SELECT 'project', project_id, coalesce(sum(request_count), 0), coalesce(sum(success_count), 0),
           coalesce(sum(failure_count), 0), coalesce(sum(avg_latency_ms * request_count), 0),
           coalesce(sum(bytes_sent), 0), coalesce(sum(bytes_received), 0), 0, 0,
           coalesce(max(timestamp), timezone('UTC', now()))
    FROM project_metrics GROUP BY project_id
    UNION ALL
    SELECT 'tunnel_peer', peer_id, coalesce(sum(request_count), 0), coalesce(sum(success_count), 0),
           coalesce(sum(failure_count), 0), coalesce(sum(avg_latency_ms * request_count), 0),
           coalesce(sum(bytes_sent), 0), coalesce(sum(bytes_received), 0),
           coalesce(sum(by_address), 0), coalesce(sum(encrypted_dns_blocked), 0),
           coalesce(max(timestamp), timezone('UTC', now()))
    FROM tunnel_peer_metrics GROUP BY peer_id
    ON CONFLICT (kind, entity_id) DO NOTHING
    """
)
# The metrics flushes committed, by their job entry's ID: a flush delivered
# again after its commit finds its ID and only clears its staging hash.
_CREATE_FLUSHES = text(
    "CREATE TABLE IF NOT EXISTS metric_flushes (message_id varchar(64) PRIMARY KEY, flushed_at timestamp NOT NULL)"
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.execute(_SET_LOCK_TIMEOUT)
        run_statement(bind, "proxies", _ADD_STATUS)
        fill_statuses(bind)
        run_statement(bind, "metric_totals", _CREATE_TOTALS)
        run_statement(bind, "metric_totals", _FILL_TOTALS)
        run_statement(bind, "metric_flushes", _CREATE_FLUSHES)
        bind.execute(_RESET_LOCK_TIMEOUT)


def downgrade() -> None:
    op.drop_table("metric_flushes")
    op.drop_table("metric_totals")
    op.drop_column("proxies", "status_changed_at")
    op.drop_column("proxies", "status")


def fill_statuses(bind: Connection) -> None:
    """Give every proxy still unknown the status its 5.x row implies, ``STATUS_BATCH`` proxies at a time."""
    last: str | None = ""
    while last is not None:
        last = run_statement(bind, "proxies", _FILL_STATUSES, last=last, size=STATUS_BATCH).scalar()


def run_statement(bind: Connection, table: str, statement: TextClause, **params: Any) -> CursorResult[Any]:
    """Run one statement, tried again while its lock on ``table`` is not granted in time or a deadlock picks it."""
    started = time.monotonic()
    attempt = 1
    while True:
        try:
            return bind.execute(statement, params)
        except DBAPIError as error:
            if getattr(error.orig, "pgcode", None) not in _RETRIED or time.monotonic() - started > RETRY_BUDGET_SECONDS:
                raise
        logger.warning("Migration statement waiting for a lock; retrying", table=table, attempt=attempt)
        time.sleep(random.uniform(*RETRY_PAUSE_SECONDS))
        attempt += 1
