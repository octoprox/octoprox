# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Drop the source order: the routing value has one fixed precedence.

``geo_settings.default_sources`` and ``projects.location_sources`` ranked
the three kinds of evidence (databases, vendor, echo endpoint) for a
proxy's routing value. The order never changed what counted as evidence
when judging the vendor, only which answer became the value, and the one
real choice it offered, ranking the vendor above the databases, is what the
location assurance level now expresses: trust in the vendor is a level's
business, never the value's. The value is now always the databases (in
their own priority), then the echo endpoint, then the vendor filling what
nothing independent answered. A project or install that ranked the vendor
first sees its contradicted rows routed on the independent answer from now
on, which is what the default order did for everyone else; an install that
left the echo out sees an exit only the echo can place take the echo's
country.

Revision ID: 042
Revises: 041
Create Date: 2026-10-14
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "042"
down_revision: str | None = "041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_ORDER = '["database", "vendor", "endpoint"]'


def upgrade() -> None:
    op.drop_column("projects", "location_sources")
    op.drop_column("geo_settings", "default_sources")


def downgrade() -> None:
    op.add_column("geo_settings", sa.Column("default_sources", sa.JSON(), nullable=True))
    op.execute(sa.text(f"UPDATE geo_settings SET default_sources = '{DEFAULT_ORDER}'::json"))
    op.alter_column("geo_settings", "default_sources", nullable=False)
    op.add_column("projects", sa.Column("location_sources", sa.JSON(), nullable=True))
