# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add bytes_sent and bytes_received tracking to proxy_metrics.

Revision ID: 004
Revises: 003
Create Date: 2024-01-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '004'
down_revision: Union[str, None] = '003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add bytes_sent and bytes_received columns to proxy_metrics table
    op.add_column(
        'proxy_metrics',
        sa.Column('bytes_sent', sa.BigInteger(), nullable=False, server_default='0')
    )
    op.add_column(
        'proxy_metrics',
        sa.Column('bytes_received', sa.BigInteger(), nullable=False, server_default='0')
    )


def downgrade() -> None:
    # Remove bytes_sent and bytes_received columns from proxy_metrics table
    op.drop_column('proxy_metrics', 'bytes_received')
    op.drop_column('proxy_metrics', 'bytes_sent')

