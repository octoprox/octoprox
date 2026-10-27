# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Let a project tell its clients which exit IP served each request.

With it on, the gateway names the exit IP, where that IP came from, when it
was seen and the proxy row in ``X-Octoprox-*`` headers: on the 200 that
answers a CONNECT, and on a plain HTTP response.

* ``projects.exit_headers``: false for every existing project. Added only
  where missing: the way back to 5.x sets the version to 047 and keeps it.

Revision ID: 050
Revises: 049
Create Date: 2026-10-27
"""
from collections.abc import Sequence

from alembic import op

revision: str = "050"
down_revision: str | None = "049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS exit_headers boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.drop_column("projects", "exit_headers")
