# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add host_metrics: per-project request history by destination host and connector.

The fifth metrics history table. Every completed request through the proxy
port or a tunnel is counted against the host it was for (the CONNECT target
or the URL host) under the connector that carried it, so a project can see
where its traffic goes and which connector serves which host. Same shape,
flush, compaction tiers and retention as the other four tables. Rows go
with their connector, as connector_metrics rows do.

Revision ID: 036
Revises: 035
Create Date: 2026-10-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '036'
down_revision: str | None = '035'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "host_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("connector_id", sa.String(length=36), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_latency_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("bytes_sent", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bytes_received", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("granularity", sa.Integer(), nullable=False, server_default="60"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connector_id"], ["connectors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # The page reads one project's window: filter by project and time, then
    # group by host or by connector. Compaction walks a project's rows by
    # granularity and age. The connector index serves the cascade.
    op.create_index("ix_host_metrics_project_ts", "host_metrics", ["project_id", "timestamp"])
    op.create_index(
        "ix_host_metrics_project_granularity_ts", "host_metrics", ["project_id", "granularity", "timestamp"]
    )
    op.create_index("ix_host_metrics_connector_id", "host_metrics", ["connector_id"])


def downgrade() -> None:
    op.drop_index("ix_host_metrics_connector_id", table_name="host_metrics")
    op.drop_index("ix_host_metrics_project_granularity_ts", table_name="host_metrics")
    op.drop_index("ix_host_metrics_project_ts", table_name="host_metrics")
    op.drop_table("host_metrics")
