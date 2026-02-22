# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add ON DELETE CASCADE to project-related foreign keys.

This ensures that when a project is deleted, all its credentials and
connectors are automatically deleted by the database.

Foreign keys updated:
- credentials.project_id -> projects.id
- connectors.project_id -> projects.id

Revision ID: 010
Revises: 009
Create Date: 2026-02-21

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '010'
down_revision: str | None = '009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # credentials.project_id -> projects.id
    op.drop_constraint('credentials_project_id_fkey', 'credentials', type_='foreignkey')
    op.create_foreign_key(
        'credentials_project_id_fkey',
        'credentials',
        'projects',
        ['project_id'],
        ['id'],
        ondelete='CASCADE'
    )

    # connectors.project_id -> projects.id
    op.drop_constraint('connectors_project_id_fkey', 'connectors', type_='foreignkey')
    op.create_foreign_key(
        'connectors_project_id_fkey',
        'connectors',
        'projects',
        ['project_id'],
        ['id'],
        ondelete='CASCADE'
    )


def downgrade() -> None:
    # connectors.project_id -> projects.id (remove CASCADE)
    op.drop_constraint('connectors_project_id_fkey', 'connectors', type_='foreignkey')
    op.create_foreign_key(
        'connectors_project_id_fkey',
        'connectors',
        'projects',
        ['project_id'],
        ['id']
    )

    # credentials.project_id -> projects.id (remove CASCADE)
    op.drop_constraint('credentials_project_id_fkey', 'credentials', type_='foreignkey')
    op.create_foreign_key(
        'credentials_project_id_fkey',
        'credentials',
        'projects',
        ['project_id'],
        ['id']
    )

