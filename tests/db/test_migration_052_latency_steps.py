# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 052 adds the step columns and moves the all-time latency sums onto the successful requests, once."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, make_url, text

from api.db.migrations import alembic_config

STEP_COLUMNS = {"avg_route_ms", "avg_preflight_ms", "avg_connect_ms", "avg_handshake_ms", "avg_response_ms"}


@pytest.fixture
def database(migrated_database: str) -> Iterator[str]:
    """The sync URL of a database of its own at revision 051, dropped after the test."""
    admin = create_engine(migrated_database, isolation_level="AUTOCOMMIT")
    name = f"migration_052_{uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(migrated_database).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "051")
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


def test_the_columns_are_added_and_each_total_keeps_its_average_over_its_successes(database: str) -> None:
    engine = create_engine(database)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO metric_totals (kind, entity_id, request_count, success_count, failure_count, "
                "latency_sum_ms, bytes_sent, bytes_received, by_address, encrypted_dns_blocked, updated_at) VALUES "
                "('project', 'half', 10, 5, 5, 2000, 0, 0, 0, 0, now()), "
                "('proxy', 'all-good', 4, 4, 0, 400, 0, 0, 0, 0, now()), "
                "('proxy', 'all-failed', 3, 0, 3, 900, 0, 0, 0, 0, now()), "
                "('tunnel_peer', 'idle', 0, 0, 0, 0, 0, 0, 0, 0, now())"
            ))

        command.upgrade(alembic_config(database), "052")

        with engine.connect() as conn:
            for table in ("project_metrics", "connector_metrics"):
                columns = conn.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = :table"), {"table": table}
                ).scalars()
                assert set(columns) >= STEP_COLUMNS
            rows = conn.execute(text("SELECT entity_id, latency_sum_ms FROM metric_totals"))
            sums = {row.entity_id: row.latency_sum_ms for row in rows}
        # Each average (the sum over its successes now) is what it was over every request.
        assert sums == {"half": 1000.0, "all-good": 400.0, "all-failed": 0.0, "idle": 0.0}

        # The way back to an older release keeps the columns and sets the version back: the next start runs 052 again.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '051'"))
        command.upgrade(alembic_config(database), "052")

        with engine.connect() as conn:
            assert conn.execute(text("SELECT latency_sum_ms FROM metric_totals WHERE entity_id = 'half'")).scalar() == 1000.0

        # A downgrade drops the columns and scales the sums back, so the upgrade after it scales them once.
        command.downgrade(alembic_config(database), "051")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT latency_sum_ms FROM metric_totals WHERE entity_id = 'half'")).scalar() == 2000.0
        command.upgrade(alembic_config(database), "052")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT latency_sum_ms FROM metric_totals WHERE entity_id = 'half'")).scalar() == 1000.0
    finally:
        engine.dispose()
