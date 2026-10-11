# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 038: the rule that picks one spelling per address, and the migration run against seeded rows."""

import importlib
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

migration = importlib.import_module("api.migrations.versions.038_lowercase_user_emails")
EmailRow = migration.EmailRow
normalize_emails = migration.normalize_emails

T1 = datetime(2026, 1, 1)
T2 = datetime(2026, 6, 1)
T3 = datetime(2026, 9, 1)


def test_already_lowercase_rows_are_untouched() -> None:
    assert normalize_emails([EmailRow("a", "jane@example.com", None, T1), EmailRow("b", "", None, T1)]) == {}


def test_mixed_case_is_lowered() -> None:
    assert normalize_emails([EmailRow("a", "Jane@Example.COM", None, T1)]) == {"a": "jane@example.com"}


def test_latest_login_keeps_the_address() -> None:
    rows = [
        EmailRow("old", "jane@example.com", T1, T1),
        EmailRow("recent", "Jane@example.com", T3, T2),
        EmailRow("never", "JANE@EXAMPLE.COM", None, T1),
    ]
    assert normalize_emails(rows) == {
        "recent": "jane@example.com",
        "old": "jane+1@example.com",
        "never": "jane+2@example.com",
    }


def test_without_logins_the_oldest_account_keeps_it() -> None:
    rows = [EmailRow("newer", "Jane@example.com", None, T2), EmailRow("older", "jane@example.com", None, T1)]
    assert normalize_emails(rows) == {"newer": "jane+1@example.com"}


def test_tags_skip_addresses_already_in_use() -> None:
    rows = [
        EmailRow("k", "jane@example.com", T2, T1),
        EmailRow("taken", "jane+1@example.com", None, T1),
        EmailRow("dup", "JANE@example.com", None, T1),
    ]
    assert normalize_emails(rows) == {"dup": "jane+2@example.com"}


def test_groups_are_independent() -> None:
    rows = [
        EmailRow("a1", "A@x.com", T2, T1),
        EmailRow("a2", "a@x.com", T1, T1),
        EmailRow("b1", "B@y.com", None, T1),
    ]
    assert normalize_emails(rows) == {"a1": "a@x.com", "a2": "a+1@x.com", "b1": "b@y.com"}



async def test_migration_lowercases_and_tags_duplicates(db_session: AsyncSession) -> None:
    rows = [
        ("u-recent", "recent", "Jane@Example.com", datetime(2026, 9, 1), datetime(2026, 2, 1)),
        ("u-old", "old", "jane@example.com", datetime(2026, 1, 1), datetime(2026, 1, 1)),
        ("u-never", "never", "JANE@EXAMPLE.COM", None, datetime(2026, 3, 1)),
        ("u-other", "other", "Bob@example.com", None, datetime(2026, 1, 1)),
    ]
    for user_id, username, email, last_login, created in rows:
        await db_session.execute(
            text(
                "INSERT INTO users (id, username, email, password_hash, role, is_active, last_login_at, created_at, updated_at) "
                "VALUES (:id, :username, :email, 'x', 'viewer', true, :last_login, :created, :created)"
            ),
            {"id": user_id, "username": username, "email": email, "last_login": last_login, "created": created},
        )
    await db_session.commit()

    await db_session.run_sync(lambda sync_session: migration.apply(sync_session.connection()))
    await db_session.commit()

    result = await db_session.execute(text("SELECT id, email FROM users WHERE id LIKE 'u-%' ORDER BY id"))
    assert dict(result.all()) == {
        "u-never": "jane+2@example.com",
        "u-old": "jane+1@example.com",
        "u-other": "bob@example.com",
        "u-recent": "jane@example.com",
    }
