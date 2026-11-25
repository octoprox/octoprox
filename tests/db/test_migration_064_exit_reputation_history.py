# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 064 adds the tables of the factor moves and the avoid moves, empty: history starts with the release; and
each project's Keep data for, 30 days for the projects already there."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, make_url, text

from api.db.migrations import alembic_config


@pytest.fixture
def database(migrated_database: str) -> Iterator[str]:
    """The sync URL of a database of its own at revision 063, dropped after the test."""
    admin = create_engine(migrated_database, isolation_level="AUTOCOMMIT")
    name = f"migration_064_{uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(migrated_database).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "063")
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


def _indexes(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = :table"), {"table": table}).scalars()
        )


def _insert_project(engine: Engine, project_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (id, name, description, username, password, created_at, updated_at) "
                "VALUES (:id, :id, '', :id, 'pw', now(), now())"
            ),
            {"id": project_id},
        )


_INSERT_MOVE = text(
    "INSERT INTO connector_factor_moves (project_id, site, place, connector_id, at, old_factor, new_factor, requests, "
    "blocked_reports) VALUES (:project, 'google.com', 'US', 'no-such-connector', :at, NULL, 0.75, 20, 5)"
)
_INSERT_AVOID_MOVE = text(
    "INSERT INTO exit_ip_avoid_moves (project_id, site, ip, at, old_avoid_until, new_avoid_until) "
    "VALUES (:project, 'google.com', '203.0.113.7', :at, NULL, '2026-11-25 10:00')"
)
_DAYS = text("SELECT exit_reputation_retention_days FROM projects WHERE id = :id")
# The project columns of the exit reputation's days.
_DAYS_COLUMNS = text(
    "SELECT column_name FROM information_schema.columns WHERE table_name = 'projects' "
    "AND column_name LIKE 'exit\\_reputation\\_%\\_days'"
)
_COUNTS = text("SELECT (SELECT count(*) FROM connector_factor_moves), (SELECT count(*) FROM exit_ip_avoid_moves)")


def test_the_tables_with_their_indexes_and_the_setting_gone_with_their_project_and_back(database: str) -> None:
    engine = create_engine(database)
    try:
        _insert_project(engine, "p1")

        command.upgrade(alembic_config(database), "064")

        assert _indexes(engine, "connector_factor_moves") == {
            "connector_factor_moves_pkey",
            "ix_connector_factor_moves_project_at",
        }
        assert _indexes(engine, "exit_ip_avoid_moves") == {"exit_ip_avoid_moves_pkey", "ix_exit_ip_avoid_moves_project_at"}
        with engine.begin() as conn:
            assert tuple(conn.execute(_COUNTS).one()) == (0, 0)
            # One setting for the records and the history; a project already there keeps the 30 days the install kept
            # its records.
            assert list(conn.execute(_DAYS_COLUMNS).scalars()) == ["exit_reputation_retention_days"]
            assert conn.execute(_DAYS, {"id": "p1"}).scalar() == 30
            # No foreign key to the connector or the exit IP's record: their moves age out.
            conn.execute(_INSERT_MOVE, {"project": "p1", "at": "2026-11-25 09:00"})
            conn.execute(_INSERT_MOVE, {"project": "p1", "at": "2026-11-25 09:01"})
            conn.execute(_INSERT_AVOID_MOVE, {"project": "p1", "at": "2026-11-25 09:00"})
        # One move per key and time.
        with pytest.raises(Exception, match="connector_factor_moves_pkey"), engine.begin() as conn:
            conn.execute(_INSERT_MOVE, {"project": "p1", "at": "2026-11-25 09:00"})
        with pytest.raises(Exception, match="exit_ip_avoid_moves_pkey"), engine.begin() as conn:
            conn.execute(_INSERT_AVOID_MOVE, {"project": "p1", "at": "2026-11-25 09:00"})
        # A project's go with it.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM projects WHERE id = 'p1'"))
            assert tuple(conn.execute(_COUNTS).one()) == (0, 0)
        _insert_project(engine, "p2")
        with engine.begin() as conn:
            conn.execute(_INSERT_MOVE, {"project": "p2", "at": "2026-11-25 10:00"})
            conn.execute(_INSERT_AVOID_MOVE, {"project": "p2", "at": "2026-11-25 10:00"})
            conn.execute(text("UPDATE projects SET exit_reputation_retention_days = 0 WHERE id = 'p2'"))

        # The way back to an older release sets the version back: the next start runs 064 again.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '063'"))
        command.upgrade(alembic_config(database), "064")
        with engine.connect() as conn:
            assert tuple(conn.execute(_COUNTS).one()) == (1, 1)
            assert conn.execute(_DAYS, {"id": "p2"}).scalar() == 0

        command.downgrade(alembic_config(database), "063")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('connector_factor_moves')")).scalar() is None
            assert conn.execute(text("SELECT to_regclass('exit_ip_avoid_moves')")).scalar() is None
            assert list(conn.execute(_DAYS_COLUMNS).scalars()) == []
    finally:
        engine.dispose()
