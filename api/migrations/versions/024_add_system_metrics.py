# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add install-wide system metrics snapshots.

Unlike ``proxy_metrics`` and ``project_metrics``, which accumulate *counters*
per entity, this table stores periodic *gauge* readings for the install as a
whole: how big the database is, how much memory Redis holds, how many entities
exist. One row per snapshot, written by whichever instance holds the
``system_snapshotter`` lease.

There is no ``granularity`` column and no compaction tier: a global snapshot
every few minutes is ~288 rows a day, so plain retention is enough.

Revision ID: 024
Revises: 023
Create Date: 2026-09-19

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '024'
down_revision: str | None = '023'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "system_metrics",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        # Storage
        sa.Column("database_size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("redis_memory_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("redis_keys", sa.BigInteger(), nullable=False, server_default="0"),
        # Inventory
        sa.Column("projects", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credentials", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("connectors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("connectors_enabled", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("users", sa.Integer(), nullable=False, server_default="0"),
        # Pool
        sa.Column("proxies_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("proxies_healthy", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("proxies_unhealthy", sa.Integer(), nullable=False, server_default="0"),
        # Breakdowns kept as JSON: charted rarely, but they are what answers
        # "which table grew" once the size trend raises the question.
        sa.Column("table_sizes", sa.JSON(), nullable=False),
        sa.Column("proxy_status_counts", sa.JSON(), nullable=False),
    )
    op.create_index("ix_system_metrics_timestamp", "system_metrics", ["timestamp"])


def downgrade() -> None:
    op.drop_index("ix_system_metrics_timestamp", table_name="system_metrics")
    op.drop_table("system_metrics")
