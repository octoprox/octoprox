# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 049 gives the observation log single-column indexes and leaves the exit table only its primary key."""

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, text

from api.core.config import Settings
from api.db.migrations import alembic_config

NEW = {
    "ix_ip_observations_project_id": "(project_id)",
    "ix_ip_observations_connector_id": "(connector_id)",
    "ix_ip_observations_proxy_id": "(proxy_id)",
    "ix_ip_observations_ip": "(ip)",
}
REPLACED = {
    "ix_ip_observations_project_observed",
    "ix_ip_observations_connector_observed",
    "ix_ip_observations_proxy_observed",
    "ix_ip_observations_ip_observed",
}
EXIT_LAST_SEEN = {"ix_connector_exit_ips_last_seen", "ix_connector_exit_ips_connector_last_seen"}
AUTOVACUUM = {"autovacuum_vacuum_scale_factor=0.02", "autovacuum_vacuum_insert_scale_factor=0.02"}


def _indexes(engine: Engine, table: str) -> dict[str, tuple[str, bool]]:
    """Each index on ``table``: the part of its definition after the table, and whether it is valid."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, pg_get_indexdef(i.indexrelid), i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = CAST(:table AS regclass)"
            ),
            {"table": table},
        )
        return {name: (definition.split(" USING btree ", 1)[1], valid) for name, definition, valid in rows}


def _options(engine: Engine, table: str) -> set[str]:
    """The storage parameters set on ``table``."""
    with engine.connect() as conn:
        options = conn.execute(text("SELECT reloptions FROM pg_class WHERE oid = CAST(:table AS regclass)"), {"table": table}).scalar()
        return set(options or [])


@pytest.mark.usefixtures("db_session")  # migrations are at head and the tables are truncated afterwards
def test_indexes_and_storage_change_and_a_broken_build_is_redone(test_settings: Settings) -> None:
    engine = create_engine(test_settings.database_url_sync)
    cfg = alembic_config(test_settings.database_url_sync)
    try:
        command.downgrade(cfg, "048")
        before = _indexes(engine, "ip_observations")
        assert set(before) >= REPLACED and not set(NEW) & set(before)
        assert set(_indexes(engine, "connector_exit_ips")) >= EXIT_LAST_SEEN
        assert _options(engine, "ip_observations") == set() and _options(engine, "connector_exit_ips") == set()

        # A build cut short leaves an invalid index under the name: a unique
        # build over duplicate rows fails the same way a killed one does.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ip_observations (observed_at, project_id, source, ip, candidates, instance_id) "
                    "VALUES (now(), 'p', 'preflight', '10.0.0.1', '[]', ''), (now(), 'p', 'preflight', '10.0.0.1', '[]', '')"
                )
            )
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn, pytest.raises(Exception, match="unique"):
            conn.execute(text("CREATE UNIQUE INDEX CONCURRENTLY ix_ip_observations_ip ON ip_observations (ip)"))
        assert _indexes(engine, "ip_observations")["ix_ip_observations_ip"] == ("(ip)", False)

        command.upgrade(cfg, "049")
        after = _indexes(engine, "ip_observations")
        assert {name: after.get(name) for name in NEW} == {name: (definition, True) for name, definition in NEW.items()}
        assert not REPLACED & set(after)
        assert set(_indexes(engine, "connector_exit_ips")) == {"connector_exit_ips_pkey"}
        assert _options(engine, "ip_observations") == AUTOVACUUM
        assert _options(engine, "connector_exit_ips") == {"fillfactor=80"}

        # Down again restores the composite and last_seen indexes and the default storage.
        command.downgrade(cfg, "048")
        back = _indexes(engine, "ip_observations")
        assert set(back) >= REPLACED and all(back[name][1] for name in REPLACED) and not set(NEW) & set(back)
        exits = _indexes(engine, "connector_exit_ips")
        assert set(exits) >= EXIT_LAST_SEEN and all(exits[name][1] for name in EXIT_LAST_SEEN)
        assert _options(engine, "ip_observations") == set() and _options(engine, "connector_exit_ips") == set()
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ip_observations WHERE project_id = 'p'"))
        engine.dispose()
