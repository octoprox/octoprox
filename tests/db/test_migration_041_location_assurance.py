# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 041 folds the location policy and the preflight mode into one assurance level."""

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from api.core.config import Settings
from api.db.migrations import alembic_config

PROJECTS = {
    # name: (policy, preflight mode) -> (assurance, preflight on)
    "m-warn-off": ("warn", "off", "observe", False),
    "m-warn-report": ("warn", "report", "observe", True),
    "m-warn-retry": ("warn", "retry", "verified", True),
    "m-warn-reject": ("warn", "reject", "verified", True),
    "m-strict-off": ("strict", "off", "confirmed", False),
    "m-strict-report": ("strict", "report", "confirmed", True),
    "m-strict-reject": ("strict", "reject", "confirmed", True),
}


@pytest.mark.usefixtures("db_session")  # migrations are at head and the tables are truncated afterwards
def test_existing_projects_keep_their_behaviour(test_settings: Settings) -> None:
    engine = create_engine(test_settings.database_url_sync)
    cfg = alembic_config(test_settings.database_url_sync)
    try:
        command.downgrade(cfg, "040")
        with engine.begin() as conn:
            for name, (policy, mode, _, _) in PROJECTS.items():
                conn.execute(
                    text(
                        "INSERT INTO projects (id, name, username, password, location_policy, location_preflight, created_at, updated_at) "
                        "VALUES (:id, :name, :user, 'pw', :policy, :mode, now(), now())"
                    ),
                    {"id": name, "name": name, "user": name, "policy": policy, "mode": mode},
                )
        command.upgrade(cfg, "041")
        with engine.begin() as conn:
            got = {
                r.id: (r.location_assurance, r.location_preflight)
                for r in conn.execute(text("SELECT id, location_assurance, location_preflight FROM projects WHERE id LIKE 'm-%'"))
            }
            columns = {
                r[0]: r[1]
                for r in conn.execute(
                    text("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'projects'")
                )
            }
        assert got == {name: (assurance, on) for name, (_, _, assurance, on) in PROJECTS.items()}
        assert "location_policy" not in columns and columns["location_preflight"] == "boolean"
        # Down again restores a pair the previous version understands.
        command.downgrade(cfg, "040")
        with engine.begin() as conn:
            back = {
                r.id: (r.location_policy, r.location_preflight)
                for r in conn.execute(text("SELECT id, location_policy, location_preflight FROM projects WHERE id LIKE 'm-%'"))
            }
        assert back["m-warn-off"] == ("warn", "off") and back["m-warn-report"] == ("warn", "report")
        assert back["m-warn-reject"] == ("warn", "reject") and back["m-strict-off"] == ("strict", "off")
        assert back["m-strict-reject"] == ("strict", "reject")
        command.upgrade(cfg, "head")
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM projects WHERE id LIKE 'm-%'"))
