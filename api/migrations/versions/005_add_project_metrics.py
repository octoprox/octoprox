# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add project_metrics table for aggregate project-level metrics.

Revision ID: 005
Revises: 004
Create Date: 2026-02-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '005'
down_revision: Union[str, None] = '004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create project_metrics table for aggregate project-level metrics
    # These metrics persist across proxy rotation
    op.create_table(
        "project_metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_latency_ms", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("bytes_sent", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bytes_received", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_index("ix_project_metrics_project_id", "project_metrics", ["project_id"])
    op.create_index("ix_project_metrics_timestamp", "project_metrics", ["timestamp"])


def downgrade() -> None:
    op.drop_index("ix_project_metrics_timestamp", table_name="project_metrics")
    op.drop_index("ix_project_metrics_project_id", table_name="project_metrics")
    op.drop_table("project_metrics")

