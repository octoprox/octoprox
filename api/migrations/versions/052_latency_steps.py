# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Latency over successful requests only, and split into steps for projects and connectors.

* ``project_metrics`` and ``connector_metrics``: ``avg_route_ms``,
  ``avg_preflight_ms``, ``avg_connect_ms``, ``avg_handshake_ms`` and
  ``avg_response_ms``, each step's average over the row's successful
  requests. Null in every existing row, which has no steps.
* ``metric_totals.latency_sum_ms``: from now on a sum over the successful
  requests, so the averages divide it by them. Each existing sum, which every
  request added to, is scaled by the entity's share of successes, so its
  average stays what it was and later requests move it. Done only on the run
  that adds the columns: a second run would scale it twice. The downgrade
  scales it back as it drops them, so upgrading again scales it once.

Existing history rows keep their average over every request, now weighted by
their successful requests.

Revision ID: 052
Revises: 051
Create Date: 2026-10-28
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "052"
down_revision: str | None = "051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STEP_COLUMNS = ("avg_route_ms", "avg_preflight_ms", "avg_connect_ms", "avg_handshake_ms", "avg_response_ms")
TABLES = ("project_metrics", "connector_metrics")

_RESCALE_TOTALS = sa.text(
    "UPDATE metric_totals SET latency_sum_ms = latency_sum_ms * success_count / request_count "
    "WHERE request_count > 0 AND success_count < request_count"
)
# The reverse, where it can be: a sum with no successes stays 0.
_UNSCALE_TOTALS = sa.text(
    "UPDATE metric_totals SET latency_sum_ms = latency_sum_ms * request_count / success_count "
    "WHERE success_count > 0 AND success_count < request_count"
)


def upgrade() -> None:
    bind = op.get_bind()
    first_run = "avg_route_ms" not in {column["name"] for column in sa.inspect(bind).get_columns("project_metrics")}
    for table in TABLES:
        added = ", ".join(f"ADD COLUMN IF NOT EXISTS {column} double precision" for column in STEP_COLUMNS)
        op.execute(f"ALTER TABLE {table} {added}")
    if first_run:
        op.execute(_RESCALE_TOTALS)


def downgrade() -> None:
    op.execute(_UNSCALE_TOTALS)
    for table in TABLES:
        for column in STEP_COLUMNS:
            op.drop_column(table, column)
