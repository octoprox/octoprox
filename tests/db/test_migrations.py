# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""run_migrations: one process at a time, a plain upgrade recorded in ``alembic_version``, and its answer.

Each test that changes the version table does so in a database of its own, created in the test container and dropped after.
"""

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import structlog
from alembic import command
from alembic.util import CommandError
from sqlalchemy import Engine, create_engine, make_url, text

from api.db import models
from api.db.migrations import alembic_config, run_migrations
from tests.conftest import HEAD, tables

LOCK_KEY = "hashtext('octoprox.migrations')"


@pytest.fixture
def autocommit(migrated_database: str) -> Iterator[Engine]:
    engine = create_engine(migrated_database, isolation_level="AUTOCOMMIT")
    yield engine
    engine.dispose()


@pytest.fixture
def new_database(migrated_database: str, autocommit: Engine) -> Iterator[str]:
    """The sync URL of an empty database in the test container, dropped after the test."""
    name = f"migrations_{uuid4().hex[:12]}"
    with autocommit.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    try:
        yield make_url(migrated_database).set(database=name).render_as_string(hide_password=False)
    finally:
        with autocommit.connect() as conn:
            conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))


def version(url: str) -> str | None:
    """The revision ``alembic_version`` records, None while the table is missing."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            if not conn.execute(text("SELECT to_regclass('alembic_version')")).scalar():
                return None
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()


def set_version(url: str, revision: str) -> None:
    """Record ``revision`` in ``alembic_version``, as the way back to 5.0.11 does with 047."""
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :revision"), {"revision": revision})
    finally:
        engine.dispose()


def test_the_alembic_config_keeps_a_url_s_percent_signs() -> None:
    """The URL goes through ConfigParser interpolation, and a password quoted into it carries percent signs."""
    url = "postgresql+psycopg2://octoprox:p%40ss%25word@db:5432/octoprox"

    assert alembic_config(url).get_main_option("sqlalchemy.url") == url


def test_a_waiting_runner_holds_no_snapshot_and_migrates_once_the_lock_is_free(
    migrated_database: str, autocommit: Engine
) -> None:
    """A backend blocked in ``pg_advisory_lock`` would hold a snapshot, which a ``CREATE INDEX CONCURRENTLY`` waits for."""
    outcomes: list[bool | Exception] = []

    def migrate() -> None:
        try:
            outcomes.append(run_migrations(migrated_database))
        except Exception as exc:  # handed to the test's thread
            outcomes.append(exc)

    with autocommit.connect() as holder, autocommit.connect() as builder, patch("api.db.migrations.logger") as logger:
        holder_pid = holder.execute(text(f"SELECT pg_advisory_lock({LOCK_KEY}), pg_backend_pid()")).one()[1]
        runner = threading.Thread(target=migrate)
        runner.start()
        try:
            _wait_for(lambda: logger.info.call_args_list != [])
            builder.execute(text("SET statement_timeout = '10s'"))
            builder.execute(text("CREATE TABLE migration_lock_probe (x int)"))
            builder.execute(text("CREATE INDEX CONCURRENTLY migration_lock_probe_x ON migration_lock_probe (x)"))
            assert runner.is_alive()  # still waiting: the build did not need it gone
            logger.info.assert_any_call("Waiting for the migration lock", holder_pid=holder_pid)
        finally:
            holder.execute(text(f"SELECT pg_advisory_unlock({LOCK_KEY})"))
            runner.join(timeout=30)
            builder.execute(text("DROP TABLE IF EXISTS migration_lock_probe"))
        # Another process brought the schema to head: this one applied nothing.
        assert not runner.is_alive() and outcomes == [False]
        assert _was_logged(logger, "Database migrations complete")
        held = builder.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 1 "
                f"AND ((classid::bigint << 32) | objid::bigint) = {LOCK_KEY}"
            )
        ).scalar_one()
        assert held == 0


def test_a_new_database_gets_the_tables_6_0_maps_and_records_its_revision_in_alembic_version(new_database: str) -> None:
    assert run_migrations(new_database) is True
    assert version(new_database) == HEAD
    assert tables(new_database) == set(models.Base.metadata.tables) | {"alembic_version"}

    assert run_migrations(new_database) is False
    assert version(new_database) == HEAD


def test_a_048_database_set_back_to_047_runs_048_again_cleanly(new_database: str) -> None:
    """The way back to 5.0.11 sets the version to 047 and keeps what 048 added; the next 6.0 start runs 048 over it."""
    run_migrations(new_database)
    set_version(new_database, "047")

    with structlog.testing.capture_logs() as logs:
        assert run_migrations(new_database) is True

    assert version(new_database) == HEAD
    assert tables(new_database) == set(models.Base.metadata.tables) | {"alembic_version"}
    assert [(log["previous"], log["revision"]) for log in logs if log["event"] == "Database migrations complete"] == [
        ("047", HEAD)
    ]


def test_a_revision_this_release_does_not_know_fails_the_start(new_database: str) -> None:
    run_migrations(new_database)
    set_version(new_database, "999")

    with pytest.raises(CommandError, match="999"):
        run_migrations(new_database)


def test_autogenerate_leaves_alone_the_version_columns_only_5x_writes(new_database: str) -> None:
    """It would propose dropping them; a table and a version column 5.x has nothing to do with are still compared."""
    run_migrations(new_database)
    engine = create_engine(new_database)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE autogenerate_probe (id int PRIMARY KEY)"))
            conn.execute(text("ALTER TABLE users ADD COLUMN version integer"))
    finally:
        engine.dispose()
    diffs: list[Any] = []

    def capture(_context: Any, _revision: Any, directives: list[Any]) -> None:
        diffs.extend(directives[0].upgrade_ops.as_diffs())
        directives[:] = []  # write no script

    command.revision(alembic_config(new_database), autogenerate=True, process_revision_directives=capture)

    removed_tables = {diff[1].name for diff in diffs if diff[0] == "remove_table"}
    removed_columns = {(diff[2], diff[3].name) for diff in diffs if diff[0] == "remove_column"}
    assert removed_tables == {"autogenerate_probe"}
    assert ("users", "version") in removed_columns
    assert not {table for table, column in removed_columns if column == "version"} - {"users"}


def _wait_for(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def _was_logged(logger: MagicMock, event: str) -> bool:
    return any(call.args[:1] == (event,) for call in logger.info.call_args_list)
