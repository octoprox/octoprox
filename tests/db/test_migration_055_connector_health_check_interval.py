# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 055 gives connectors a health check config and drops the projects' unread health check columns."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, make_url, text

from api.db.migrations import alembic_config


@pytest.fixture
def database(migrated_database: str) -> Iterator[str]:
    """The sync URL of a database of its own at revision 054, dropped after the test."""
    admin = create_engine(migrated_database, isolation_level="AUTOCOMMIT")
    name = f"migration_055_{uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(migrated_database).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "054")
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


def test_connectors_get_an_empty_config_and_projects_lose_the_columns(database: str) -> None:
    engine = create_engine(database)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO projects (id, name, description, username, password, health_check_interval, "
                "health_check_timeout, created_at, updated_at) VALUES ('p1', 'one', '', 'u', 'pw', 120, 10, now(), now())"
            ))
            conn.execute(text(
                "INSERT INTO credentials (id, name, type, config, project_id, created_at, updated_at) "
                "VALUES ('c1', 'c', 'webshare', '{}', 'p1', now(), now())"
            ))
            conn.execute(text(
                "INSERT INTO connectors (id, name, config, enabled, project_id, credential_id, created_at, updated_at) "
                "VALUES ('k1', 'k', '{}', true, 'p1', 'c1', now(), now())"
            ))

        command.upgrade(alembic_config(database), "055")

        assert not {"health_check_interval", "health_check_timeout"} & _columns(engine, "projects")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT health_check_config::text FROM connectors WHERE id = 'k1'")).scalar() == "{}"

        # The way back to an older release sets the version back: the next start runs 055 again.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '054'"))
        command.upgrade(alembic_config(database), "055")

        command.downgrade(alembic_config(database), "054")
        assert "health_check_config" not in _columns(engine, "connectors")
        with engine.connect() as conn:
            project = conn.execute(text("SELECT health_check_interval, health_check_timeout FROM projects WHERE id = 'p1'")).one()
        assert tuple(project) == (60, 30)
    finally:
        engine.dispose()
