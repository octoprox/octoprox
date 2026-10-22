# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 048: the proxy status, the running totals and the metric flushes, added online beside 5.x and repeatable.

The database tests take the shared test database down to 047 and back to head; ``db_session`` empties the tables after.
"""

import importlib
import shutil
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import structlog
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import Settings
from api.db.migrations import MIGRATIONS_DIR, alembic_config
from tests.conftest import tables

migration = importlib.import_module("api.migrations.versions.048_role_split")

DRAINING_SINCE = datetime(2026, 10, 16, 9, 30, 0, 123456)
LATER_DRAIN = datetime(2026, 10, 17, 8, 0)
T1 = datetime(2026, 10, 1, 12, 0)
T2 = datetime(2026, 10, 1, 12, 1)
T3 = datetime(2026, 10, 1, 12, 2)
VERSIONED = ("projects", "credentials", "connectors", "proxies")
RETRY_LOGGED = "Migration statement waiting for a lock; retrying"

SEED_047 = [
    "INSERT INTO projects (id, name, username, password, created_at, updated_at) "
    "VALUES ('m48-project', 'm48', 'm48', 'pw', :t1, :t1)",
    "INSERT INTO credentials (id, name, type, config, project_id, created_at, updated_at) "
    "VALUES ('m48-credential', 'm48', 'static_proxy_provider', '{}', 'm48-project', :t1, :t1)",
    "INSERT INTO connectors (id, name, config, enabled, project_id, credential_id, created_at, updated_at) "
    "VALUES ('m48-connector', 'm48', '{}', true, 'm48-project', 'm48-credential', :t1, :t1)",
    "INSERT INTO proxies (id, host, port, protocol, connector_id, tags, metadata, created_at, updated_at) VALUES "
    "('m48-draining', '10.0.0.1', 8080, 'http', 'm48-connector', '[]', "
    f"'{{\"country\": \"GB\", \"draining_started_at\": \"{DRAINING_SINCE.isoformat()}\"}}', :t1, :t1), "
    "('m48-serving', '10.0.0.2', 8080, 'http', 'm48-connector', '[]', '{\"country\": \"GB\"}', :t1, :t1)",
    # Two windows of one proxy and one of the other: 3 requests at 100 ms then 1 at 300 ms.
    "INSERT INTO proxy_metrics (proxy_id, timestamp, request_count, success_count, failure_count, avg_latency_ms, "
    "bytes_sent, bytes_received, status, granularity) VALUES "
    "('m48-serving', :t1, 3, 2, 1, 100.0, 10, 100, 'healthy', 60), "
    "('m48-serving', :t2, 1, 1, 0, 300.0, 5, 50, 'healthy', 60), "
    "('m48-draining', :t1, 2, 2, 0, 50.0, 1, 2, 'healthy', 60)",
    "INSERT INTO project_metrics (project_id, timestamp, request_count, success_count, failure_count, avg_latency_ms, "
    "bytes_sent, bytes_received, granularity) VALUES "
    "('m48-project', :t1, 5, 4, 1, 90.0, 11, 102, 60), ('m48-project', :t2, 1, 1, 0, 300.0, 5, 50, 3600)",
    # No foreign key to the device, which the totals do not need either.
    "INSERT INTO tunnel_peer_metrics (peer_id, protocol, project_id, timestamp, request_count, success_count, "
    "failure_count, avg_latency_ms, bytes_sent, bytes_received, by_address, encrypted_dns_blocked, granularity) VALUES "
    "('m48-device', 'wireguard', 'm48-project', :t1, 2, 2, 0, 20.0, 7, 70, 3, 1, 60), "
    "('m48-device', 'wireguard', 'm48-project', :t2, 1, 0, 1, 40.0, 1, 10, 1, 0, 60)",
    # A legacy or hand-edited window: the counters were always nullable, the totals' columns are not.
    "INSERT INTO proxies (id, host, port, protocol, connector_id, tags, metadata, created_at, updated_at) VALUES "
    "('m48-legacy', '10.0.0.3', 8080, 'http', 'm48-connector', '[]', '{}', :t1, :t1)",
    "INSERT INTO proxy_metrics (proxy_id, timestamp, request_count, success_count, failure_count, avg_latency_ms, "
    "bytes_sent, bytes_received, status, granularity) VALUES "
    "('m48-legacy', :t1, NULL, NULL, NULL, NULL, 4, 40, 'healthy', 60)",
]
# What 048 makes of the seed: a drain in flight comes back draining since it started, the rest serve until checked.
STATUSES = {
    "m48-draining": ("draining", DRAINING_SINCE),
    "m48-serving": ("healthy", None),
    "m48-legacy": ("healthy", None),
}
TOTALS = {
    ("proxy", "m48-serving"): (4, 3, 1, 600.0, 15, 150, 0, 0, T2),
    ("proxy", "m48-draining"): (2, 2, 0, 100.0, 1, 2, 0, 0, T1),
    ("proxy", "m48-legacy"): (0, 0, 0, 0.0, 4, 40, 0, 0, T1),
    ("project", "m48-project"): (6, 5, 1, 750.0, 16, 152, 0, 0, T2),
    ("tunnel_peer", "m48-device"): (3, 2, 1, 80.0, 8, 80, 4, 1, T2),
}
# The two tables 048 creates: (column, type, nullable, default) in order, and the primary key.
METRIC_TOTALS = (
    [
        ("kind", "character varying", "NO", None),
        ("entity_id", "character varying", "NO", None),
        *(
            (name, "bigint", "NO", "0")
            for name in ("request_count", "success_count", "failure_count")
        ),
        ("latency_sum_ms", "double precision", "NO", "0"),
        *(
            (name, "bigint", "NO", "0")
            for name in ("bytes_sent", "bytes_received", "by_address", "encrypted_dns_blocked")
        ),
        ("updated_at", "timestamp without time zone", "NO", None),
    ],
    ["kind", "entity_id"],
)
METRIC_FLUSHES = (
    [("message_id", "character varying", "NO", None), ("flushed_at", "timestamp without time zone", "NO", None)],
    ["message_id"],
)
STATUS_COLUMNS = [
    ("status", "character varying", "NO", "'unknown'::character varying"),
    ("status_changed_at", "timestamp without time zone", "YES", None),
]


@pytest.fixture
def engine(test_settings: Settings) -> Iterator[Engine]:
    engine = create_engine(test_settings.database_url_sync)
    yield engine
    engine.dispose()


@pytest.fixture
def at_047(test_settings: Settings, engine: Engine, db_session: AsyncSession) -> Iterator[Config]:
    """The test database brought down to 047, as 5.x left it; back at head after the test, the seed's rows gone."""
    cfg = alembic_config(test_settings.database_url_sync)
    command.downgrade(cfg, "047")
    try:
        yield cfg
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM projects WHERE id = 'm48-project'"))
            conn.execute(text("DELETE FROM tunnel_peer_metrics WHERE peer_id = 'm48-device'"))


def _seed(engine: Engine) -> None:
    with engine.begin() as conn:
        for statement in SEED_047:
            conn.execute(text(statement), {"t1": T1, "t2": T2})


def _statuses(conn: Connection) -> dict[str, tuple[str, datetime | None]]:
    rows = conn.execute(text("SELECT id, status, status_changed_at FROM proxies WHERE id LIKE 'm48-%'"))
    return {row.id: (row.status, row.status_changed_at) for row in rows}


def _totals(conn: Connection) -> dict[tuple[str, str], tuple[Any, ...]]:
    rows = conn.execute(
        text(
            "SELECT kind, entity_id, request_count, success_count, failure_count, latency_sum_ms, "
            "bytes_sent, bytes_received, by_address, encrypted_dns_blocked, updated_at FROM metric_totals"
        )
    )
    return {(row.kind, row.entity_id): tuple(row)[2:] for row in rows}


def _shape(conn: Connection, table: str) -> tuple[list[tuple[Any, ...]], list[str]]:
    """A table's columns as (name, type, nullable, default) in order, and its primary key; both empty without it."""
    columns = conn.execute(
        text(
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = :table ORDER BY ordinal_position"
        ),
        {"table": table},
    ).all()
    primary = conn.execute(
        text(
            "SELECT kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name "
            "WHERE tc.table_name = :table AND tc.constraint_type = 'PRIMARY KEY' ORDER BY kcu.ordinal_position"
        ),
        {"table": table},
    ).scalars().all()
    return [tuple(column) for column in columns], list(primary)


def _status_columns(conn: Connection) -> list[tuple[Any, ...]]:
    return [column for column in _shape(conn, "proxies")[0] if column[0].startswith("status")]


def _versions(conn: Connection) -> dict[str, list[int]]:
    """The version values of each 5.x versioned table's seeded rows."""
    return {
        table: conn.execute(text(f"SELECT DISTINCT version FROM {table} WHERE id LIKE 'm48-%'")).scalars().all()
        for table in VERSIONED
    }


def test_on_a_5x_database_it_fills_statuses_and_totals_keeps_the_version_columns_and_adds_no_trigger(
    at_047: Config, engine: Engine, migrated_database: str
) -> None:
    _seed(engine)
    before = tables(migrated_database)

    command.upgrade(at_047, "048")

    with engine.begin() as conn:
        assert _statuses(conn) == STATUSES
        assert _totals(conn) == TOTALS
        # 5.x still writes them.
        assert _versions(conn) == {table: [1] for table in VERSIONED}
        assert tables(migrated_database) - before == {"metric_totals", "metric_flushes"}
        assert conn.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")).all() == []

    # Down again restores 047's shape: nothing of the new, the version columns as they were.
    command.downgrade(at_047, "047")
    with engine.begin() as conn:
        assert _status_columns(conn) == []
        assert _shape(conn, "metric_totals") == _shape(conn, "metric_flushes") == ([], [])
        assert _versions(conn) == {table: [1] for table in VERSIONED}


def test_on_an_empty_database_it_adds_the_columns_and_tables_and_fills_nothing(at_047: Config, engine: Engine) -> None:
    command.upgrade(at_047, "048")

    with engine.begin() as conn:
        assert _status_columns(conn) == STATUS_COLUMNS
        assert _shape(conn, "metric_totals") == METRIC_TOTALS
        assert _shape(conn, "metric_flushes") == METRIC_FLUSHES
        assert conn.execute(text("SELECT count(*) FROM metric_totals")).scalar_one() == 0


def test_a_second_run_keeps_what_the_first_did_and_fills_what_5x_added_since(at_047: Config, engine: Engine) -> None:
    """A start cut short after the body ran and before Alembic recorded 048 runs the whole body again, as does a start after the way back to 5.0.11 set the version to 047."""
    _seed(engine)
    command.upgrade(at_047, "048")
    with engine.begin() as conn:
        # Meanwhile 5.x starts a drain, adds a proxy (its status the column's default) and flushes windows,
        # and 6.0's own metric_flushes table already holds a flush.
        conn.execute(
            text("UPDATE proxies SET metadata = :metadata WHERE id = 'm48-serving'"),
            {"metadata": f'{{"draining_started_at": "{LATER_DRAIN.isoformat()}"}}'},
        )
        conn.execute(
            text(
                "INSERT INTO proxies (id, host, port, protocol, connector_id, tags, metadata, created_at, updated_at) "
                "VALUES ('m48-late', '10.0.0.4', 8080, 'http', 'm48-connector', '[]', '{}', :t3, :t3)"
            ),
            {"t3": T3},
        )
        conn.execute(
            text(
                "INSERT INTO proxy_metrics (proxy_id, timestamp, request_count, success_count, failure_count, "
                "avg_latency_ms, bytes_sent, bytes_received, status, granularity) VALUES "
                "('m48-serving', :t3, 7, 7, 0, 10.0, 1, 1, 'healthy', 60), "
                "('m48-late', :t3, 2, 1, 1, 50.0, 3, 30, 'healthy', 60)"
            ),
            {"t3": T3},
        )
        conn.execute(text("INSERT INTO metric_flushes (message_id, flushed_at) VALUES ('1-0', :t3)"), {"t3": T3})
        conn.execute(text("UPDATE alembic_version SET version_num = '047'"))

    command.upgrade(at_047, "048")

    with engine.begin() as conn:
        assert _statuses(conn) == {**STATUSES, "m48-late": ("healthy", None)}
        assert _totals(conn) == {**TOTALS, ("proxy", "m48-late"): (2, 1, 1, 100.0, 3, 30, 0, 0, T3)}
        assert conn.execute(text("SELECT message_id FROM metric_flushes")).scalars().all() == ["1-0"]
        assert _status_columns(conn) == STATUS_COLUMNS


def test_the_status_fill_reaches_every_proxy_a_batch_at_a_time(
    at_047: Config, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each batch starts after the last id of the one before, and the pass ends at the first empty batch."""
    _seed(engine)
    command.upgrade(at_047, "048")
    with engine.begin() as conn:
        conn.execute(text("UPDATE proxies SET status = 'unknown', status_changed_at = NULL WHERE id LIKE 'm48-%'"))
    monkeypatch.setattr(migration, "STATUS_BATCH", 1)

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        migration.fill_statuses(conn)

    with engine.begin() as conn:
        assert _statuses(conn) == STATUSES


def test_a_statement_whose_lock_is_not_granted_in_time_is_tried_again(at_047: Config, engine: Engine) -> None:
    """A 5.x reader holds proxies: the column waits half a second, gives way, and is added once the reader is done."""
    blocker = engine.connect()
    reading = blocker.begin()
    blocker.execute(text("LOCK TABLE proxies IN ACCESS SHARE MODE"))
    release = threading.Timer(1.2, reading.commit)
    release.start()
    try:
        with structlog.testing.capture_logs() as logs:
            command.upgrade(at_047, "048")
    finally:
        release.join()
        blocker.close()

    waits = [(log["table"], log["attempt"]) for log in logs if log["event"] == RETRY_LOGGED]
    assert waits and waits[0] == ("proxies", 1)
    with engine.begin() as conn:
        assert _status_columns(conn) == STATUS_COLUMNS


# A migration after 048 that records the lock timeout its statements run under,
# on a branch of its own so it never stands in for the real next revision.
PROBE = '''"""Probe.

Revision ID: probe
Revises: 048
"""
from alembic import op

revision = "probe"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TABLE lock_timeout_probe AS SELECT current_setting('lock_timeout') AS value")


def downgrade() -> None:
    op.execute("DROP TABLE lock_timeout_probe")
'''


def test_a_later_migration_in_the_same_run_waits_for_its_locks_as_long_as_it_needs(
    at_047: Config, engine: Engine, tmp_path: Path
) -> None:
    """048's half-second lock timeout ends with 048."""
    scripts = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    (scripts / "versions" / "probe.py").write_text(PROBE)
    at_047.set_main_option("script_location", str(scripts))
    try:
        command.upgrade(at_047, "probe")
        with engine.begin() as conn:
            assert conn.execute(text("SELECT value FROM lock_timeout_probe")).scalar_one() == "0"
    finally:
        command.downgrade(at_047, "048")
        at_047.set_main_option("script_location", str(MIGRATIONS_DIR))


class FakeBind:
    """A connection whose statements fail with these SQLSTATEs, one per call, then succeed; each call on a fake clock."""

    def __init__(self, clock: "FakeClock", *codes: str) -> None:
        self.clock = clock
        self.codes = list(codes)
        self.calls: list[float] = []

    def execute(self, statement: Any, params: dict[str, Any]) -> str:
        self.calls.append(self.clock.now)
        if self.codes:
            raise OperationalError(str(statement), params, DriverError(self.codes.pop(0)))
        return "done"


class DriverError(Exception):
    """What psycopg2 raises: the SQLSTATE in ``pgcode``."""

    def __init__(self, pgcode: str) -> None:
        super().__init__(pgcode)
        self.pgcode = pgcode


class FakeClock:
    """The migration's ``time``: its pauses advance it."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(migration, "time", clock)
    monkeypatch.setattr(migration, "RETRY_PAUSE_SECONDS", (3.0, 3.0))
    monkeypatch.setattr(migration, "RETRY_BUDGET_SECONDS", 10.0)
    return clock


class TestRunStatement:
    def test_a_deadlock_victim_is_tried_again_after_a_pause(self, clock: FakeClock) -> None:
        bind = FakeBind(clock, "40P01")

        with structlog.testing.capture_logs() as logs:
            assert migration.run_statement(bind, "proxies", text("SELECT 1")) == "done"

        assert bind.calls == [0.0, 3.0]
        assert [(log["event"], log["table"], log["attempt"]) for log in logs] == [
            (RETRY_LOGGED, "proxies", 1)
        ]

    def test_any_other_error_fails_at_once(self, clock: FakeClock) -> None:
        bind = FakeBind(clock, "42P01")

        with pytest.raises(OperationalError):
            migration.run_statement(bind, "proxies", text("SELECT 1"))

        assert bind.calls == [0.0]

    def test_a_lock_still_refused_once_the_budget_is_spent_fails_the_migration(self, clock: FakeClock) -> None:
        bind = FakeBind(clock, *["55P03"] * 10)

        with pytest.raises(OperationalError):
            migration.run_statement(bind, "proxies", text("SELECT 1"))

        # Tried at 0, 3, 6 and 9 s; the try at 12 s fails past the 10 s budget.
        assert bind.calls == [0.0, 3.0, 6.0, 9.0, 12.0]
