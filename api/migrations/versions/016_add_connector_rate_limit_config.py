# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add rate_limit_config JSON column to connectors table.

This column stores per-proxy rate limiting configuration (max requests
per time window and quarantine duration range) that controls request
throttling through a connector's proxies.

Revision ID: 016
Revises: 015
Create Date: 2026-03-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '016'
down_revision: str | None = '015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'connectors',
        sa.Column('rate_limit_config', sa.JSON(), nullable=False, server_default='{}'),
    )


def downgrade() -> None:
    op.drop_column('connectors', 'rate_limit_config')
