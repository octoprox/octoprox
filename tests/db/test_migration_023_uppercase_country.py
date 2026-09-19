# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 023 upper-cases the exit country already stored in proxy metadata."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from api.core.config import Settings
from api.db.migrations import MIGRATIONS_DIR


def _alembic(url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.mark.usefixtures("db_session")  # migrations are at head and the tables are truncated afterwards
def test_existing_countries_are_upper_cased(test_settings: Settings) -> None:
    engine = create_engine(test_settings.database_url_sync)
    cfg = _alembic(test_settings.database_url_sync)
    project_id, credential_id, connector_id = str(uuid4()), str(uuid4()), str(uuid4())
    ts = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    rows = {
        "lower": {"discovered_ip": "1.1.1.1", "country": "nz"},
        "upper": {"discovered_ip": "2.2.2.2", "country": "US"},
        "none": {"discovered_ip": "3.3.3.3"},
    }
    ids = {name: str(uuid4()) for name in rows}
    try:
        command.downgrade(cfg, "022")
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO projects (id, name, username, password, created_at, updated_at) VALUES (:id, :name, :name, 'pw', :ts, :ts)"),
                {"id": project_id, "name": f"mig023-{project_id[:8]}", "ts": ts},
            )
            conn.execute(
                text("INSERT INTO credentials (id, name, type, config, project_id, created_at, updated_at) VALUES (:id, 'c', 'static_proxy_provider', '{}', :pid, :ts, :ts)"),
                {"id": credential_id, "pid": project_id, "ts": ts},
            )
            conn.execute(
                text("INSERT INTO connectors (id, name, config, enabled, project_id, credential_id, created_at, updated_at) VALUES (:id, 'k', '{}', true, :pid, :cid, :ts, :ts)"),
                {"id": connector_id, "pid": project_id, "cid": credential_id, "ts": ts},
            )
            for name, metadata in rows.items():
                conn.execute(
                    text("INSERT INTO proxies (id, host, port, protocol, connector_id, tags, metadata, created_at, updated_at) VALUES (:id, :host, 8080, 'http', :cid, '[]', :meta, :ts, :ts)"),
                    {"id": ids[name], "host": f"{name}.example.com", "cid": connector_id, "meta": json.dumps(metadata), "ts": ts},
                )
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            stored = {
                name: conn.execute(text("SELECT metadata FROM proxies WHERE id = :id"), {"id": ids[name]}).scalar_one()
                for name in rows
            }
        assert stored["lower"]["country"] == "NZ"
        assert stored["lower"]["discovered_ip"] == "1.1.1.1"  # other keys untouched
        assert stored["upper"]["country"] == "US"
        assert "country" not in stored["none"]
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_id})
        engine.dispose()
