# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The health check interval moves to the connector; the projects' never-read copies go.

* ``connectors.health_check_config``: ``{}`` for every existing connector, whose proxies keep being checked every
  ``health_check_interval`` of the install. ``{"interval_seconds": N}`` checks the connector's proxies every N
  seconds instead, so a large list of fixed exits can be checked less often than a residential pool.
* ``projects.health_check_interval`` and ``projects.health_check_timeout`` are dropped. The API stored them, but
  nothing ever read them: checks have always run on the install's settings.

The downgrade puts the project columns back at their old defaults (60 and 30), which the older release reads.

Revision ID: 055
Revises: 054
Create Date: 2026-11-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "055"
down_revision: str | None = "054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE connectors ADD COLUMN IF NOT EXISTS health_check_config JSON NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS health_check_interval")
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS health_check_timeout")


def downgrade() -> None:
    op.add_column("projects", sa.Column("health_check_interval", sa.Integer(), nullable=True, server_default="60"))
    op.add_column("projects", sa.Column("health_check_timeout", sa.Integer(), nullable=True, server_default="30"))
    op.drop_column("connectors", "health_check_config")
