# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Make credential and connector names unique per project.

Names are how connectors and the UI refer to credentials, and how operators
tell connectors apart, yet nothing stopped two rows in one project from
sharing a name. The unique index is the only check that holds across a
cluster: instances accept writes independently and learn about each other's
changes through Redis a moment later, so an application-level lookup could
let two simultaneous creates through.

Uniqueness ignores case (``lower(name)``). Existing duplicates are renamed
first, oldest row keeps its name and later ones get a `` (2)``, `` (3)``
suffix, so the index can be created on databases that already contain
collisions. Should a suffixed name itself collide, the index creation fails
and the operator resolves it by hand rather than the migration guessing.

Revision ID: 022
Revises: 021
Create Date: 2026-09-08

"""
from collections.abc import Sequence

from alembic import op

revision: str = '022'
down_revision: str | None = '021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("credentials", "connectors")


def _dedupe(table: str) -> str:
    return f"""
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (PARTITION BY project_id, lower(name) ORDER BY created_at, id) AS rn
            FROM {table}
        )
        UPDATE {table} AS t
        SET name = t.name || ' (' || r.rn || ')'
        FROM ranked AS r
        WHERE t.id = r.id AND r.rn > 1
    """


def upgrade() -> None:
    for table in TABLES:
        op.execute(_dedupe(table))
        op.execute(
            f"CREATE UNIQUE INDEX ix_{table}_project_name_unique ON {table} (project_id, lower(name))"
        )


def downgrade() -> None:
    # Renames applied on upgrade are not reverted.
    for table in TABLES:
        op.drop_index(f"ix_{table}_project_name_unique", table_name=table)
