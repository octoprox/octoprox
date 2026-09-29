# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Move stored provider descriptors from the connector country field to ``geo.country``.

Templates used to read the targeted country through the connector's country
field (``{connector.country_code}``), which the engine quietly narrowed to
one country per slot group or per request. That narrowing is gone: the
field is the list the admin configured, and the place a render targets is
the ``geo`` namespace. The shipped descriptors were rewritten in the
repository; this rewrites the admin-authored ones in ``provider_descriptors``
the same way, so nothing has to be edited by hand.

Only the proxy types are touched: username, password, host and port
templates, their ``when`` conditions, metadata values and the known-IP
lookup call. Option sources and validation calls keep reading the connector
field, since there the whole list is the right value. The rewrite is exact
(the descriptor's own country field key), idempotent, and skipped for
descriptors without a country field. Versions are left alone: no one
authored this change.

Revision ID: 031
Revises: 030
Create Date: 2026-09-29
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = '031'
down_revision: str | None = '030'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def country_field_key(spec: dict[str, Any]) -> str | None:
    """The connector field a descriptor holds its country list in, if any."""
    for field in spec.get("connector_fields") or []:
        if isinstance(field, dict) and (field.get("type") == "country" or field.get("options_preset") == "countries"):
            key = field.get("key")
            return str(key) if key else None
    return None


def rewrite_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return the spec with its proxy types reading ``geo.country``, and whether anything changed."""
    key = country_field_key(spec)
    if key is None or not isinstance(spec.get("proxy_types"), list):
        return spec, False
    placeholder = re.compile(r"\{connector\." + re.escape(key) + r"((?:\|[^}]*)?)\}")
    field_path = f"connector.{key}"
    changed = False

    def text(value: Any) -> Any:
        nonlocal changed
        if not isinstance(value, str):
            return value
        new = placeholder.sub(r"{geo.country\1}", value)
        if new != value:
            changed = True
        return new

    def condition(value: Any) -> Any:
        nonlocal changed
        if isinstance(value, list):
            return [condition(v) for v in value]
        if isinstance(value, dict) and value.get("field") == field_path:
            changed = True
            return {**value, "field": "geo.country"}
        return value

    def template(value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("parts"), list):
            parts = []
            for part in value["parts"]:
                if isinstance(part, dict):
                    part = {**part, "text": text(part.get("text"))}
                    if "when" in part:
                        part["when"] = condition(part["when"])
                parts.append(part)
            return {**value, "parts": parts}
        return text(value)

    def strings(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strings(v) for k, v in value.items()}
        if isinstance(value, list):
            return [strings(v) for v in value]
        return text(value)

    proxy_types = []
    for ptype in spec["proxy_types"]:
        if not isinstance(ptype, dict):
            proxy_types.append(ptype)
            continue
        ptype = dict(ptype)
        for name in ("username", "password", "host", "port"):
            if name in ptype:
                ptype[name] = template(ptype[name])
        if isinstance(ptype.get("metadata"), dict):
            ptype["metadata"] = strings(ptype["metadata"])
        if isinstance(ptype.get("known_ips"), dict) and isinstance(ptype["known_ips"].get("call"), dict):
            ptype["known_ips"] = {**ptype["known_ips"], "call": strings(ptype["known_ips"]["call"])}
        proxy_types.append(ptype)
    return {**spec, "proxy_types": proxy_types}, changed


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, spec FROM provider_descriptors")).fetchall()
    for row in rows:
        spec = row.spec if isinstance(row.spec, dict) else json.loads(row.spec or "{}")
        rewritten, changed = rewrite_spec(spec)
        if changed:
            connection.execute(
                sa.text("UPDATE provider_descriptors SET spec = CAST(:spec AS JSON) WHERE id = :id"),
                {"spec": json.dumps(rewritten), "id": row.id},
            )


def downgrade() -> None:
    # The old spelling relied on engine behaviour that no longer exists; there is nothing to go back to.
    pass
