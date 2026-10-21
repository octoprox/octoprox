# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 043 stores the two location decisions in place of the assurance level."""

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from api.core.config import Settings
from api.db.migrations import alembic_config

LEVELS = {
    # name: level -> (contradicted, unknown)
    "d-observe": ("observe", "evidence", "vendor"),
    "d-verified": ("verified", "refuse", "vendor"),
    "d-confirmed": ("confirmed", "refuse", "refuse"),
}


@pytest.mark.usefixtures("db_session")  # migrations are at head and the tables are truncated afterwards
def test_levels_become_their_two_decisions(test_settings: Settings) -> None:
    engine = create_engine(test_settings.database_url_sync)
    cfg = alembic_config(test_settings.database_url_sync)
    try:
        command.downgrade(cfg, "042")
        with engine.begin() as conn:
            for name, (level, _, _) in LEVELS.items():
                conn.execute(
                    text(
                        "INSERT INTO projects (id, name, username, password, location_assurance, created_at, updated_at) "
                        "VALUES (:id, :name, :user, 'pw', :level, now(), now())"
                    ),
                    {"id": name, "name": name, "user": name, "level": level},
                )
        command.upgrade(cfg, "043")
        with engine.begin() as conn:
            got = {
                r.id: (r.location_contradicted, r.location_unknown)
                for r in conn.execute(text("SELECT id, location_contradicted, location_unknown FROM projects WHERE id LIKE 'd-%'"))
            }
            columns = {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'projects'"))}
        assert got == {name: (c, u) for name, (_, c, u) in LEVELS.items()}
        assert "location_assurance" not in columns
        # Down again restores the level. The pairs no level offered fold into one that
        # 041's code knows: the vendor choice into observe, refusing unknown exits alone
        # into confirmed, since the downgraded code would reject any other value.
        with engine.begin() as conn:
            conn.execute(text("UPDATE projects SET location_contradicted = 'vendor' WHERE id = 'd-observe'"))
            conn.execute(
                text(
                    "INSERT INTO projects (id, name, username, password, location_contradicted, location_unknown, created_at, updated_at) "
                    "VALUES ('d-placed', 'd-placed', 'd-placed', 'pw', 'evidence', 'refuse', now(), now())"
                )
            )
        command.downgrade(cfg, "042")
        with engine.begin() as conn:
            back = {r.id: r.location_assurance for r in conn.execute(text("SELECT id, location_assurance FROM projects WHERE id LIKE 'd-%'"))}
        assert back == {**{name: level for name, (level, _, _) in LEVELS.items()}, "d-placed": "confirmed"}
        command.upgrade(cfg, "head")
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM projects WHERE id LIKE 'd-%'"))
