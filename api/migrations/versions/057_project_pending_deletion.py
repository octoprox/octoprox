# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Let a deleted project wait for its cloud instances to be terminated before it goes.

A delete marks the project pending deletion, removes each connector as the connector delete does, and the scale job
deletes the project with its last cloud connector, so no instance outlives its project's rows.

* ``projects.pending_deletion``: false for every existing project.

Revision ID: 057
Revises: 056
Create Date: 2026-11-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "057"
down_revision: str | None = "056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS pending_deletion boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.drop_column("projects", "pending_deletion")
