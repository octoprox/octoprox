# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tell service accounts from people: an account a program uses through API tokens, never signing in.

* ``users.kind``: ``person`` for every existing user. Service accounts are only those an admin creates as such.

Revision ID: 059
Revises: 058
Create Date: 2026-11-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "059"
down_revision: str | None = "058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS kind varchar(16) NOT NULL DEFAULT 'person'")


def downgrade() -> None:
    op.drop_column("users", "kind")
