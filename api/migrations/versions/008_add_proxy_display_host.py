# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add display_host column to proxies table.

This column stores the host to display in the UI, which may differ from
the actual host used for routing. For example, Oxylabs port-based proxies
store the discovered IP in display_host while host keeps the Oxylabs endpoint.

Revision ID: 008
Revises: 007
Create Date: 2026-02-21

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '008'
down_revision: str | None = '007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add display_host column to proxies table
    op.add_column(
        'proxies',
        sa.Column('display_host', sa.String(255), nullable=True)
    )


def downgrade() -> None:
    # Remove display_host column from proxies table
    op.drop_column('proxies', 'display_host')

