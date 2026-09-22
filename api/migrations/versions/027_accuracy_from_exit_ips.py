# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Compute provider accuracy from distinct exit IPs; drop the daily claim counts.

``location_claim_stats`` counted every observation that carried a vendor
claim, so re-attributing a pool after a database change, or any re-check of
an exit a proxy already had, inflated "claims checked" and "confirmed"
together and froze judgements made with older databases. Accuracy now reads
``connector_exit_ips``: each distinct exit counts once, with the verdict of
its latest observation, and a re-attribution simply rewrites those verdicts.

The accuracy query groups a connector's exits by when they were last seen, so
the table gets an index on (connector_id, last_seen).

Revision ID: 027
Revises: 026
Create Date: 2026-09-22

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '027'
down_revision: str | None = '026'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_connector_exit_ips_connector_last_seen",
        "connector_exit_ips",
        ["connector_id", "last_seen"],
    )
    op.drop_table("location_claim_stats")


def downgrade() -> None:
    op.create_table(
        "location_claim_stats",
        sa.Column(
            "connector_id",
            sa.String(36),
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("claimed_country", sa.String(2), primary_key=True, server_default=""),
        sa.Column("observed_country", sa.String(2), primary_key=True, server_default=""),
        sa.Column("observations", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.drop_index("ix_connector_exit_ips_connector_last_seen", table_name="connector_exit_ips")
