# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Have every unflagged proxy judged again, now that the verdict is stored.

Attribution used to keep one bit per proxy, ``metadata.location_conflict``,
true for a contradicted vendor claim and false for anything else. A strict
project now wants clear evidence: it routes to a proxy whose claim is
confirmed, not to one whose claim is open (the databases disagree, or none
knows the IP), and that distinction is written as ``metadata.location_verdict``.
Rows judged before this release carry the bit alone, and a false bit may
hide an open claim. Dropping the bit from those rows marks them unjudged,
so the next health check (one per minute) or discovery refresh resolves
them again and writes the verdict; a contradicted row keeps its bit and
stays flagged meanwhile. Until re-judged, an unjudged row is eligible under
``strict``, exactly as a slot provisioned seconds ago is.

Revision ID: 040
Revises: 039
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "040"
down_revision: str | None = "039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE proxies SET metadata = (metadata::jsonb - 'location_conflict' - 'location_verdict')::json "
            "WHERE metadata->>'location_conflict' = 'false'"
        )
    )


def downgrade() -> None:
    # The verdict key is unknown to the previous version and ignored by it; the
    # dropped bits come back with the next health check there as well.
    op.execute(
        sa.text(
            "UPDATE proxies SET metadata = (metadata::jsonb - 'location_verdict')::json "
            "WHERE metadata->>'location_verdict' IS NOT NULL"
        )
    )
