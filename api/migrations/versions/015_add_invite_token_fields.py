# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add invite token fields to users table and make password_hash nullable.

Revision ID: 015
Revises: 014
Create Date: 2026-03-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("invite_token", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("invite_token_expires_at", sa.DateTime(), nullable=True))
    op.execute(
        "CREATE UNIQUE INDEX ix_users_invite_token ON users (invite_token) WHERE invite_token IS NOT NULL"
    )
    op.alter_column("users", "password_hash", existing_type=sa.String(255), nullable=True)


def downgrade() -> None:
    op.alter_column("users", "password_hash", existing_type=sa.String(255), nullable=False)
    op.drop_index("ix_users_invite_token", table_name="users")
    op.drop_column("users", "invite_token_expires_at")
    op.drop_column("users", "invite_token")
