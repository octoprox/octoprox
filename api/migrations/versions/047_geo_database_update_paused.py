# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Pause a database's download schedule without losing it.

A scheduled database could only stop updating by being disabled, which also
unloads its file, or by clearing its URL or interval, which loses them. A
paused schedule keeps the file loaded and the URL, credentials and interval
as they are; the updater skips it, and a manual refresh still downloads.

* ``geo_databases.update_paused``: false for every existing row.

Revision ID: 047
Revises: 046
Create Date: 2026-10-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "047"
down_revision: str | None = "046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("geo_databases", sa.Column("update_paused", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("geo_databases", "update_paused")
