# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Retire the ``off`` location policy; ``warn`` is what it always did.

``projects.location_policy`` offered ``off``, ``warn`` and ``strict``, but
nothing ever branched on ``warn``: the contradiction flag was written on
the proxy row and shown in the UI under both, and routing used the
resolved country under both. Only ``strict`` changes anything (flagged
proxies leave the eligible set). Two names for one behaviour, so the
value is folded into ``warn`` and the default becomes ``warn``.

Revision ID: 039
Revises: 038
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "039"
down_revision: str | None = "038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE projects SET location_policy = 'warn' WHERE location_policy = 'off'"))
    op.alter_column("projects", "location_policy", server_default="warn")


def downgrade() -> None:
    # Rows stay ``warn``: it is a valid value for the previous version and
    # behaves exactly as ``off`` did there.
    op.alter_column("projects", "location_policy", server_default="off")
