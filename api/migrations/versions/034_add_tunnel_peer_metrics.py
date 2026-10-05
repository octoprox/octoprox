# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tunnel device metrics history, and where a device was last seen.

* ``tunnel_peer_metrics``: the fourth metrics history table, per tunnel
  device, for every tunnel protocol. Until now a device's counters were
  what ``wg show`` reported on the instance carrying its session: lost
  when that instance restarted, invisible from the others except through
  a short-lived Redis key, and without history. Requests through the
  tunnel are now metered on the proxy path keyed by the device, flushed
  and compacted like the proxy, project and connector history. The two
  name-resolution signals the transparent listener counts per device
  (connections relayed by address, encrypted-DNS connections closed) ride
  the same rows. ``project_id`` is copied from the device at flush time so
  retention and the project cascade apply; the device tables differ per
  protocol, so there is no foreign key to the device and each protocol's
  repository deletes a device's rows with it.
* ``wireguard_peers.last_handshake_at`` / ``last_endpoint``: when a
  device last handshaked and from where, persisted by the carrying
  instance so the answer survives its restart.

Revision ID: 034
Revises: 033
Create Date: 2026-10-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '034'
down_revision: str | None = '033'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tunnel_peer_metrics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("peer_id", sa.String(length=36), nullable=False),
        sa.Column("protocol", sa.String(length=16), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_latency_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("bytes_sent", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("bytes_received", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("by_address", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("encrypted_dns_blocked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("granularity", sa.Integer(), nullable=False, server_default="60"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tunnel_peer_metrics_peer_id", "tunnel_peer_metrics", ["peer_id"])
    op.create_index("ix_tunnel_peer_metrics_project_id", "tunnel_peer_metrics", ["project_id"])
    op.create_index("ix_tunnel_peer_metrics_timestamp", "tunnel_peer_metrics", ["timestamp"])
    op.create_index(
        "ix_tunnel_peer_metrics_peer_granularity_ts",
        "tunnel_peer_metrics",
        ["peer_id", "granularity", "timestamp"],
    )

    op.add_column("wireguard_peers", sa.Column("last_handshake_at", sa.DateTime(), nullable=True))
    op.add_column("wireguard_peers", sa.Column("last_endpoint", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("wireguard_peers", "last_endpoint")
    op.drop_column("wireguard_peers", "last_handshake_at")
    op.drop_index("ix_tunnel_peer_metrics_peer_granularity_ts", table_name="tunnel_peer_metrics")
    op.drop_index("ix_tunnel_peer_metrics_timestamp", table_name="tunnel_peer_metrics")
    op.drop_index("ix_tunnel_peer_metrics_project_id", table_name="tunnel_peer_metrics")
    op.drop_index("ix_tunnel_peer_metrics_peer_id", table_name="tunnel_peer_metrics")
    op.drop_table("tunnel_peer_metrics")
