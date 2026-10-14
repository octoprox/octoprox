# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Two explicit location decisions per project, in place of the assurance level.

``projects.location_assurance`` named three levels (``observe``,
``verified``, ``confirmed``) that were nothing but combinations of two
decisions, and the names needed a glossary. The decisions are now stored as
themselves:

* ``location_contradicted``: the independent sources agree against the
  vendor's claim. ``evidence`` routes on where they put the exit,
  ``vendor`` routes on the claim anyway, ``refuse`` skips the proxy and
  replaces a pooled session.
* ``location_unknown``: nothing independent places the exit. ``vendor``
  routes on the vendor's word, ``refuse`` skips the proxy.

``observe`` was evidence and vendor, ``verified`` refuse and vendor,
``confirmed`` refuse and refuse. The ``vendor`` choice on a contradiction,
and refusing unknown exits without refusing contradicted ones, are new: no
level offered them.

Revision ID: 043
Revises: 042
Create Date: 2026-10-14
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "043"
down_revision: str | None = "042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("location_contradicted", sa.String(10), nullable=False, server_default="evidence"))
    op.add_column("projects", sa.Column("location_unknown", sa.String(10), nullable=False, server_default="vendor"))
    op.execute(
        sa.text(
            "UPDATE projects SET "
            "location_contradicted = CASE WHEN location_assurance IN ('verified', 'confirmed') THEN 'refuse' ELSE 'evidence' END, "
            "location_unknown = CASE WHEN location_assurance = 'confirmed' THEN 'refuse' ELSE 'vendor' END"
        )
    )
    op.drop_column("projects", "location_assurance")


def downgrade() -> None:
    # ``vendor`` on a contradiction had no level; it is folded into ``observe``,
    # which also shows the contradiction. Refusing unknown exits had none either
    # without refusing contradicted ones; it is folded into ``confirmed``, the
    # level that keeps that guarantee.
    op.add_column("projects", sa.Column("location_assurance", sa.String(10), nullable=False, server_default="observe"))
    op.execute(
        sa.text(
            "UPDATE projects SET location_assurance = CASE "
            "WHEN location_unknown = 'refuse' THEN 'confirmed' "
            "WHEN location_contradicted = 'refuse' THEN 'verified' "
            "ELSE 'observe' END"
        )
    )
    op.drop_column("projects", "location_unknown")
    op.drop_column("projects", "location_contradicted")
