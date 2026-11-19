# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 062 adds the alerts table, each project's alert settings (empty for every existing project) and the install
alerts' retention (90 days on the settings row)."""

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
    """The sync URL of a database of its own at revision 061, dropped after the test."""
    admin = create_engine(migrated_database, isolation_level="AUTOCOMMIT")
    name = f"migration_062_{uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(migrated_database).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "061")
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


def _indexes(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'alerts'")).scalars())


def _insert_project(engine: Engine, project_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (id, name, description, username, password, created_at, updated_at) "
                "VALUES (:id, :id, '', :id, 'pw', now(), now())"
            ),
            {"id": project_id},
        )


def test_the_table_its_indexes_and_both_columns_with_their_defaults_and_back(database: str) -> None:
    engine = create_engine(database)
    try:
        _insert_project(engine, "p1")
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO system_settings (id, audit_retention_days) VALUES (1, 30)"))

        command.upgrade(alembic_config(database), "062")

        with engine.connect() as conn:
            assert conn.execute(text("SELECT alert_settings FROM projects WHERE id = 'p1'")).scalar() == {}
            assert conn.execute(text("SELECT install_alert_retention_days FROM system_settings")).scalar() == 90
        assert _indexes(engine) == {"alerts_pkey", "uq_alerts_open", "ix_alerts_resolved", "ix_alerts_project_opened"}
        with engine.begin() as conn:
            insert = text(
                "INSERT INTO alerts (id, project_id, key, kind, severity, worst_severity, body, opened_at, last_seen_at, "
                "resolved_at) VALUES (:id, :project, 'connectors_off', 'connectors_off', 'problem', 'problem', 'x', "
                "now(), now(), :resolved)"
            )
            conn.execute(insert, {"id": "a1", "project": "p1", "resolved": None})
            conn.execute(insert, {"id": "a2", "project": "p1", "resolved": "2026-11-18 09:00"})
            # The install's own: no project.
            conn.execute(insert, {"id": "a3", "project": None, "resolved": None})
        # One open alert per scope and key.
        with pytest.raises(Exception, match="uq_alerts_open"), engine.begin() as conn:
            conn.execute(insert, {"id": "a4", "project": "p1", "resolved": None})
        # A project's go with it.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM projects WHERE id = 'p1'"))
            assert conn.execute(text("SELECT id FROM alerts")).scalars().all() == ["a3"]

        # The way back to an older release sets the version back: the next start runs 062 again.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '061'"))
        command.upgrade(alembic_config(database), "062")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM alerts")).scalar() == 1

        command.downgrade(alembic_config(database), "061")
        assert "alert_settings" not in _columns(engine, "projects")
        assert "install_alert_retention_days" not in _columns(engine, "system_settings")
        assert _columns(engine, "alerts") == set()
        # 6.8's inserts, which name neither column, work on either side.
        _insert_project(engine, "p2")
    finally:
        engine.dispose()


def test_the_migration_waits_out_a_long_reader_without_holding_updates_behind_it(database: str) -> None:
    """The adds and the table's foreign key each want a lock on projects; queued behind a reader, they would hold every
    statement on projects behind them. 062 asks for half a second at a time."""
    engine = create_engine(database)
    _insert_project(engine, "p1")
    reader = engine.connect()
    finished = threading.Event()
    try:
        # A long read of the projects holds its lock on the table until it ends.
        reader.begin()
        reader.execute(text("SELECT count(*) FROM projects"))
        migration = threading.Thread(
            target=lambda: (command.upgrade(alembic_config(database), "062"), finished.set()), daemon=True
        )
        migration.start()
        time.sleep(1.5)

        # Meanwhile a project edit goes through at once, never queued for long behind the waiting statement.
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '2s'"))
            conn.execute(text("UPDATE projects SET description = 'edited' WHERE id = 'p1'"))
        assert not finished.is_set()

        reader.rollback()
        migration.join(timeout=30)
        assert finished.is_set()
        assert "alert_settings" in _columns(engine, "projects")
        assert "id" in _columns(engine, "alerts")
    finally:
        reader.close()
        engine.dispose()
