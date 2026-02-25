# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Add ON DELETE CASCADE to proxies.connector_id foreign key.

This ensures that when a connector is deleted, all its proxies are
automatically deleted by the database, preventing foreign key violations.

Revision ID: 009
Revises: 008
Create Date: 2026-02-21

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '009'
down_revision: str | None = '008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop the existing foreign key constraint
    op.drop_constraint('proxies_connector_id_fkey', 'proxies', type_='foreignkey')

    # Re-create with ON DELETE CASCADE
    op.create_foreign_key(
        'proxies_connector_id_fkey',
        'proxies',
        'connectors',
        ['connector_id'],
        ['id'],
        ondelete='CASCADE'
    )


def downgrade() -> None:
    # Drop the CASCADE foreign key constraint
    op.drop_constraint('proxies_connector_id_fkey', 'proxies', type_='foreignkey')

    # Re-create without ON DELETE CASCADE
    op.create_foreign_key(
        'proxies_connector_id_fkey',
        'proxies',
        'connectors',
        ['connector_id'],
        ['id']
    )

