# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Initial schema for sources, proxies, and metrics.

Revision ID: 001
Revises:
Create Date: 2026-02-11 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create sources table
    op.create_table(
        "sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("enabled", sa.Boolean(), default=True),
        sa.Column("config", sa.JSON(), default=dict),
        sa.Column("refresh_interval_seconds", sa.Integer(), default=300),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sources_name", "sources", ["name"])
    op.create_index("ix_sources_type", "sources", ["type"])

    # Create proxies table
    op.create_table(
        "proxies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(20), default="http"),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("password", sa.String(255), nullable=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tags", sa.JSON(), default=list),
        sa.Column("metadata", sa.JSON(), default=dict),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_proxies_host_port", "proxies", ["host", "port"])
    op.create_index("ix_proxies_source_id", "proxies", ["source_id"])

    # Create proxy_metrics table for historical data
    op.create_table(
        "proxy_metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("proxy_id", sa.String(36), sa.ForeignKey("proxies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("request_count", sa.Integer(), default=0),
        sa.Column("success_count", sa.Integer(), default=0),
        sa.Column("failure_count", sa.Integer(), default=0),
        sa.Column("avg_latency_ms", sa.Float(), default=0.0),
        sa.Column("status", sa.String(20), default="unknown"),
    )
    op.create_index("ix_proxy_metrics_proxy_id", "proxy_metrics", ["proxy_id"])
    op.create_index("ix_proxy_metrics_timestamp", "proxy_metrics", ["timestamp"])


def downgrade() -> None:
    op.drop_table("proxy_metrics")
    op.drop_table("proxies")
    op.drop_table("sources")

