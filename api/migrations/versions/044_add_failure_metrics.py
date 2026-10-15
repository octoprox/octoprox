# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add failure_metrics: per-project counts of failed requests by kind.

The sixth metrics history table, behind the Failures page summary. The
failure log keeps examples; this keeps every failure counted, by what went
wrong (the kind), the vendor's status when it refused, and the connector
that carried the request (none for a request refused before a proxy was
chosen). Same flush, compaction tiers and retention as the other five.

Revision ID: 044
Revises: 043
Create Date: 2026-10-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '044'
down_revision: str | None = '043'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "failure_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("connector_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("upstream_status", sa.Integer(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("granularity", sa.Integer(), nullable=False, server_default="60"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connector_id"], ["connectors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # The page reads one project's window; compaction walks a project's rows
    # by granularity and age. The connector index serves the cascade.
    op.create_index("ix_failure_metrics_project_ts", "failure_metrics", ["project_id", "timestamp"])
    op.create_index(
        "ix_failure_metrics_project_granularity_ts", "failure_metrics", ["project_id", "granularity", "timestamp"]
    )
    op.create_index("ix_failure_metrics_connector_id", "failure_metrics", ["connector_id"])


def downgrade() -> None:
    op.drop_index("ix_failure_metrics_connector_id", table_name="failure_metrics")
    op.drop_index("ix_failure_metrics_project_granularity_ts", table_name="failure_metrics")
    op.drop_index("ix_failure_metrics_project_ts", table_name="failure_metrics")
    op.drop_table("failure_metrics")
