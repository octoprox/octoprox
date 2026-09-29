# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 032 folds ``conflict`` and ``disagreement`` into one nullable ``country_conflict``."""

from datetime import UTC, datetime

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


ROWS = {
    # ip: (claimed, resolved_source, conflict, disagreement) -> expected country_conflict
    "10.0.0.1": ("US", "database", False, False, False),  # confirmed by an independent source
    "10.0.0.2": ("US", "database", True, False, True),  # contradicted
    "10.0.0.3": ("US", "database", False, True, None),  # sources disagreed
    "10.0.0.4": ("US", "vendor", False, False, None),  # only the vendor answered
    "10.0.0.5": ("US", None, False, False, None),  # nothing answered
    "10.0.0.6": (None, "database", False, False, None),  # nothing claimed
}


@pytest.mark.usefixtures("db_session")  # migrations are at head and the tables are truncated afterwards
def test_verdicts_are_converted_conservatively(test_settings: Settings) -> None:
    engine = create_engine(test_settings.database_url_sync)
    cfg = _alembic(test_settings.database_url_sync)
    ts = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    try:
        command.downgrade(cfg, "031")
        with engine.begin() as conn:
            for ip, (claimed, source, conflict, disagreement, _expected) in ROWS.items():
                conn.execute(
                    text(
                        "INSERT INTO ip_observations (observed_at, source, ip, claimed_country, resolved_country, resolved_source, conflict, disagreement, candidates, instance_id) "
                        "VALUES (:ts, 'discovery', :ip, :claimed, 'GB', :rsource, :conflict, :disagreement, '[]', 't')"
                    ),
                    {"ts": ts, "ip": ip, "claimed": claimed, "rsource": source, "conflict": conflict, "disagreement": disagreement},
                )
        command.upgrade(cfg, "032")
        with engine.begin() as conn:
            got = {r.ip: r.country_conflict for r in conn.execute(text("SELECT ip, country_conflict FROM ip_observations"))}
            columns = {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'ip_observations'"))}
        assert got == {ip: expected for ip, (*_, expected) in ROWS.items()}
        assert "disagreement" not in columns and "conflict" not in columns
        # Down and up again keeps the rows readable.
        command.downgrade(cfg, "031")
        command.upgrade(cfg, "head")
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ip_observations WHERE instance_id = 't'"))
