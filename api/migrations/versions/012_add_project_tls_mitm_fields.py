# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add TLS MITM configuration fields to projects table.

Adds tls_mitm_mode, tls_mitm_engine, and tls_mitm_browser columns
to support configurable MITM interception modes.

Revision ID: 012
Revises: 011
Create Date: 2026-02-27

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '012'
down_revision: str | None = '011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'projects',
        sa.Column('tls_mitm_mode', sa.String(20), nullable=False, server_default='off'),
    )
    op.add_column(
        'projects',
        sa.Column('tls_mitm_engine', sa.String(20), nullable=True),
    )
    op.add_column(
        'projects',
        sa.Column('tls_mitm_browser', sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('projects', 'tls_mitm_browser')
    op.drop_column('projects', 'tls_mitm_engine')
    op.drop_column('projects', 'tls_mitm_mode')
