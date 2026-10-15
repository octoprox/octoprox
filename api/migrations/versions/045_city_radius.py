# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""How far from a claimed city an answer may be and still confirm it.

Cities were judged by name alone, so an exit a database placed in a
neighbouring municipality (DB-IP's Diadema for a São Paulo claim, 15 km from
the centre) contradicted the claim. With a GeoNames file loaded the claimed
city has a point, and an answer within the radius confirms it.

* ``geo_settings.default_city_radius_km``: the install default, 25 km.
* ``projects.location_city_radius_km``: a project's own radius; null
  inherits the default, 0 compares names only.

The GeoNames file itself is a row of ``geo_databases`` like any IP database
(format ``geonames``, kind ``places``), so no table changes for it.

Revision ID: 045
Revises: 044
Create Date: 2026-10-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "045"
down_revision: str | None = "044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("geo_settings", sa.Column("default_city_radius_km", sa.Float(), nullable=False, server_default="25"))
    op.add_column("projects", sa.Column("location_city_radius_km", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "location_city_radius_km")
    op.drop_column("geo_settings", "default_city_radius_km")
