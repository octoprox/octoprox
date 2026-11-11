# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 058 adds the audit log and the system settings, and moves the provider audit log into the audit log."""

import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, make_url, text

from api.db.migrations import alembic_config

LOGO = "data:image/svg+xml;base64,PHN2Zy8+"
CREATED: dict[str, Any] = {
    "id": "acme",
    "name": "Acme Proxies",
    "logo": LOGO,
    "credential_fields": [{"key": "api_key", "secret": True}],
    "options": {"regions": {"call": {"url": "https://api.acme.test/regions"}}},
}
UPDATED: dict[str, Any] = {
    **CREATED,
    "options": {
        "regions": {"call": {"url": "https://api.acme.test/regions"}, "value": "code"},
        "extra": {"call": {"url": "https://other.acme.test/x"}},
    },
}


@pytest.fixture
def database(migrated_database: str) -> Iterator[str]:
    """The sync URL of a database of its own at revision 057, dropped after the test."""
    admin = create_engine(migrated_database, isolation_level="AUTOCOMMIT")
    name = f"migration_058_{uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {name}"))
    url = make_url(migrated_database).set(database=name).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "057")
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


def _tables(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())


def _entries(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, action, entity_type, entity_id, entity_name, actor, changes::text AS changes, "
                "details::text AS details FROM audit_log ORDER BY created_at"
            )
        ).mappings()
        return [{**row, "changes": json.loads(row["changes"]), "details": json.loads(row["details"])} for row in rows]


def test_the_provider_audit_log_moves_into_the_audit_log_as_diffs_and_back(database: str) -> None:
    engine = create_engine(database)
    try:
        with engine.begin() as conn:
            for entry_id, action, hosts, spec, minute in (
                ("e1", "created", ["api.acme.test"], CREATED, 1),
                ("e2", "updated", ["api.acme.test", "other.acme.test"], UPDATED, 2),
                ("e3", "deleted", ["api.acme.test", "other.acme.test"], UPDATED, 3),
            ):
                conn.execute(
                    text(
                        "INSERT INTO provider_audit_log (id, provider_id, action, actor, egress_hosts, hosts_changed, "
                        "spec, created_at) VALUES (:id, 'acme', :action, 'ivan', CAST(:hosts AS JSON), true, "
                        "CAST(:spec AS JSON), :created_at)"
                    ),
                    {
                        "id": entry_id,
                        "action": action,
                        "hosts": json.dumps(hosts),
                        "spec": json.dumps(spec),
                        "created_at": f"2026-11-01 10:0{minute}:00",
                    },
                )

        command.upgrade(alembic_config(database), "058")

        assert {"audit_log", "system_settings"} <= _tables(engine)
        assert "provider_audit_log" not in _tables(engine)
        created, updated, deleted = _entries(engine)
        common = {"entity_type": "provider", "entity_id": "acme", "entity_name": "Acme Proxies", "actor": "ivan"}
        assert created == {
            **common,
            "id": "e1",
            "action": "created",
            "changes": {
                "spec.id": [None, "acme"],
                "spec.name": [None, "Acme Proxies"],
                "spec.logo": [None, "********"],
                "spec.credential_fields": [None, CREATED["credential_fields"]],
                "spec.options": [None, CREATED["options"]],
            },
            "details": {"hosts_added": ["api.acme.test"]},
        }
        assert updated == {
            **common,
            "id": "e2",
            "action": "updated",
            "changes": {"spec.options": [CREATED["options"], UPDATED["options"]]},
            "details": {"hosts_added": ["other.acme.test"]},
        }
        assert deleted["action"] == "deleted" and deleted["details"] == {}
        assert deleted["changes"]["spec.logo"] == ["********", None]
        assert deleted["changes"]["spec.options"] == [UPDATED["options"], None]
        with engine.connect() as conn:
            assert LOGO not in conn.execute(text("SELECT string_agg(changes::text, '') FROM audit_log")).scalar_one()

        # The way back to an older release sets the version back: the next start runs 058 again, adding nothing.
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = '057'"))
        command.upgrade(alembic_config(database), "058")
        assert [entry["id"] for entry in _entries(engine)] == ["e1", "e2", "e3"]

        command.downgrade(alembic_config(database), "057")
        assert not {"audit_log", "system_settings"} & _tables(engine)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, provider_id, action, actor, egress_hosts::text, hosts_changed, spec "
                    "FROM provider_audit_log ORDER BY created_at"
                )
            ).all()
        assert [tuple(row) for row in rows] == [
            ("e1", "acme", "created", "ivan", "[]", True, None),
            ("e2", "acme", "updated", "ivan", "[]", True, None),
            ("e3", "acme", "deleted", "ivan", "[]", False, None),
        ]
    finally:
        engine.dispose()


def test_a_database_without_provider_history_gets_empty_tables(database: str) -> None:
    engine = create_engine(database)
    try:
        command.upgrade(alembic_config(database), "058")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM system_settings")).scalar_one() == 0
            indexes = set(
                conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'audit_log'")).scalars()
            )
        assert {"ix_audit_log_entity", "ix_audit_log_created_at"} <= indexes
    finally:
        engine.dispose()
