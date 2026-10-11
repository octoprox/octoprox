# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Lower-case user emails, so an address has exactly one row.

Sign-in through a provider (docs/sso.md) matches the provider's verified
email against the user table by equality, and every write path now stores
the address lowercase. Rows written before that may differ only in case,
which the unique index did not stop. Where lowercasing would make two rows
equal, the one that signed in most recently (then the oldest) keeps the
address and the rest get ``local+1@domain``, ``local+2@domain``, ... so
nothing is lost and an admin can tell them apart. The rule is
``normalize_emails`` below: pure, so it is tested without a database.

Revision ID: 038
Revises: 037
Create Date: 2026-10-12
"""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "038"
down_revision: str | None = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


@dataclass(frozen=True)
class EmailRow:
    id: str
    email: str
    # Who keeps the address when several rows share it: the most recently
    # signed-in, then the oldest account. ``None`` sorts after any time.
    last_login_at: datetime | None
    created_at: datetime | None


def _tagged(email: str, n: int) -> str:
    local, _, domain = email.partition("@")
    return f"{local}+{n}@{domain}"


def normalize_emails(rows: list[EmailRow]) -> dict[str, str]:
    """New email for every row whose stored value has to change, by row id.

    Every email is lowercased. Where that makes several rows equal, the row
    with the latest login (then the earliest creation) keeps the address and
    the others get ``local+1@domain``, ``local+2@domain``, ... with numbers
    that collide with no address in the set. Rows whose value does not
    change are absent from the result.
    """
    by_lower: dict[str, list[EmailRow]] = {}
    for row in rows:
        if row.email:
            by_lower.setdefault(row.email.strip().lower(), []).append(row)

    taken = set(by_lower)
    changes: dict[str, str] = {}
    far_future = datetime.max
    for lowered, group in by_lower.items():
        group.sort(
            key=lambda r: (
                -r.last_login_at.timestamp() if r.last_login_at else float("inf"),
                r.created_at or far_future,
            )
        )
        keeper, *rest = group
        if keeper.email != lowered:
            changes[keeper.id] = lowered
        n = 1
        for row in rest:
            candidate = _tagged(lowered, n)
            while candidate in taken:
                n += 1
                candidate = _tagged(lowered, n)
            taken.add(candidate)
            changes[row.id] = candidate
            n += 1
    return changes


def upgrade() -> None:
    apply(op.get_bind())


def apply(conn: sa.engine.Connection) -> None:
    """The whole migration, on any connection, so a test can run it against seeded rows."""
    rows = conn.execute(
        sa.text("SELECT id, email, last_login_at, created_at FROM users WHERE email <> ''")
    ).all()
    changes = normalize_emails([EmailRow(r.id, r.email, r.last_login_at, r.created_at) for r in rows])
    if not changes:
        return
    # The unique index is checked per statement, so the rows that move to a
    # tagged address go first; only then can a keeper take the plain one.
    update = sa.text("UPDATE users SET email = :email WHERE id = :id")
    ordered = sorted(changes.items(), key=lambda item: "+" not in item[1].partition("@")[0])
    for user_id, email in ordered:
        conn.execute(update, {"id": user_id, "email": email})


def downgrade() -> None:
    # The original case is not recoverable, and lowercase addresses were always valid.
    pass
