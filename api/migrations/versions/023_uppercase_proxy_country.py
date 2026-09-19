# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Upper-case the exit country stored in proxy metadata.

Vendors report country codes in either case (Bright Data's IP list says
``nz``, Oxylabs' discovery endpoint says ``US``). Country routing compares
upper-cased values, but the raw value was persisted as received, so the
same country appeared in two spellings in the Proxies table and in exports.
Every write path now upper-cases; this brings existing rows in line.

Revision ID: 023
Revises: 022
Create Date: 2026-09-19

"""
from collections.abc import Sequence

from alembic import op

revision: str = '023'
down_revision: str | None = '022'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE proxies
        SET metadata = (metadata::jsonb || jsonb_build_object('country', upper(metadata->>'country')))::json
        WHERE metadata->>'country' IS NOT NULL
          AND metadata->>'country' <> upper(metadata->>'country')
        """
    )


def downgrade() -> None:
    # Case is not recoverable and lower-case values were never required; nothing to undo.
    pass
