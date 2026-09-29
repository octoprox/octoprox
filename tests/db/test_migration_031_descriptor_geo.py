# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Migration 031 rewrites stored descriptors from the connector country field to ``geo.country``."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from api.core.config import Settings
from api.db.migrations import MIGRATIONS_DIR
from api.providers.sdk.loader import descriptor_from_dict
from api.providers.sdk.templating import country_field_key

_MODULE_PATH = Path(MIGRATIONS_DIR) / "versions" / "031_descriptor_geo_namespace.py"


def _migration():
    spec = importlib.util.spec_from_file_location("migration_031", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alembic(url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


OLD_SPEC = {
    "id": "acme",
    "name": "Acme",
    "credential_fields": [
        {"key": "username", "label": "U", "required": True},
        {"key": "password", "label": "P", "type": "password", "secret": True, "required": True},
    ],
    "connector_fields": [
        {"key": "proxy_type", "label": "Type", "type": "select", "default": "res", "options": [{"value": "res", "label": "Res"}, {"value": "isp", "label": "ISP"}]},
        {"key": "country_code", "label": "Countries", "type": "select", "options_preset": "countries"},
        {"key": "num_proxies", "label": "N", "type": "number", "default": 1},
        {"key": "zone", "label": "Zone", "type": "select", "options_from": "zones"},
    ],
    "proxy_type_field": "connector.proxy_type",
    "options": {
        # Option sources keep reading the connector field: there the list is the right value.
        "zones": {"call": {"url": "https://api.acme.example/zones", "params": {"country": "{connector.country_code}"}}, "items": "@", "value": "id", "label": "id"},
    },
    "proxy_types": [
        {
            "key": "res", "label": "Res", "mode": "session",
            "host": "{connector.country_code|lower|or:any}.gw.acme.example", "port": 9000,
            "username": {
                "separator": "-",
                "parts": [
                    {"text": "{credential.username}"},
                    {"text": "cc-{connector.country_code|lower}", "when": {"field": "connector.country_code"}},
                    {"text": "sid-{session_id}"},
                ],
            },
            "password": "{credential.password}",
            "metadata": {"country_code": "{connector.country_code}", "zone": "{connector.zone}"},
        },
        {
            "key": "isp", "label": "ISP", "mode": "port", "port_strategy": "fixed",
            "host": "gw.acme.example", "port": 9001,
            "username": {
                "separator": "-",
                "parts": [
                    {"text": "{credential.username}"},
                    {"text": "ip-{discovered_ip}", "when": {"field": "discovered_ip"}},
                    {"text": "country-{connector.country_code|lower}", "when": [{"field": "connector.country_code"}, {"field": "discovered_ip", "negate": True}]},
                ],
            },
            "password": "{credential.password}",
            "discovery": {"url": "https://echo.acme.example/ip", "ip_path": "ip"},
            "known_ips": {"call": {"url": "https://api.acme.example/ips", "params": {"country": "{connector.country_code|lower}"}}, "items": "@", "ip": "ip"},
        },
    ],
}


class TestRewrite:
    def test_rewrites_proxy_types_only(self) -> None:
        rewritten, changed = _migration().rewrite_spec(OLD_SPEC)
        assert changed
        res, isp = rewritten["proxy_types"]
        assert res["host"] == "{geo.country|lower|or:any}.gw.acme.example"
        assert res["username"]["parts"][1] == {"text": "cc-{geo.country|lower}", "when": {"field": "geo.country"}}
        assert res["metadata"] == {"country_code": "{geo.country}", "zone": "{connector.zone}"}
        assert isp["username"]["parts"][2]["text"] == "country-{geo.country|lower}"
        assert isp["username"]["parts"][2]["when"] == [{"field": "geo.country"}, {"field": "discovered_ip", "negate": True}]
        assert isp["known_ips"]["call"]["params"] == {"country": "{geo.country|lower}"}
        # Untouched: the options source and everything that is not the country field.
        assert rewritten["options"] == OLD_SPEC["options"]
        assert OLD_SPEC["proxy_types"][0]["host"].startswith("{connector.")  # input not mutated
        # The result is a valid descriptor that geo-targets through the country field, and the rewrite is idempotent.
        descriptor = descriptor_from_dict(rewritten)
        assert country_field_key(descriptor, descriptor.get_proxy_type("res")) == "country_code"
        assert _migration().rewrite_spec(rewritten) == (rewritten, False)

    def test_descriptor_without_a_country_field_is_left_alone(self) -> None:
        spec = {**OLD_SPEC, "connector_fields": [{"key": "num_proxies", "label": "N", "type": "number"}]}
        assert _migration().rewrite_spec(spec) == (spec, False)


@pytest.mark.usefixtures("db_session")  # migrations are at head and the tables are truncated afterwards
def test_stored_descriptors_are_rewritten(test_settings: Settings) -> None:
    engine = create_engine(test_settings.database_url_sync)
    cfg = _alembic(test_settings.database_url_sync)
    ts = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    plain = {**OLD_SPEC, "id": "plain", "connector_fields": [{"key": "num_proxies", "label": "N", "type": "number"}]}
    try:
        command.downgrade(cfg, "030")
        with engine.begin() as conn:
            for spec in (OLD_SPEC, plain):
                conn.execute(
                    text(
                        "INSERT INTO provider_descriptors (id, name, spec, enabled, version, created_at, updated_at) "
                        "VALUES (:id, :name, CAST(:spec AS JSON), true, 3, :ts, :ts)"
                    ),
                    {"id": spec["id"], "name": spec["name"], "spec": json.dumps(spec), "ts": ts},
                )
        command.upgrade(cfg, "031")
        with engine.begin() as conn:
            rows = {r.id: (r.spec if isinstance(r.spec, dict) else json.loads(r.spec), r.version) for r in conn.execute(text("SELECT id, spec, version FROM provider_descriptors"))}
        acme, version = rows["acme"]
        assert acme["proxy_types"][0]["username"]["parts"][1]["text"] == "cc-{geo.country|lower}"
        assert version == 3  # nobody authored this change
        assert rows["plain"][0] == plain
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM provider_descriptors WHERE id IN ('acme', 'plain')"))
