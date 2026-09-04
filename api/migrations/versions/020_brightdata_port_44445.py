# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Move existing BrightData proxies from port 33335 to 44445.

BrightData asked customers to switch the brd.superproxy.io endpoint port
from 33335 to 44445. New proxies pick up the new port from
api/providers/brightdata.py (BRIGHTDATA_PORT); this migration rewrites
rows that were created against the old port so existing connectors keep
working without being recreated.

The version counter is bumped so peer instances treat the change like any
other proxy mutation and reload their cached copy.

Revision ID: 020
Revises: 019
Create Date: 2026-09-04

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '020'
down_revision: str | None = '019'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


BRIGHTDATA_HOST = "brd.superproxy.io"
OLD_PORT = 33335
NEW_PORT = 44445


def _rewrite_port(old_port: int, new_port: int) -> None:
    op.execute(
        sa.text(
            "UPDATE proxies "
            "SET port = :new_port, version = version + 1, updated_at = NOW() "
            "WHERE host = :host AND port = :old_port"
        ).bindparams(host=BRIGHTDATA_HOST, old_port=old_port, new_port=new_port)
    )


def upgrade() -> None:
    _rewrite_port(OLD_PORT, NEW_PORT)


def downgrade() -> None:
    _rewrite_port(NEW_PORT, OLD_PORT)
