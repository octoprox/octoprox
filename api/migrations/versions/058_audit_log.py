# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""One audit log for every user-driven change, and the install's admin-edited settings.

* ``audit_log``: one row per change, written in the change's own transaction: who, what, when, and the field diff
  ``{field: [before, after]}``, secrets masked. ``ix_audit_log_entity`` serves one entity's history newest first by a
  backward scan, ``ix_audit_log_created_at`` the install-wide list and the retention delete. Audit volume is small
  next to the observation log, so the page splits migration 049 avoided do not matter here.
* ``system_settings``: one row, id 1, written by the first save; no row means the defaults (365 days of audit
  history, no license key).
* ``provider_audit_log`` moves into ``audit_log`` and is dropped. Each entry keeps its id, time, actor and action;
  the spec snapshot it carried becomes the diff against the provider's previous entry, one top-level key at a time
  (the logos masked), and the egress hosts added and removed go in its details.

Each step is safe to run again. 6.6.x refuses this revision at start, so the way back is ``alembic downgrade 057``
with this release's image first: it recreates ``provider_audit_log`` as 021 made it and copies the provider entries
back without their spec snapshots (6.6.x never shows them), then drops both tables.

Revision ID: 058
Revises: 057
Create Date: 2026-11-10
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Connection

revision: str = "058"
down_revision: str | None = "057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# How the audit log writes a masked value that is set (api.models.audit.MASK, frozen here).
MASK = "********"
# Descriptor keys too large to keep in every entry: written as set or not.
MASKED_SPEC_KEYS = frozenset({"logo", "logo_dark"})
# The widths of the moved entries' bounded text columns.
NAME_CHARS = 255
ACTOR_CHARS = 255

CREATE_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS audit_log (
    id varchar(36) PRIMARY KEY,
    created_at timestamp NOT NULL,
    action varchar(32) NOT NULL,
    entity_type varchar(32) NOT NULL,
    entity_id varchar(64) NOT NULL DEFAULT '',
    entity_name varchar(255) NOT NULL DEFAULT '',
    project_id varchar(36),
    actor_id varchar(36),
    actor varchar(255) NOT NULL DEFAULT '',
    request_id varchar(128),
    changes json NOT NULL DEFAULT '{}',
    details json NOT NULL DEFAULT '{}'
)
"""
CREATE_SYSTEM_SETTINGS = """
CREATE TABLE IF NOT EXISTS system_settings (
    id integer PRIMARY KEY,
    audit_retention_days integer NOT NULL DEFAULT 365,
    license_key text,
    CONSTRAINT ck_system_settings_single_row CHECK (id = 1)
)
"""
INSERT_ENTRY = sa.text(
    "INSERT INTO audit_log (id, created_at, action, entity_type, entity_id, entity_name, actor, changes, details) "
    "VALUES (:id, :created_at, :action, 'provider', :entity_id, :entity_name, :actor, "
    "CAST(:changes AS JSON), CAST(:details AS JSON)) "
    "ON CONFLICT (id) DO NOTHING"
)


def _loaded(value: Any) -> Any:
    """A json column's value as Python, whether the driver parsed it or not."""
    return json.loads(value) if isinstance(value, str) else value


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str | list | dict) and not value)


def _diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, list[Any]]:
    """The keys of two specs whose values differ, as ``spec.<key>``: the audit log's rule (api.models.audit), frozen."""
    changes: dict[str, list[Any]] = {}
    for key in dict.fromkeys([*old, *new]):
        was, now = old.get(key), new.get(key)
        if was == now or (_empty(was) and _empty(now)):
            continue
        if key in MASKED_SPEC_KEYS:
            was, now = (None if _empty(was) else MASK), (None if _empty(now) else MASK)
        changes[f"spec.{key}"] = [was, now]
    return changes


def _hosts(old: list[str], new: list[str]) -> dict[str, list[str]]:
    """The egress hosts added and removed between two consecutive entries, each list only when not empty."""
    details: dict[str, list[str]] = {}
    if added := sorted(set(new) - set(old)):
        details["hosts_added"] = added
    if removed := sorted(set(old) - set(new)):
        details["hosts_removed"] = removed
    return details


def _provider_entries(connection: Connection) -> list[dict[str, Any]]:
    """Each ``provider_audit_log`` row as an audit log entry, its spec compared with the provider's previous row's.

    A create or an import compares with nothing, a delete leaves nothing behind it, and a row without a snapshot
    changes nothing.
    """
    rows = connection.execute(
        sa.text(
            "SELECT id, provider_id, action, actor, egress_hosts, spec, created_at FROM provider_audit_log "
            "ORDER BY provider_id, created_at, id"
        )
    ).fetchall()
    entries: list[dict[str, Any]] = []
    provider_id: str | None = None
    previous: dict[str, Any] = {}
    previous_hosts: list[str] = []
    for row in rows:
        if row.provider_id != provider_id or row.action in ("created", "imported"):
            previous, previous_hosts = {}, []
        provider_id = row.provider_id
        spec = _loaded(row.spec)
        hosts = [str(host) for host in _loaded(row.egress_hosts) or []]
        if row.action == "deleted":
            changes = _diff(spec if isinstance(spec, dict) else previous, {})
        elif isinstance(spec, dict):
            changes = _diff(previous, spec)
        else:
            changes = {}
        named = spec if isinstance(spec, dict) else previous
        entries.append(
            {
                "id": row.id,
                "created_at": row.created_at,
                "action": row.action,
                "entity_id": row.provider_id,
                "entity_name": str(named.get("name") or row.provider_id)[:NAME_CHARS],
                "actor": (row.actor or "")[:ACTOR_CHARS],
                "changes": json.dumps(changes),
                "details": json.dumps(_hosts(previous_hosts, hosts)),
            }
        )
        if row.action == "deleted":
            previous, previous_hosts = {}, []
        else:
            previous = spec if isinstance(spec, dict) else previous
            previous_hosts = hosts
    return entries


def upgrade() -> None:
    op.execute(CREATE_AUDIT_LOG)
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_log_entity ON audit_log (entity_type, entity_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_log_created_at ON audit_log (created_at)")
    op.execute(CREATE_SYSTEM_SETTINGS)

    connection = op.get_bind()
    if connection.execute(sa.text("SELECT to_regclass('provider_audit_log')")).scalar() is None:
        return
    entries = _provider_entries(connection)
    if entries:
        connection.execute(INSERT_ENTRY, entries)
    op.execute("DROP TABLE IF EXISTS provider_audit_log")


def downgrade() -> None:
    op.create_table(
        "provider_audit_log",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("egress_hosts", sa.JSON(), nullable=False),
        sa.Column("hosts_changed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("spec", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_provider_audit_log_provider_id", "provider_audit_log", ["provider_id"])
    op.create_index("ix_provider_audit_log_created_at", "provider_audit_log", ["created_at"])
    op.execute(
        "INSERT INTO provider_audit_log (id, provider_id, action, actor, egress_hosts, hosts_changed, spec, created_at) "
        "SELECT id, entity_id, action, actor, '[]', "
        "(details -> 'hosts_added') IS NOT NULL OR (details -> 'hosts_removed') IS NOT NULL, NULL, created_at "
        "FROM audit_log WHERE entity_type = 'provider' AND action IN ('created', 'imported', 'updated', 'deleted')"
    )
    op.execute("DROP TABLE IF EXISTS audit_log")
    op.execute("DROP TABLE IF EXISTS system_settings")
