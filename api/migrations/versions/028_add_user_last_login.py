# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Record when a user last obtained a token.

``users.last_login_at`` is set whenever a token is issued: on a password
login and when an invited user sets their password. It is nullable because
existing users and users who have not accepted their invite have never
logged in. Tokens are stateless, so this is "last login", not "last seen".

Revision ID: 028
Revises: 027
Create Date: 2026-09-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '028'
down_revision: str | None = '027'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
