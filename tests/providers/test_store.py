# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the provider store: custom descriptors applied to an injected registry, diff-only."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core.config import Settings
from api.db.repository import ProviderDescriptorRepository
from api.models.provider import ProviderRecord
from api.providers.registry import ProviderRegistry, build_registry, registry_from_settings
from api.providers.store import ProviderStore


def _spec(provider_id: str = "acme", name: str = "Acme") -> dict[str, Any]:
    return {
        "id": provider_id,
        "name": name,
        "credential_fields": [
            {"key": "username", "label": "User", "required": True},
            {"key": "password", "label": "Pass", "type": "password", "secret": True, "required": True},
        ],
        "connector_fields": [{"key": "num_proxies", "label": "N", "type": "number", "default": 1, "min": 1}],
        "proxy_types": [
            {
                "key": "res", "label": "Res", "mode": "session", "host": "gw.acme.test", "port": 9000,
                "username": "{credential.username}-{session_id}", "password": "{credential.password}",
            }
        ],
    }


@pytest.fixture
def registry() -> ProviderRegistry:
    return build_registry(include_plugins=False)


class TestApplySpecs:
    """What a gateway does with the global section's descriptors, and what every sync ends in."""

    def test_applies_only_a_change(self, registry: ProviderRegistry) -> None:
        store = ProviderStore(registry, None)
        with patch("api.providers.store.logger") as log:
            assert store.apply_specs([_spec()])
            ptype = registry.get("acme")
            assert ptype is not None and ptype.source == "custom"
            # The same documents again: not parsed, not logged, the registration untouched.
            assert not store.apply_specs([_spec()])
            assert registry.get("acme") is ptype
            assert len(log.info.call_args_list) == 1
        assert store.apply_specs([_spec(name="Acme v2")]) and registry.get("acme").name == "Acme v2"  # type: ignore[union-attr]
        assert store.apply_specs([]) and registry.get("acme") is None
        assert not store.apply_specs([])

    def test_a_bad_document_is_said_once_and_skipped(self, registry: ProviderRegistry) -> None:
        store = ProviderStore(registry, None)
        broken = {"id": "broken", "name": "Broken"}
        with patch("api.providers.store.logger") as log:
            assert store.apply_specs([broken, _spec()])
            assert not store.apply_specs([broken, _spec()])
            assert [c.kwargs["provider_id"] for c in log.error.call_args_list] == ["broken"]
        assert registry.get("broken") is None and registry.get("acme") is not None

    def test_a_document_cannot_shadow_a_built_in(self, registry: ProviderRegistry) -> None:
        store = ProviderStore(registry, None)
        assert not store.apply_specs([_spec("oxylabs")])
        assert registry.get("oxylabs").source == "builtin"  # type: ignore[union-attr]

    async def test_without_postgres_there_is_nothing_to_read(self, registry: ProviderRegistry) -> None:
        assert not await ProviderStore(registry, None).sync_all()


class TestSyncAll:
    async def test_reads_the_enabled_rows(
        self, registry: ProviderRegistry, db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        repo = ProviderDescriptorRepository(db_session)
        await repo.create(ProviderRecord(id="acme", name="Acme", spec=_spec()))
        await repo.create(ProviderRecord(id="off", name="Off", spec=_spec("off"), enabled=False))
        await db_session.commit()
        store = ProviderStore(registry, db_session_factory)
        assert await store.sync_all()
        assert registry.get("acme") is not None and registry.get("off") is None
        # A read that finds no descriptor changed applies nothing.
        assert not await store.sync_all()
        await repo.delete("acme")
        await db_session.commit()
        assert await store.sync_all() and registry.get("acme") is None


class TestRegistryFromSettings:
    def test_each_process_builds_its_own(self, tmp_path: Path) -> None:
        (tmp_path / "local.yaml").write_text(json.dumps(_spec("local", "Local")))  # JSON is YAML
        settings = Settings(providers_dir=str(tmp_path), provider_egress_allow_http=True)  # type: ignore[call-arg]
        first, second = registry_from_settings(settings), registry_from_settings(settings)
        assert first is not second
        assert first.get("local") is not None and first.get("oxylabs") is not None
        assert first.runtime.egress_policy.allow_http
