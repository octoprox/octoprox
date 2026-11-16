# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 061 adds each project's muted Needs attention lines, empty for every existing project."""

import threading
import time
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, make_url, text

from api.db.migrations import alembic_config


@pytest.fixture
def database(migrated_database: str) -> Iterator[str]:
    """The sync URL of a database of its own at revision 060, dropped after the test."""
    admin = create_engine(migrated_database, isolation_level="AUTOCOMMIT")
    name = f"migration_061_{uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(migrated_database).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "060")
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


def _columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = :table"), {"table": table}
        ).scalars()
        return set(found)


def test_an_existing_project_has_muted_nothing_and_the_column_goes_with_a_downgrade(database: str) -> None:
    engine = create_engine(database)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO projects (id, name, description, username, password, created_at, updated_at) "
                "VALUES ('p1', 'one', '', 'u', 'pw', now(), now())"
            ))

        command.upgrade(alembic_config(database), "061")

        with engine.connect() as conn:
            assert conn.execute(text("SELECT attention_mutes FROM projects WHERE id = 'p1'")).scalar() == {}

        # The way back to an older release sets the version back: the next start runs 061 again.
        with engine.begin() as conn:
            conn.execute(text("UPDATE projects SET attention_mutes = '{\"connectors_off\": {}}' WHERE id = 'p1'"))
            conn.execute(text("UPDATE alembic_version SET version_num = '060'"))
        command.upgrade(alembic_config(database), "061")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT attention_mutes FROM projects WHERE id = 'p1'")).scalar() == {
                "connectors_off": {}
            }

        command.downgrade(alembic_config(database), "060")
        assert "attention_mutes" not in _columns(engine, "projects")
        # 6.8's inserts, which name no mutes, work on either side.
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO projects (id, name, description, username, password, created_at, updated_at) "
                "VALUES ('p2', 'two', '', 'u2', 'pw', now(), now())"
            ))
    finally:
        engine.dispose()


def test_the_add_waits_out_a_long_reader_without_holding_updates_behind_it(database: str) -> None:
    """An add queued behind a reader would hold every statement on projects behind it; 061 asks for half a second at
    a time."""
    engine = create_engine(database)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO projects (id, name, description, username, password, created_at, updated_at) "
            "VALUES ('p1', 'one', '', 'u', 'pw', now(), now())"
        ))
    reader = engine.connect()
    finished = threading.Event()
    try:
        # A long read of the projects holds its lock on the table until it ends.
        reader.begin()
        reader.execute(text("SELECT count(*) FROM projects"))
        migration = threading.Thread(
            target=lambda: (command.upgrade(alembic_config(database), "061"), finished.set()), daemon=True
        )
        migration.start()
        time.sleep(1.5)

        # Meanwhile a project edit goes through at once, never queued for long behind the waiting add.
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '2s'"))
            conn.execute(text("UPDATE projects SET description = 'edited' WHERE id = 'p1'"))
        assert not finished.is_set()

        reader.rollback()
        migration.join(timeout=30)
        assert finished.is_set()
        assert "attention_mutes" in _columns(engine, "projects")
    finally:
        reader.close()
        engine.dispose()
