# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 060 gives projects their exit reputation settings and adds the records' two tables, each indexed by its
key and the day it was last seen, the exit IP records also by the end of their avoid and the connector records by their
connector."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, make_url, text

from api.db.migrations import alembic_config
from tests.conftest import tables

SETTINGS = (
    "exit_reputation",
    "exit_reputation_avoid_minutes",
    "exit_reputation_half_life_hours",
    "exit_reputation_connector_half_life_requests",
    "exit_reputation_bad_reports",
    "exit_reputation_bad_share",
)


@pytest.fixture
def database(migrated_database: str) -> Iterator[str]:
    """The sync URL of a database of its own at revision 059, dropped after the test."""
    admin = create_engine(migrated_database, isolation_level="AUTOCOMMIT")
    name = f"migration_060_{uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(migrated_database).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "059")
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


def _indexes(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = :table"), {"table": table}
            ).scalars()
        )


def _options(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        options = conn.execute(
            text("SELECT reloptions FROM pg_class WHERE oid = CAST(:table AS regclass)"),
            {"table": table},
        ).scalar()
        return set(options or [])


def test_projects_get_the_defaults_and_records_go_with_their_project_or_connector(database: str) -> None:
    engine = create_engine(database)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO projects (id, name, description, username, password, created_at, updated_at) "
                    "VALUES ('p1', 'one', '', 'u', 'pw', now(), now())"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO credentials (id, name, type, config, project_id, created_at, updated_at) "
                    "VALUES ('k1', 'one', 'static_proxy_provider', '{}', 'p1', now(), now())"
                )
            )
            for connector_id in ("c1", "c2"):
                conn.execute(
                    text(
                        "INSERT INTO connectors (id, name, config, enabled, project_id, credential_id, created_at, "
                        "updated_at) VALUES (:id, :id, '{}', true, 'p1', 'k1', now(), now())"
                    ),
                    {"id": connector_id},
                )

        command.upgrade(alembic_config(database), "060")

        with engine.connect() as conn:
            settings = conn.execute(text(f"SELECT {', '.join(SETTINGS)} FROM projects WHERE id = 'p1'")).one()
        assert tuple(settings) == ("off", 60, 24, 100, 1, 50)
        # Besides the key, the day the retention sweep selects by, the avoid's end the controller's read of the avoids
        # in force selects by, and the connector its factor read and a connector delete select by; new pages keep a
        # fifth free for updates in place.
        assert _indexes(engine, "exit_ip_reputation") == {
            "exit_ip_reputation_pkey",
            "ix_exit_ip_reputation_last_seen_day",
            "ix_exit_ip_reputation_avoid_until",
        }
        assert _indexes(engine, "connector_site_reputation") == {
            "connector_site_reputation_pkey",
            "ix_connector_site_reputation_last_seen_day",
            "ix_connector_site_reputation_connector_id",
        }
        assert _options(engine, "exit_ip_reputation") == _options(engine, "connector_site_reputation") == {
            "fillfactor=80"
        }
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO exit_ip_reputation (project_id, ip, site, weighted_at, first_seen_at, last_seen_at, "
                    "last_seen_day) VALUES ('p1', '203.0.113.7', 'google.com', now(), now(), now(), current_date)"
                )
            )
            record = conn.execute(
                text(
                    "SELECT good_reports, bad_reports, weighted_good, weighted_bad, weighted_avoids, avoid_until, "
                    "last_report_at, last_reason FROM exit_ip_reputation"
                )
            ).one()
            assert tuple(record) == (0, 0, 0.0, 0.0, 0.0, None, None, None)
            for connector_id in ("c1", "c2"):
                conn.execute(
                    text(
                        "INSERT INTO connector_site_reputation (project_id, connector_id, site, place, weighted_at, "
                        "first_seen_at, last_seen_at, last_seen_day) "
                        "VALUES ('p1', :c, 'google.com', 'US', now(), now(), now(), current_date)"
                    ),
                    {"c": connector_id},
                )
            factor = conn.execute(
                text(
                    "SELECT blocked_reports, good_reports, requests, weighted_blocked, weighted_requests, factor "
                    "FROM connector_site_reputation WHERE connector_id = 'c1'"
                )
            ).one()
            assert tuple(factor) == (0, 0, 0, 0.0, 0.0, None)
            conn.execute(text("DELETE FROM connectors WHERE id = 'c1'"))
            assert conn.execute(text("SELECT connector_id FROM connector_site_reputation")).scalars().all() == ["c2"]
            conn.execute(text("DELETE FROM projects WHERE id = 'p1'"))
            assert conn.execute(text("SELECT count(*) FROM exit_ip_reputation")).scalar() == 0
            assert conn.execute(text("SELECT count(*) FROM connector_site_reputation")).scalar() == 0

        # The way back to an older release sets the version back: the next start runs 060 again.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '059'"))
        command.upgrade(alembic_config(database), "060")

        command.downgrade(alembic_config(database), "059")
        assert not {"exit_ip_reputation", "connector_site_reputation"} & tables(database)
        assert not set(SETTINGS) & _columns(engine, "projects")
    finally:
        engine.dispose()
