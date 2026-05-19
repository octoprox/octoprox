# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add version columns to projects, credentials, connectors, and proxies.

These integer counters are bumped on every mutation. Cross-instance cache
reload handlers compare the message version against their cached value to
drop stale events when running multi-instance (Phase A2 onward).

Revision ID: 019
Revises: 018
Create Date: 2026-05-19

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '019'
down_revision: str | None = '018'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLES = ("projects", "credentials", "connectors", "proxies")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "version",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, "version")
