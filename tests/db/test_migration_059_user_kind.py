# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 059 adds the user kind and leaves every existing user a person."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, make_url, text

from api.db.migrations import alembic_config


@pytest.fixture
def database(migrated_database: str) -> Iterator[str]:
    """The sync URL of a database of its own at revision 058, dropped after the test."""
    admin = create_engine(migrated_database, isolation_level="AUTOCOMMIT")
    name = f"migration_059_{uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(migrated_database).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "058")
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


def _add_user(
    engine: Engine, user_id: str, *, password: str | None = None, email: str = "", invite: str | None = None,
    tokens: int = 0, identity: bool = False,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, email, password_hash, role, is_active, invite_token, "
                "invite_token_expires_at, created_at, updated_at) VALUES (:id, :id, :email, :password, 'viewer', true, "
                ":invite, CASE WHEN CAST(:invite AS text) IS NULL THEN NULL ELSE now() END, now(), now())"
            ),
            {"id": user_id, "email": email, "password": password, "invite": invite},
        )
        for n in range(tokens):
            conn.execute(
                text(
                    "INSERT INTO api_tokens (id, user_id, name, token_hash, token_prefix, created_at) "
                    "VALUES (:id, :user_id, 'bot', :hash, 'octo_abcdefg', now())"
                ),
                {"id": f"{user_id}-t{n}", "user_id": user_id, "hash": f"{user_id}-hash-{n}"},
            )
        if identity:
            conn.execute(
                text(
                    "INSERT INTO auth_identities (id, user_id, provider, subject, email, created_at) "
                    "VALUES (:id, :id, 'github', :id, '', now())"
                ),
                {"id": user_id},
            )


def _users(engine: Engine) -> dict[str, tuple[str, str | None, bool]]:
    """Each user's kind, invite link and whether the link's expiry is still set, by id."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, kind, invite_token, invite_token_expires_at FROM users"))
        return {row.id: (row.kind, row.invite_token, row.invite_token_expires_at is not None) for row in rows}


def test_every_existing_user_stays_a_person(database: str) -> None:
    engine = create_engine(database)
    try:
        # A token-only account (invited, never accepted, issued a token) included: only an admin makes a service
        # account, by creating one as such.
        _add_user(engine, "bot", invite="bot-link", tokens=2)
        _add_user(engine, "bot-no-link", tokens=1)
        _add_user(engine, "invited", invite="invited-link")
        _add_user(engine, "with-password", password="hash", tokens=1)
        _add_user(engine, "with-email", email="someone@example.com", invite="email-link", tokens=1)
        _add_user(engine, "linked", identity=True, tokens=1)

        command.upgrade(alembic_config(database), "059")

        assert _users(engine) == {
            "bot": ("person", "bot-link", True),
            "bot-no-link": ("person", None, False),
            "invited": ("person", "invited-link", True),
            "with-password": ("person", None, False),
            "with-email": ("person", "email-link", True),
            "linked": ("person", None, False),
        }

        # The way back to an older release sets the version back and keeps the column: the next start runs 059 again
        # and changes nothing.
        with engine.begin() as conn:
            conn.execute(text("UPDATE users SET kind = 'service' WHERE id = 'bot-no-link'"))
            conn.execute(text("UPDATE alembic_version SET version_num = '058'"))
        command.upgrade(alembic_config(database), "059")
        assert {user_id: kind for user_id, (kind, _, _) in _users(engine).items()} == {
            "bot": "person", "bot-no-link": "service", "invited": "person", "with-password": "person",
            "with-email": "person", "linked": "person",
        }

        command.downgrade(alembic_config(database), "058")
        with engine.connect() as conn:
            columns = set(
                conn.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")
                ).scalars()
            )
        assert "kind" not in columns
    finally:
        engine.dispose()
