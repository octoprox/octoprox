# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Let a project move a client session preflight refused to a new exit, instead of refusing it.

With it on, a refused ``-sessid-`` on a dynamic-sessions gateway moves to a new vendor session, which the
session's later requests keep, and one on a pooled slot moves to another slot, which sticky binds it to.

* ``projects.location_session_move``: false for every existing project, which keeps refusing as before.

Revision ID: 056
Revises: 055
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "056"
down_revision: str | None = "055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS location_session_move boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.drop_column("projects", "location_session_move")
