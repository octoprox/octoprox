# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 053 drops the observations' candidates and instance ID, and adds each project's observation log settings."""

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
    """The sync URL of a database of its own at revision 052, dropped after the test."""
    admin = create_engine(migrated_database, isolation_level="AUTOCOMMIT")
    name = f"migration_053_{uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(migrated_database).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "052")
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


def test_the_rows_lose_the_two_columns_and_projects_keep_their_log_inheriting_the_retention(database: str) -> None:
    engine = create_engine(database)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO projects (id, name, description, username, password, created_at, updated_at) "
                "VALUES ('p1', 'one', '', 'u', 'pw', now(), now())"
            ))
            conn.execute(text(
                "INSERT INTO ip_observations (observed_at, project_id, source, ip, candidates, instance_id) "
                "VALUES (now(), 'p1', 'preflight', '10.0.0.1', '[{\"source\": \"database\"}]', 'gateway-1')"
            ))

        command.upgrade(alembic_config(database), "053")

        assert not {"candidates", "instance_id"} & _columns(engine, "ip_observations")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT ip FROM ip_observations")).scalar() == "10.0.0.1"
            project = conn.execute(text("SELECT observation_log, observation_retention_days FROM projects WHERE id = 'p1'")).one()
        assert tuple(project) == (True, None)

        # The way back to an older release sets the version back: the next start runs 053 again.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '052'"))
        command.upgrade(alembic_config(database), "053")

        # A downgrade brings the columns back, filled for the rows written meanwhile, and 6.3's inserts work again.
        command.downgrade(alembic_config(database), "052")
        assert {"candidates", "instance_id"} <= _columns(engine, "ip_observations")
        assert not {"observation_log", "observation_retention_days"} & _columns(engine, "projects")
        with engine.begin() as conn:
            assert tuple(conn.execute(text("SELECT candidates::text, instance_id FROM ip_observations")).one()) == ("[]", "")
            conn.execute(text(
                "INSERT INTO ip_observations (observed_at, source, ip, candidates, instance_id) "
                "VALUES (now(), 'preflight', '10.0.0.2', '[]', 'gateway-2')"
            ))
    finally:
        engine.dispose()


def test_the_drop_waits_out_a_long_reader_without_holding_inserts_behind_it(database: str) -> None:
    """A drop queued behind a reader would hold every insert behind it; 053 asks for half a second at a time."""
    engine = create_engine(database)
    reader = engine.connect()
    finished = threading.Event()
    try:
        # A long log page read holds its lock on the table until it ends.
        reader.begin()
        reader.execute(text("SELECT count(*) FROM ip_observations"))
        migration = threading.Thread(
            target=lambda: (command.upgrade(alembic_config(database), "053"), finished.set()), daemon=True
        )
        migration.start()
        time.sleep(1.5)

        # Meanwhile the ingest's insert goes through at once, never queued for long behind the waiting drop.
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '2s'"))
            conn.execute(text(
                "INSERT INTO ip_observations (observed_at, source, ip, candidates, instance_id) "
                "VALUES (now(), 'preflight', '10.0.0.3', '[]', '')"
            ))
        assert not finished.is_set()

        reader.rollback()
        migration.join(timeout=30)
        assert finished.is_set()
        assert "candidates" not in _columns(engine, "ip_observations")
    finally:
        reader.close()
        engine.dispose()
