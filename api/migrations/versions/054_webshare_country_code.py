# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Webshare connectors list their countries under ``country_code``, as every other built-in provider does.

The Webshare descriptor filtered its list through a free-text ``country_codes`` field, which Octoprox did not know
as the connector's country list: routing, the Countries page and reserved or excluded countries saw a connector
listing none. The field is now the countries dropdown under ``country_code``, and each listed country is read from
the vendor's list on its own. This moves each stored value to the new key as a list of upper-case codes (UK as GB,
each once), and drops an empty one. A connector without the old key is left alone, so running it again changes nothing.

The downgrade moves the list back to ``country_codes`` as the comma-separated text the older release reads.

Revision ID: 054
Revises: 053
Create Date: 2026-11-02
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Connection

revision: str = "054"
down_revision: str | None = "053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KEY = "country_codes"
NEW_KEY = "country_code"
# The free-text field took any spelling; the countries field takes GB, not UK.
ALIASES = {"UK": "GB"}


def codes(value: Any) -> list[str]:
    """The country codes a stored value holds (one code, a comma-separated string or a list), aliases mapped, once each."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    found: list[str] = []
    for item in items:
        code = str(item).strip().upper()
        code = ALIASES.get(code, code)
        if code and code not in found:
            found.append(code)
    return found


def _webshare_connectors(connection: Connection) -> list[tuple[str, dict[str, Any]]]:
    rows = connection.execute(
        sa.text(
            "SELECT c.id, c.config FROM connectors c JOIN credentials cr ON cr.id = c.credential_id "
            "WHERE cr.type = 'webshare'"
        )
    ).fetchall()
    return [(row.id, row.config if isinstance(row.config, dict) else json.loads(row.config or "{}")) for row in rows]


def _save(connection: Connection, connector_id: str, config: dict[str, Any]) -> None:
    connection.execute(
        sa.text("UPDATE connectors SET config = CAST(:config AS JSON) WHERE id = :id"),
        {"config": json.dumps(config), "id": connector_id},
    )


def upgrade() -> None:
    connection = op.get_bind()
    for connector_id, config in _webshare_connectors(connection):
        if OLD_KEY not in config:
            continue
        listed = codes(config.pop(OLD_KEY))
        if listed and not config.get(NEW_KEY):
            config[NEW_KEY] = listed
        _save(connection, connector_id, config)


def downgrade() -> None:
    connection = op.get_bind()
    for connector_id, config in _webshare_connectors(connection):
        if NEW_KEY not in config:
            continue
        listed = codes(config.pop(NEW_KEY))
        if listed:
            config[OLD_KEY] = ",".join(listed)
        _save(connection, connector_id, config)
