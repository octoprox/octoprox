# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add OpenVPN: the install's CA and server identity, and the per-project devices.

* ``openvpn_settings``: one row (id 1) holding the install's private CA,
  the server certificate it signed, the tls-crypt key, and what every
  device profile needs to reach the endpoint: host, port, transport, the
  tunnel subnet and the client-side knobs. One identity for the whole
  install, so a profile works against whichever instance terminates the
  endpoint.
* ``openvpn_peers``: a device that may connect. Its certificate (issued by
  the CA, the device id as common name) and key are kept so the profile can
  be shown again; the serial tells the current certificate from a rotated
  one at connect time. The tunnel address is the credential, as for
  WireGuard: the daemon only accepts packets a device sends from the
  address it was pushed, and that address names this row.

Revision ID: 035
Revises: 034
Create Date: 2026-10-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '035'
down_revision: str | None = '034'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "openvpn_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ca_cert", sa.Text(), nullable=False),
        sa.Column("ca_key", sa.Text(), nullable=False),
        sa.Column("server_cert", sa.Text(), nullable=False),
        sa.Column("server_key", sa.Text(), nullable=False),
        sa.Column("tls_crypt_key", sa.Text(), nullable=False),
        sa.Column("endpoint_host", sa.String(255), nullable=False, server_default=""),
        sa.Column("endpoint_port", sa.Integer(), nullable=False, server_default="1194"),
        sa.Column("protocol", sa.String(3), nullable=False, server_default="udp"),
        sa.Column("subnet", sa.String(43), nullable=False, server_default="10.67.0.0/16"),
        sa.Column("keepalive_interval", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("keepalive_timeout", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("client_mtu", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_openvpn_settings_single_row"),
    )

    op.create_table(
        "openvpn_peers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("certificate", sa.Text(), nullable=False),
        sa.Column("private_key", sa.Text(), nullable=False),
        sa.Column("serial", sa.String(64), nullable=False, unique=True),
        sa.Column("certificate_expires_at", sa.DateTime(), nullable=False),
        sa.Column("address", sa.String(45), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column("country", sa.String(2), nullable=True),
        sa.Column("state", sa.String(8), nullable=True),
        sa.Column("city", sa.String(120), nullable=True),
        sa.Column("last_connected_at", sa.DateTime(), nullable=True),
        sa.Column("last_endpoint", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_openvpn_peers_project_id", "openvpn_peers", ["project_id"])
    op.create_index(
        "ix_openvpn_peers_project_name_unique",
        "openvpn_peers",
        ["project_id", sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_openvpn_peers_project_name_unique", table_name="openvpn_peers")
    op.drop_index("ix_openvpn_peers_project_id", table_name="openvpn_peers")
    op.drop_table("openvpn_peers")
    op.drop_table("openvpn_settings")
