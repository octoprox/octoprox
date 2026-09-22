# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Store the latest observation's state on each connector exit IP.

``connector_exit_ips`` held only the count and the last resolved country. The
"Exit IPs" view shows one row per distinct exit with what its most recent
observation found (the proxy that held it, how it was seen, what the vendor
claimed, how the claim was judged), so those columns live on the row itself
rather than being joined out of the raw ``ip_observations`` history, which can
run to millions of rows. The flusher fills them from every observation of the
exit; only hand-outs move ``sightings``.

Rows that predate this migration carry empty state until the exit is next
observed; a re-attribution backfills all of them.

Revision ID: 026
Revises: 025
Create Date: 2026-09-22

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '026'
down_revision: str | None = '025'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "connector_exit_ips"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("proxy_id", sa.String(36), nullable=True))
    op.add_column(_TABLE, sa.Column("source", sa.String(20), nullable=True))
    op.add_column(_TABLE, sa.Column("claimed_country", sa.String(2), nullable=True))
    op.add_column(_TABLE, sa.Column("resolved_source", sa.String(20), nullable=True))
    op.add_column(
        _TABLE, sa.Column("conflict", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        _TABLE, sa.Column("disagreement", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    for name in ("disagreement", "conflict", "resolved_source", "claimed_country", "source", "proxy_id"):
        op.drop_column(_TABLE, name)
