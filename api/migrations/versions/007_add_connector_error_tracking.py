# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add error tracking columns to connectors table.

Revision ID: 007
Revises: 006
Create Date: 2026-02-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '007'
down_revision: Union[str, None] = '006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add error tracking columns to connectors table
    op.add_column(
        'connectors',
        sa.Column('last_error', sa.String(1024), nullable=True)
    )
    op.add_column(
        'connectors',
        sa.Column('last_error_at', sa.DateTime(), nullable=True)
    )
    op.add_column(
        'connectors',
        sa.Column('consecutive_errors', sa.Integer(), nullable=False, server_default='0')
    )


def downgrade() -> None:
    # Remove error tracking columns from connectors table
    op.drop_column('connectors', 'consecutive_errors')
    op.drop_column('connectors', 'last_error_at')
    op.drop_column('connectors', 'last_error')

