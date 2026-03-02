# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add partial unique index on users.email (non-empty only).

Revision ID: 014
Revises: 013
Create Date: 2026-03-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX ix_users_email_unique ON users (email) WHERE email != ''"
    )


def downgrade() -> None:
    op.drop_index("ix_users_email_unique", table_name="users")
