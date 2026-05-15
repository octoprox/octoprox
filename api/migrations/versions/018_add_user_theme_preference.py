# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add theme_preference column to the users table.

Stores the user's chosen UI theme (e.g. 'light', 'dark', 'dracula', …) so
the preference follows them across devices. The allowed value set lives in
api/models/user.py (ALLOWED_THEMES) and web/src/themes/index.ts.

Revision ID: 018
Revises: 017
Create Date: 2026-05-15

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '018'
down_revision: str | None = '017'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'theme_preference',
            sa.String(32),
            nullable=False,
            server_default='light',
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'theme_preference')
