# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add granularity column to metrics tables and retention config to projects.

The granularity column tracks the time-bucket width (in seconds) of each
metrics row: 60 = raw 1-minute data, 3600 = hourly, 21600 = 6-hourly,
86400 = daily.  The metrics compactor uses this to progressively reduce
granularity as data ages while preserving aggregate correctness.

metrics_retention_days on the projects table controls how long historical
metrics are kept before deletion (0 = keep forever).

Revision ID: 017
Revises: 016
Create Date: 2026-04-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '017'
down_revision: str | None = '016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add granularity column to proxy_metrics
    op.add_column(
        'proxy_metrics',
        sa.Column('granularity', sa.Integer(), nullable=False, server_default='60'),
    )

    # Add granularity column to project_metrics
    op.add_column(
        'project_metrics',
        sa.Column('granularity', sa.Integer(), nullable=False, server_default='60'),
    )

    # Add metrics_retention_days to projects
    op.add_column(
        'projects',
        sa.Column('metrics_retention_days', sa.Integer(), nullable=False, server_default='90'),
    )

    # Composite indexes for efficient queries filtering by granularity
    op.create_index(
        'ix_proxy_metrics_proxy_granularity_ts',
        'proxy_metrics',
        ['proxy_id', 'granularity', 'timestamp'],
    )
    op.create_index(
        'ix_project_metrics_project_granularity_ts',
        'project_metrics',
        ['project_id', 'granularity', 'timestamp'],
    )


def downgrade() -> None:
    op.drop_index('ix_project_metrics_project_granularity_ts', table_name='project_metrics')
    op.drop_index('ix_proxy_metrics_proxy_granularity_ts', table_name='proxy_metrics')
    op.drop_column('projects', 'metrics_retention_days')
    op.drop_column('project_metrics', 'granularity')
    op.drop_column('proxy_metrics', 'granularity')
