# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 065 gives projects their "Connector memory" in minutes, 10 for every project, and keeps the stored one in
requests."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, make_url, text

from api.db.migrations import alembic_config

SETTINGS = ("exit_reputation_connector_half_life_requests", "exit_reputation_connector_half_life_minutes")


@pytest.fixture
def database(migrated_database: str) -> Iterator[str]:
    """The sync URL of a database of its own at revision 064, dropped after the test."""
    admin = create_engine(migrated_database, isolation_level="AUTOCOMMIT")
    name = f"migration_065_{uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(migrated_database).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "064")
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


def _columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = :table"),
            {"table": table},
        ).scalars()
        return set(found)


def test_projects_get_ten_minutes_and_keep_their_requests(database: str) -> None:
    engine = create_engine(database)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO projects (id, name, description, username, password, created_at, updated_at, "
                    "exit_reputation_connector_half_life_requests) "
                    "VALUES ('p1', 'one', '', 'u', 'pw', now(), now(), 1000)"
                )
            )

        command.upgrade(alembic_config(database), "065")

        with engine.connect() as conn:
            settings = conn.execute(text(f"SELECT {', '.join(SETTINGS)} FROM projects WHERE id = 'p1'")).one()
        assert tuple(settings) == (1000, 10)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO projects (id, name, description, username, password, created_at, updated_at) "
                    "VALUES ('p2', 'two', '', 'u2', 'pw', now(), now())"
                )
            )
            settings = conn.execute(text(f"SELECT {', '.join(SETTINGS)} FROM projects WHERE id = 'p2'")).one()
        assert tuple(settings) == (100, 10)

        # The way back to an older release sets the version back: the next start runs 065 again.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '064'"))
        command.upgrade(alembic_config(database), "065")

        command.downgrade(alembic_config(database), "064")
        assert "exit_reputation_connector_half_life_minutes" not in _columns(engine, "projects")
        assert "exit_reputation_connector_half_life_requests" in _columns(engine, "projects")
    finally:
        engine.dispose()
