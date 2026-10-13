# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""One location assurance level per project, in place of the policy and the preflight mode.

``projects.location_policy`` (``warn``, ``strict``) said what the verdict
written on a proxy row did to its eligibility, and
``projects.location_preflight`` (``off``, ``report``, ``retry``, ``reject``)
said what a preflight verdict did to the request. They answered the same
question twice, and differently: ``strict`` refused an open claim on a row,
``reject`` forwarded one at preflight, and a dynamic-sessions gateway, which
carries no row verdict, was reached by the second alone. One level,
``location_assurance``, now answers it for every kind of row:

* ``observe``: every verdict serves; it is recorded and shown.
* ``verified``: a contradicted claim is refused, an open one serves on the
  vendor's word.
* ``confirmed``: only a confirmed (or unjudged) claim serves.

``location_preflight`` becomes a switch: whether a session's exit is echoed
before its first request. What the verdict then does is the level's call;
whether a refused request moves to another proxy is the routing strategy's
(a bound sticky session cannot move) up to ``preflight_max_attempts``.

Existing projects keep what they had: ``strict`` is ``confirmed``; ``warn``
with ``retry`` or ``reject`` is ``verified``, since both already refused a
contradicted exit and forwarded an open one; the rest is ``observe``.
Preflight is on wherever it was not ``off``.

Revision ID: 041
Revises: 040
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "041"
down_revision: str | None = "040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("location_assurance", sa.String(10), nullable=False, server_default="observe"),
    )
    op.execute(
        sa.text(
            "UPDATE projects SET location_assurance = CASE "
            "WHEN location_policy = 'strict' THEN 'confirmed' "
            "WHEN location_preflight IN ('retry', 'reject') THEN 'verified' "
            "ELSE 'observe' END"
        )
    )
    op.drop_column("projects", "location_policy")
    # Postgres will not cast the old text default, so it goes first.
    op.alter_column("projects", "location_preflight", server_default=None)
    op.alter_column(
        "projects",
        "location_preflight",
        type_=sa.Boolean(),
        postgresql_using="location_preflight <> 'off'",
    )
    op.alter_column("projects", "location_preflight", server_default=sa.text("false"))


def downgrade() -> None:
    # ``verified`` had no row-level equivalent: its rows were ``warn`` and its
    # requests ``reject``, so that pair is restored. ``confirmed`` is ``strict``;
    # a strict project with preflight on was most often ``reject`` as well.
    # ``observe`` with preflight on is ``report``.
    op.alter_column("projects", "location_preflight", server_default=None)
    op.alter_column(
        "projects",
        "location_preflight",
        type_=sa.String(10),
        postgresql_using="CASE WHEN location_preflight THEN 'reject' ELSE 'off' END",
    )
    op.alter_column("projects", "location_preflight", server_default="off")
    op.execute(
        sa.text(
            "UPDATE projects SET location_preflight = 'report' "
            "WHERE location_preflight = 'reject' AND location_assurance = 'observe'"
        )
    )
    op.add_column(
        "projects",
        sa.Column("location_policy", sa.String(10), nullable=False, server_default="warn"),
    )
    op.execute(sa.text("UPDATE projects SET location_policy = 'strict' WHERE location_assurance = 'confirmed'"))
    op.drop_column("projects", "location_assurance")
