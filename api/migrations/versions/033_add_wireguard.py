# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add WireGuard: the server identity row and the per-project device peers.

* ``wireguard_settings``: one row (id 1) holding the install's WireGuard key
  pair and what every device config needs to reach it: the public endpoint,
  the tunnel subnet and the client-side knobs. One key pair for the whole
  install, so a device config works against whichever instance terminates
  the tunnel.
* ``wireguard_peers``: a device that may connect. Its tunnel address is the
  credential: WireGuard's cryptokey routing only lets a peer send from the
  address it was given, so the address alone identifies the project (and the
  session and location the device's traffic is routed with). The private key
  is stored when Octoprox generated it, so the config can be shown again;
  null when the operator supplied their own public key.

Revision ID: 033
Revises: 032
Create Date: 2026-10-03
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '033'
down_revision: str | None = '032'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "wireguard_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("private_key", sa.String(44), nullable=False),
        sa.Column("public_key", sa.String(44), nullable=False),
        sa.Column("endpoint_host", sa.String(255), nullable=False, server_default=""),
        sa.Column("endpoint_port", sa.Integer(), nullable=False, server_default="51820"),
        sa.Column("subnet", sa.String(43), nullable=False, server_default="10.66.0.0/16"),
        sa.Column("persistent_keepalive", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("client_mtu", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_wireguard_settings_single_row"),
    )

    op.create_table(
        "wireguard_peers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("public_key", sa.String(44), nullable=False, unique=True),
        sa.Column("private_key", sa.String(44), nullable=True),
        sa.Column("preshared_key", sa.String(44), nullable=True),
        sa.Column("address", sa.String(45), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column("country", sa.String(2), nullable=True),
        sa.Column("state", sa.String(8), nullable=True),
        sa.Column("city", sa.String(120), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_wireguard_peers_project_id", "wireguard_peers", ["project_id"])
    op.create_index(
        "ix_wireguard_peers_project_name_unique",
        "wireguard_peers",
        ["project_id", sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_wireguard_peers_project_name_unique", table_name="wireguard_peers")
    op.drop_index("ix_wireguard_peers_project_id", table_name="wireguard_peers")
    op.drop_table("wireguard_peers")
    op.drop_table("wireguard_settings")
