# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 054 moves Webshare connectors' countries from ``country_codes`` to ``country_code``."""

import json
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, make_url, text

from api.db.migrations import alembic_config


@pytest.fixture
def database(migrated_database: str) -> Iterator[str]:
    """The sync URL of a database of its own at revision 053, dropped after the test."""
    admin = create_engine(migrated_database, isolation_level="AUTOCOMMIT")
    name = f"migration_054_{uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(migrated_database).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "053")
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


def _configs(engine: Engine) -> dict[str, dict[str, object]]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, config::text FROM connectors")).all()
    return {row[0]: json.loads(row[1]) for row in rows}


def test_webshare_countries_move_to_country_code_and_back(database: str) -> None:
    engine = create_engine(database)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO projects (id, name, description, username, password, created_at, updated_at) "
                "VALUES ('p1', 'one', '', 'u', 'pw', now(), now())"
            ))
            for credential_id, credential_type in (("ws", "webshare"), ("other", "static_proxy_provider")):
                conn.execute(
                    text(
                        "INSERT INTO credentials (id, name, type, config, project_id, created_at, updated_at) "
                        "VALUES (:id, :id, :type, '{}', 'p1', now(), now())"
                    ),
                    {"id": credential_id, "type": credential_type},
                )
            for connector_id, credential_id, config in (
                ("listed", "ws", {"mode": "direct", "country_codes": "au, us, uk, US", "num_proxies": 10}),
                ("empty", "ws", {"mode": "direct", "country_codes": ""}),
                ("none", "ws", {"mode": "direct"}),
                ("not-webshare", "other", {"country_codes": "AU"}),
            ):
                conn.execute(
                    text(
                        "INSERT INTO connectors (id, name, config, enabled, project_id, credential_id, created_at, updated_at) "
                        "VALUES (:id, :id, CAST(:config AS JSON), true, 'p1', :cid, now(), now())"
                    ),
                    {"id": connector_id, "cid": credential_id, "config": json.dumps(config)},
                )

        command.upgrade(alembic_config(database), "054")
        migrated = {
            "listed": {"mode": "direct", "num_proxies": 10, "country_code": ["AU", "US", "GB"]},
            "empty": {"mode": "direct"},
            "none": {"mode": "direct"},
            "not-webshare": {"country_codes": "AU"},
        }
        assert _configs(engine) == migrated

        # The way back to an older release sets the version back: the next start runs 054 again, changing nothing.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '053'"))
        command.upgrade(alembic_config(database), "054")
        assert _configs(engine) == migrated

        command.downgrade(alembic_config(database), "053")
        assert _configs(engine) == {
            "listed": {"mode": "direct", "num_proxies": 10, "country_codes": "AU,US,GB"},
            "empty": {"mode": "direct"},
            "none": {"mode": "direct"},
            "not-webshare": {"country_codes": "AU"},
        }
    finally:
        engine.dispose()
