# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add pending_deletion column to connectors table.

Revision ID: 006
Revises: 005
Create Date: 2026-02-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '006'
down_revision: Union[str, None] = '005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add pending_deletion column to connectors table
    op.add_column(
        'connectors',
        sa.Column('pending_deletion', sa.Boolean(), nullable=False, server_default='false')
    )


def downgrade() -> None:
    # Remove pending_deletion column from connectors table
    op.drop_column('connectors', 'pending_deletion')

