# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add provider descriptor storage and audit log.

Admin-authored provider descriptors (the provider SDK's declarative vendor
definitions) live in ``provider_descriptors``; every create/update/delete is
recorded in ``provider_audit_log`` together with the vendor hosts the
descriptor sends credentials to.

Existing Oxylabs and BrightData credentials, connectors and proxies need no
data changes: the built-in descriptors keep the same ``type`` ids
(``oxylabs``, ``brightdata``) and the same credential/connector config keys
as the code they replace, so they are adopted in place.

Revision ID: 021
Revises: 020
Create Date: 2026-09-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '021'
down_revision: str | None = '020'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_descriptors",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "provider_audit_log",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("egress_hosts", sa.JSON(), nullable=False),
        sa.Column("hosts_changed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("spec", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_provider_audit_log_provider_id", "provider_audit_log", ["provider_id"])
    op.create_index("ix_provider_audit_log_created_at", "provider_audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_provider_audit_log_created_at", table_name="provider_audit_log")
    op.drop_index("ix_provider_audit_log_provider_id", table_name="provider_audit_log")
    op.drop_table("provider_audit_log")
    op.drop_table("provider_descriptors")
