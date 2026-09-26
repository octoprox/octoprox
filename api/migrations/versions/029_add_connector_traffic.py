# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Per-connector traffic limits, pricing and metrics history.

``connectors.traffic_config`` holds the traffic period, byte limit, action at
the limit and price per GB (see ``TrafficConfig``). ``traffic_reset_at`` is
set by a manual usage reset: usage in the current period counts from there.

``connector_metrics`` is the third metrics history table, per connector. The
proxy_metrics rows cascade with their proxy, so a connector's traffic over a
billing period could not be summed reliably from them once cloud rotation or
a provider re-sync had replaced its proxies.

Revision ID: 029
Revises: 028
Create Date: 2026-09-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '029'
down_revision: str | None = '028'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connectors",
        sa.Column("traffic_config", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column("connectors", sa.Column("traffic_reset_at", sa.DateTime(), nullable=True))

    op.create_table(
        "connector_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("connector_id", sa.String(length=36), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_latency_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("bytes_sent", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bytes_received", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("granularity", sa.Integer(), nullable=False, server_default="60"),
        sa.ForeignKeyConstraint(["connector_id"], ["connectors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_connector_metrics_connector_id", "connector_metrics", ["connector_id"])
    op.create_index("ix_connector_metrics_timestamp", "connector_metrics", ["timestamp"])
    op.create_index(
        "ix_connector_metrics_connector_granularity_ts",
        "connector_metrics",
        ["connector_id", "granularity", "timestamp"],
    )


def downgrade() -> None:
    op.drop_index("ix_connector_metrics_connector_granularity_ts", table_name="connector_metrics")
    op.drop_index("ix_connector_metrics_timestamp", table_name="connector_metrics")
    op.drop_index("ix_connector_metrics_connector_id", table_name="connector_metrics")
    op.drop_table("connector_metrics")
    op.drop_column("connectors", "traffic_reset_at")
    op.drop_column("connectors", "traffic_config")
