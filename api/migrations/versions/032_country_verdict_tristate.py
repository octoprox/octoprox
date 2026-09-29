# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Store the country verdict like the state and city verdicts: one nullable flag.

``conflict`` (contradicted) and ``disagreement`` (independent sources
disagreed) could not say when a claim had no independent evidence at all:
that looked exactly like a confirmed claim, and provider accuracy counted it
as one. An IP no database knows may well be somewhere else; nobody can tell.
The country now gets the same tri-state as the levels below it:
``country_conflict`` TRUE contradicted, FALSE confirmed, NULL no verdict
(nothing claimed, nothing independent answered, or the independent sources
disagreed under ``consensus``).

Existing rows are converted conservatively: a row is left confirmed only
when it had a claim, was not contradicted, the sources did not disagree,
and the resolution came from an independent source. A claim the policy let
the vendor answer, or one no source answered, becomes NULL, since the
evidence behind it cannot be recovered. ``disagreement`` is then dropped.

Revision ID: 032
Revises: 031
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '032'
down_revision: str | None = '031'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("ip_observations", "connector_exit_ips")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table, "conflict", new_column_name="country_conflict", existing_type=sa.Boolean(), nullable=True, server_default=None
        )
        op.execute(
            f"""
            UPDATE {table}
            SET country_conflict = NULL
            WHERE claimed_country IS NULL
               OR disagreement
               OR (country_conflict = FALSE AND (resolved_source IS NULL OR resolved_source = 'vendor'))
            """
        )
        op.drop_column(table, "disagreement")


def downgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("disagreement", sa.Boolean(), nullable=False, server_default=sa.false()))
        op.execute(f"UPDATE {table} SET country_conflict = FALSE WHERE country_conflict IS NULL")
        op.alter_column(
            table, "country_conflict", new_column_name="conflict", existing_type=sa.Boolean(), nullable=False, server_default=sa.false()
        )
