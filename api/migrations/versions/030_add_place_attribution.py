# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Record state and city claims and verdicts on observations and exit IPs.

Requests can now ask for a state and a city (``-st-``, ``-city-``) below the
country, and an operator can pin them on a static proxy. Each observation
keeps what was asked at those levels, where the databases placed the IP,
and a verdict per level with the polarity of ``conflict``: TRUE when every
database that places the IP there says somewhere else, FALSE when any
database agrees, NULL when nothing was claimed or no database answered. The
exit IP row carries the same for its latest observation, so state and city
accuracy read the one indexed table the country accuracy already does.

The echo endpoint can report the exit's state and city too, so the geo
settings gain the two JMESPaths that say where (``echo_state_path``,
``echo_city_path``); unset, only the country is read from it.

Rows that predate this migration carry NULLs: no claim, no verdict.

Revision ID: 030
Revises: 029
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '030'
down_revision: str | None = '029'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("ip_observations", "connector_exit_ips")
_COLUMNS = (
    ("claimed_state", sa.String(8)),
    ("claimed_city", sa.String(120)),
    ("resolved_state", sa.String(8)),
    ("resolved_city", sa.String(120)),
    ("state_conflict", sa.Boolean()),
    ("city_conflict", sa.Boolean()),
)


_SETTINGS_COLUMNS = ("echo_state_path", "echo_city_path")


def upgrade() -> None:
    for table in _TABLES:
        for name, kind in _COLUMNS:
            op.add_column(table, sa.Column(name, kind, nullable=True))
    for name in _SETTINGS_COLUMNS:
        op.add_column("geo_settings", sa.Column(name, sa.String(255), nullable=True))


def downgrade() -> None:
    for name in reversed(_SETTINGS_COLUMNS):
        op.drop_column("geo_settings", name)
    for table in _TABLES:
        for name, _kind in reversed(_COLUMNS):
            op.drop_column(table, name)
