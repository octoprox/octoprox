# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 046 builds the observation log's indexes concurrently and recovers from a build cut short."""

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, text

from api.core.config import Settings
from api.db.migrations import alembic_config

NEW = {
    "ix_ip_observations_project_observed": "(project_id, observed_at)",
    "ix_ip_observations_connector_observed": "(connector_id, observed_at)",
    "ix_ip_observations_proxy_observed": "(proxy_id, observed_at)",
    "ix_ip_observations_ip_observed": "(ip, observed_at)",
    "ix_ip_observations_contradicted": "(observed_at) WHERE (country_conflict IS TRUE)",
}
REPLACED = {"ix_ip_observations_connector_id", "ix_ip_observations_proxy_id"}


def _indexes(engine: Engine) -> dict[str, tuple[str, bool]]:
    """Each index on ip_observations: the part of its definition after the table, and whether it is valid."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, pg_get_indexdef(i.indexrelid), i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = 'ip_observations'::regclass"
            )
        )
        return {name: (definition.split(" USING btree ", 1)[1], valid) for name, definition, valid in rows}


@pytest.mark.usefixtures("db_session")  # migrations are at head and the tables are truncated afterwards
def test_indexes_replace_the_old_ones_and_a_broken_build_is_redone(test_settings: Settings) -> None:
    engine = create_engine(test_settings.database_url_sync)
    cfg = alembic_config(test_settings.database_url_sync)
    try:
        command.downgrade(cfg, "045")
        before = _indexes(engine)
        assert set(before) >= REPLACED and not set(NEW) & set(before)

        # A build cut short leaves an invalid index under the name: a unique
        # build over duplicate rows fails the same way a killed one does.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ip_observations (observed_at, project_id, source, ip, candidates, instance_id) "
                    "VALUES (now(), 'p', 'preflight', '10.0.0.1', '[]', ''), (now(), 'p', 'preflight', '10.0.0.2', '[]', '')"
                )
            )
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn, pytest.raises(Exception, match="unique"):
            conn.execute(text("CREATE UNIQUE INDEX CONCURRENTLY ix_ip_observations_project_observed ON ip_observations (project_id)"))
        assert _indexes(engine)["ix_ip_observations_project_observed"] == ("(project_id)", False)

        command.upgrade(cfg, "046")
        after = _indexes(engine)
        assert {name: after.get(name) for name in NEW} == {name: (definition, True) for name, definition in NEW.items()}
        assert not REPLACED & set(after)

        # Down again restores the single-column indexes and drops the new ones.
        command.downgrade(cfg, "045")
        back = _indexes(engine)
        assert set(back) >= REPLACED and all(back[name][1] for name in REPLACED) and not set(NEW) & set(back)
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ip_observations WHERE project_id = 'p'"))
        engine.dispose()
