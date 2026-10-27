# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Remember which release's What's New each user last opened.

* ``users.last_seen_release``: null for every existing user, so each is shown
  the newest notes once. Added only where missing, like 050.

Revision ID: 051
Revises: 050
Create Date: 2026-10-27
"""
from collections.abc import Sequence

from alembic import op

revision: str = "051"
down_revision: str | None = "050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_release varchar(32)")


def downgrade() -> None:
    op.drop_column("users", "last_seen_release")
