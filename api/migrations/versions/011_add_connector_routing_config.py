# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add routing_config JSON column to connectors table.

This column stores domain filtering rules (whitelist/blacklist) that control
which target domains can be routed through a connector's proxies.

Revision ID: 011
Revises: 010
Create Date: 2026-02-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '011'
down_revision: str | None = '010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'connectors',
        sa.Column('routing_config', sa.JSON(), nullable=False, server_default='{}'),
    )


def downgrade() -> None:
    op.drop_column('connectors', 'routing_config')
