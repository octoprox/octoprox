# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the feed compilers against the migrated Postgres test database, rows written through the repositories.

The full compile runs over a real provider registry and the controller's real stores, which record what each compile
applies; the api's previews compile over the same registry, with no store.
"""

import time
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.core.config import Settings
from api.db.geo_repository import GeoDatabaseRepository, GeoSettingsRepository
from api.db.openvpn_repository import OpenVpnPeerRepository, OpenVpnSettingsRepository
from api.db.redis import QUARANTINE_KEY, TRAFFIC_BLOCKED_KEY, RedisClient
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    ProjectRepository,
    ProviderDescriptorRepository,
    ProxyRepository,
)
from api.db.wireguard_repository import WireGuardPeerRepository, WireGuardSettingsRepository
from api.feed.compiler import (
    DISPLAY_ONLY_METADATA,
    CompiledFeed,
    FeedCompiler,
    FullFeedCompiler,
)
from api.feed.models import (
    GLOBAL_SECTION,
    RUNTIME_SECTION,
    ConnectorSection,
    GlobalSection,
    RuntimeSection,
    Section,
    connector_section,
)
from api.geo.models import GeoDatabaseRecord, GeoDatabaseSource, GeoSettings
from api.geo.settings import GeoSettingsStore
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.openvpn import OpenVpnPeer, OpenVpnServerSettings
from api.models.project import Project
from api.models.provider import ProviderRecord
from api.models.proxy import Proxy, ProxyStatus
from api.models.wireguard import WireGuardPeer, WireGuardServerSettings
from api.providers.registry import ProviderRegistry, build_registry
from api.providers.sdk.loader import descriptor_from_dict
from api.providers.store import ProviderStore
from api.routing.router import Router
from api.routing.snapshot import RoutingSnapshot
from tests.providers.test_registry import fail_to_build

CREATED = datetime(2026, 10, 1, 9, 30)
# What the install runs with while no geo settings row is saved.
GEO_DEFAULTS = GeoSettings(echo_url="https://echo.defaults.test/ip")
# The install's default strategy: not the Project model's round_robin, so a test sees which one serves.
DEFAULT_STRATEGY = "health_based"
# An admin-authored descriptor whose connectors name their countries in a
# field of their own ("locations"), which Connector.countries cannot know.
ACME: dict[str, Any] = {
    "id": "acme",
    "name": "Acme",
    "credential_fields": [
        {"key": "username", "label": "User", "required": True},
        {"key": "password", "label": "Pass", "type": "password", "secret": True, "required": True},
    ],
    "connector_fields": [
        {"key": "num_proxies", "label": "Proxies", "type": "number", "default": 1, "min": 1},
        {"key": "locations", "label": "Countries", "type": "country"},
    ],
    "proxy_types": [
        {
            "key": "res",
            "label": "Residential",
            "mode": "session",
            "host": "gw.acme.test",
            "port": 9000,
            "username": "{credential.username}-{session_id}",
            "password": "{credential.password}",
        }
    ],
}


def make_project(project_id: str = "project-1", **overrides: Any) -> Project:
    fields: dict[str, Any] = {
        "id": project_id,
        "name": project_id,
        "username": f"user-{project_id}",
        "password": "pw",
        "created_at": CREATED,
        "updated_at": CREATED,
    }
    return Project(**{**fields, **overrides})


def make_credential(
    credential_id: str = "credential-1",
    *,
    type_id: str = CredentialType.STATIC_PROXY_PROVIDER.value,
    project_id: str = "project-1",
    config: dict[str, Any] | None = None,
) -> Credential:
    return Credential(
        id=credential_id, name=credential_id, type=type_id, project_id=project_id, config=config or {},
        created_at=CREATED, updated_at=CREATED,
    )


def make_connector(connector_id: str, credential: Credential, **overrides: Any) -> Connector:
    fields: dict[str, Any] = {
        "id": connector_id,
        "name": connector_id,
        "credential_id": credential.id,
        "credential_type": credential.type,
        "project_id": credential.project_id,
        "created_at": CREATED,
        "updated_at": CREATED,
    }
    return Connector(**{**fields, **overrides})


def make_proxy(proxy_id: str, connector: Connector, **overrides: Any) -> Proxy:
    fields: dict[str, Any] = {
        "id": proxy_id,
        "host": f"{proxy_id}.example.test",
        "port": 8080,
        "connector_id": connector.id,
        "created_at": CREATED,
        "updated_at": CREATED,
    }
    return Proxy(**{**fields, **overrides})


def acme_record(*, enabled: bool = True) -> ProviderRecord:
    return ProviderRecord(id="acme", name="Acme", spec=ACME, enabled=enabled)


def acme_api_record() -> ProviderRecord:
    """Acme with its type picked on the credential, and an account API token only its credential check reads."""
    spec = {
        **ACME,
        "id": "acme-api",
        "name": "Acme API",
        "credential_fields": [
            {"key": "proxy_type", "label": "Type", "type": "select", "options": [{"value": "res", "label": "Res"}]},
            *ACME["credential_fields"],
            {"key": "api_key", "label": "API key", "type": "password", "secret": True},
        ],
        "proxy_type_field": "credential.proxy_type",
        "validation": {
            "call": {"url": "https://api.acme.test/me", "headers": {"Authorization": "Key {credential.api_key}"}}
        },
    }
    return ProviderRecord(id="acme-api", name="Acme API", spec=spec)


class RecordingProviderStore(ProviderStore):
    """The controller's provider store, recording the descriptor documents each compile applies to its registry."""

    def __init__(self, registry: ProviderRegistry) -> None:
        super().__init__(registry, None)
        self.calls: list[list[dict[str, Any]]] = []

    def apply_specs(self, specs: Sequence[Mapping[str, Any]]) -> bool:
        self.calls.append([dict(spec) for spec in specs])
        return super().apply_specs(specs)


class RecordingGeoSettingsStore(GeoSettingsStore):
    """The controller's geo settings store, recording the settings each compile applies."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, None)
        self.calls: list[tuple[GeoSettings, bool]] = []

    def adopt(self, settings: GeoSettings, *, stored: bool) -> bool:
        self.calls.append((settings, stored))
        return super().adopt(settings, stored=stored)


class Harness:
    """The controller's compiler over the test database, with its registry and recording stores; the api's beside it."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.session_factory = session_factory
        self.registry = build_registry(include_plugins=False)
        self.descriptors = RecordingProviderStore(self.registry)
        self.geo_settings = RecordingGeoSettingsStore(settings)
        self.compiler = FullFeedCompiler(
            session_factory,
            self.registry,
            descriptor_sink=self.descriptors,
            geo_settings_sink=self.geo_settings,
            geo_defaults=GEO_DEFAULTS,
            default_strategy=DEFAULT_STRATEGY,
        )
        # The api's, for its previews: the same registry, and no store.
        self.preview = FeedCompiler(
            session_factory, self.registry, geo_defaults=GEO_DEFAULTS, default_strategy=DEFAULT_STRATEGY
        )

    async def compile(
        self,
        *,
        quarantined: dict[str, datetime] | None = None,
        blocked_until: dict[str, datetime] | None = None,
        changes: str | None = None,
    ) -> CompiledFeed:
        return await self.compiler.compile(
            quarantined=quarantined or {}, blocked_until=blocked_until or {}, changes=changes
        )

    async def save(self, *rows: Any) -> None:
        """Write rows through their repositories in one committed transaction, as a service would."""
        async with self.session_factory() as session:
            for row in rows:
                match row:
                    case Project():
                        await ProjectRepository(session).create(row)
                    case Credential():
                        await CredentialRepository(session).create(row)
                    case Connector():
                        await ConnectorRepository(session).create(row)
                    case Proxy():
                        await ProxyRepository(session).create(row)
                    case ProviderRecord():
                        await ProviderDescriptorRepository(session).create(row)
                    case GeoSettings():
                        await GeoSettingsRepository(session).save(row)
                    case WireGuardServerSettings():
                        await WireGuardSettingsRepository(session).save(row)
                    case WireGuardPeer():
                        await WireGuardPeerRepository(session).create(row)
                    case OpenVpnServerSettings():
                        await OpenVpnSettingsRepository(session).save(row)
                    case OpenVpnPeer():
                        await OpenVpnPeerRepository(session).create(row)
                    case _:
                        raise TypeError(f"No repository for {type(row).__name__}")
            await session.commit()

    async def save_descriptor(self, record: ProviderRecord) -> None:
        async with self.session_factory() as session:
            await ProviderDescriptorRepository(session).update(record)
            await session.commit()

    async def save_database(self, record: GeoDatabaseRecord, blob: bytes | None) -> None:
        async with self.session_factory() as session:
            await GeoDatabaseRepository(session).create(record, blob)
            await session.commit()


@pytest.fixture
def harness(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,  # empties the tables after the test
    test_settings: Settings,
) -> Harness:
    return Harness(db_session_factory, test_settings)


@pytest.fixture
def log() -> Iterator[MagicMock]:
    with patch("api.feed.compiler.logger") as logger:
        yield logger


def connectors_of(sections: dict[str, Section]) -> dict[str, ConnectorSection]:
    """The connector sections of a feed, by connector id."""
    return {section.connector.id: section for section in sections.values() if isinstance(section, ConnectorSection)}


def global_of(sections: dict[str, Section]) -> GlobalSection:
    section = sections[GLOBAL_SECTION]
    assert isinstance(section, GlobalSection)
    return section


def warned(log: MagicMock) -> list[tuple[str, dict[str, Any]]]:
    return [(call.args[0], call.kwargs) for call in log.warning.call_args_list]


async def static_pool(harness: Harness, *proxies: str, **connector_fields: Any) -> Connector:
    """A project with one static connector holding these proxies."""
    credential = make_credential()
    connector = make_connector("connector-1", credential, **connector_fields)
    await harness.save(
        make_project(), credential, connector, *(make_proxy(proxy_id, connector) for proxy_id in proxies)
    )
    return connector


class TestSnapshot:
    async def test_the_change_position_given_is_carried_through(self, harness: Harness) -> None:
        """The controller's position, taken before the compile: what the head and the System page report."""
        await static_pool(harness, "proxy-1")
        assert (await harness.compile(changes="1760000000000-3")).changes == "1760000000000-3"
        assert (await harness.compile()).changes is None

    async def test_a_write_committed_during_a_compile_waits_for_the_next(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every table is read from the snapshot the first read took, so no section runs ahead of another."""
        connector = await static_pool(harness, "proxy-1")
        read_proxies = ProxyRepository.get_all

        async def read_after_a_commit(repository: ProxyRepository) -> list[Proxy]:
            # Another transaction commits a proxy after the compile's first read.
            await harness.save(make_proxy("proxy-late", connector))
            return await read_proxies(repository)

        monkeypatch.setattr(ProxyRepository, "get_all", read_after_a_commit)
        compiled = await harness.compile()
        monkeypatch.undo()

        assert [proxy.id for proxy in compiled.connectors["connector-1"].proxies] == ["proxy-1"]
        later = await harness.compile()
        assert [proxy.id for proxy in later.connectors["connector-1"].proxies] == ["proxy-1", "proxy-late"]

    async def test_the_snapshot_read_leaves_its_pooled_connection_writable(
        self, test_settings: Settings, db_session: AsyncSession
    ) -> None:
        """The controller's writers share the compile's pool: read-only REPEATABLE READ must end with the compile."""
        engine = create_async_engine(test_settings.database_url, pool_size=1, max_overflow=0)
        try:
            harness = Harness(async_sessionmaker(engine, expire_on_commit=False), test_settings)
            await harness.compile()

            # The pool's only connection, back from the compile.
            await harness.save(make_project())
            async with harness.session_factory() as session:
                isolation = (await session.execute(text("SHOW transaction_isolation"))).scalar_one()
                read_only = (await session.execute(text("SHOW transaction_read_only"))).scalar_one()
        finally:
            await engine.dispose()

        assert (isolation, read_only) == ("read committed", "off")


class TestOrder:
    async def test_lists_are_in_id_order_and_proxies_in_creation_order(self, harness: Harness) -> None:
        later = datetime(2026, 10, 2, 9, 30)
        credential = make_credential()
        connector_b = make_connector("connector-b", credential)
        connector_a = make_connector("connector-a", credential)
        await harness.save(
            make_project("project-b"),
            make_project("project-1"),
            credential,
            connector_b,
            connector_a,
            make_proxy("proxy-b", connector_b),
            make_proxy("proxy-a", connector_b, created_at=later),
            make_proxy("proxy-c", connector_b),
            ProviderRecord(id="zeta", name="Zeta", spec={**ACME, "id": "zeta", "name": "Zeta"}),
            acme_record(),
        )

        compiled = await harness.compile()

        global_section = global_of(compiled.sections)
        assert [project.id for project in global_section.projects] == ["project-1", "project-b"]
        assert [spec["id"] for spec in global_section.descriptors] == ["acme", "zeta"]
        assert list(compiled.connectors) == ["connector-a", "connector-b"]
        # Ties on the creation time fall back to the id.
        assert [proxy.id for proxy in compiled.connectors["connector-b"].proxies] == ["proxy-b", "proxy-c", "proxy-a"]

    async def test_a_provider_store_finds_the_compiled_descriptors_unchanged(self, harness: Harness) -> None:
        """Role all applies the compile's list and the refresher's to one store: in two orders, both would parse."""
        zeta = ProviderRecord(id="zeta", name="Zeta", spec={**ACME, "id": "zeta", "name": "Zeta"}, created_at=CREATED)
        # Created before acme: the creation order is not the id order.
        await harness.save(zeta, acme_record())
        compiled = await harness.compile()
        store = ProviderStore(build_registry(include_plugins=False), harness.session_factory)
        assert store.apply_specs(global_of(compiled.sections).descriptors) is True

        with patch("api.providers.store.descriptor_from_dict", side_effect=descriptor_from_dict) as parse:
            assert await store.sync_all() is False
        assert not parse.called

    async def test_unchanged_definitions_compile_to_the_same_json(self, harness: Harness) -> None:
        await static_pool(harness, "proxy-1", "proxy-2")
        quarantined = {"proxy-2": datetime(2026, 10, 17, 12, 30)}

        first = await harness.compile(quarantined=quarantined)
        second = await harness.compile(quarantined=quarantined)

        assert {name: section.model_dump_json() for name, section in first.sections.items()} == {
            name: section.model_dump_json() for name, section in second.sections.items()
        }


class TestProxies:
    async def test_credentials_are_resolved_for_stored_rows(self, harness: Harness) -> None:
        credential = make_credential(type_id="acme", config={"username": "acme-user", "password": "s3cret"})
        connector = make_connector("connector-1", credential, config={"num_proxies": 1})
        # Templates leave secret fields as placeholders in stored rows.
        stored = make_proxy("proxy-1", connector, username="acme-user-abc", password="{password}")
        await harness.save(make_project(), acme_record(), credential, connector, stored)

        compiled = await harness.compile()

        proxy = compiled.connectors["connector-1"].proxies[0]
        assert (proxy.username, proxy.password) == ("acme-user-abc", "s3cret")

    async def test_status_comes_from_the_column(self, harness: Harness) -> None:
        credential = make_credential()
        connector = make_connector("connector-1", credential)
        statuses = [ProxyStatus.HEALTHY, ProxyStatus.DRAINING, ProxyStatus.UNKNOWN]
        await harness.save(
            make_project(),
            credential,
            connector,
            *(make_proxy(f"proxy-{n}", connector, status=status) for n, status in enumerate(statuses)),
        )

        compiled = await harness.compile()

        assert [proxy.status for proxy in compiled.connectors["connector-1"].proxies] == statuses

    async def test_display_only_metadata_is_left_out(self, harness: Harness) -> None:
        metadata = {
            "country": "DE",
            "location_candidates": [{"source": "database", "origin": "db-1", "country": "DE"}],
            "location": {"country": "DE", "city": "Berlin"},
            "location_checked_at": "2026-10-17T12:00:00",
        }
        credential = make_credential()
        connector = make_connector("connector-1", credential)
        await harness.save(make_project(), credential, connector, make_proxy("proxy-1", connector, metadata=metadata))

        compiled = await harness.compile()

        proxy = compiled.connectors["connector-1"].proxies[0]
        assert {"location_candidates"} == DISPLAY_ONLY_METADATA
        # What routing reads stays, in the stored order.
        assert list(proxy.metadata) == ["country", "location", "location_checked_at"]
        async with harness.session_factory() as session:
            stored = await ProxyRepository(session).get_by_id("proxy-1")
        assert stored is not None and "location_candidates" in stored.metadata


class TestConnectors:
    async def test_persisted_countries_and_domain_filters_reach_the_sections(self, harness: Harness) -> None:
        """The compile half of the monolith's persisted country routing test; routing over it is the router's."""
        credential = make_credential()
        us = make_connector(
            "connector-us",
            credential,
            config={"countries": ["US"]},
            routing_config={"domain_whitelist": ["allowed.com"]},
        )
        anywhere = make_connector("connector-any", credential)
        await harness.save(
            make_project(),
            credential,
            us,
            anywhere,
            make_proxy("proxy-us", us, status=ProxyStatus.HEALTHY),
            make_proxy("proxy-any", anywhere, status=ProxyStatus.HEALTHY),
        )

        compiled = await harness.compile()

        assert compiled.connectors["connector-us"].declared_countries == ["US"]
        assert compiled.connectors["connector-us"].connector.routing_config == {"domain_whitelist": ["allowed.com"]}
        assert compiled.connectors["connector-any"].declared_countries == []
        assert [proxy.status for proxy in compiled.connectors["connector-us"].proxies] == [ProxyStatus.HEALTHY]

    async def test_provider_errors_are_left_out_of_the_connector(self, harness: Harness) -> None:
        await static_pool(
            harness, last_error="Vendor said no", last_error_at=datetime(2026, 10, 17, 11, 0), consecutive_errors=3
        )

        compiled = await harness.compile()

        connector = compiled.connectors["connector-1"].connector
        assert (connector.last_error, connector.last_error_at, connector.consecutive_errors) == (None, None, 0)
        assert connector.name == "connector-1"

    async def test_only_descriptor_providers_ship_their_credential(self, harness: Harness) -> None:
        static = make_credential("credential-static")
        aws = make_credential("credential-aws", type_id=CredentialType.AWS.value, config={"auth_mode": "host_role"})
        acme = make_credential("credential-acme", type_id="acme", config={"username": "u", "password": "s3cret"})
        await harness.save(
            make_project(),
            acme_record(),
            static,
            aws,
            acme,
            make_connector("connector-static", static),
            make_connector("connector-aws", aws, config={"region": "us-east-1"}),
            make_connector("connector-acme", acme, config={"num_proxies": 1}),
        )

        compiled = await harness.compile()

        assert compiled.connectors["connector-static"].credential is None
        assert compiled.connectors["connector-aws"].credential is None
        assert compiled.connectors["connector-acme"].credential == acme

    async def test_a_credential_travels_without_the_secrets_no_template_reads(self, harness: Harness) -> None:
        """A vendor account's API token serves validation and syncs, which run where Postgres is, never on a gateway."""
        config = {"proxy_type": "res", "username": "u", "password": "s3cret", "api_key": "account-token"}
        credential = make_credential("credential-acme", type_id="acme-api", config=config)
        connector = make_connector("connector-acme", credential, config={"num_proxies": 1, "session_mode": "dynamic"})
        gateway = make_proxy(
            "proxy-gateway", connector, password="{password}", status=ProxyStatus.HEALTHY,
            metadata={"dynamic_sessions": "true"},
        )
        await harness.save(make_project(), acme_api_record(), credential, connector, gateway)

        compiled = await harness.compile()

        shipped = compiled.connectors["connector-acme"].credential
        assert shipped is not None and shipped.config == {"proxy_type": "res", "username": "u", "password": "s3cret"}
        # What travels is what routing reads: a request rendered on a gateway gets the password filled in.
        router = Router(RoutingSnapshot.from_sections(compiled.sections, registry=harness.registry))
        selected = await router.select_proxy_for_project("project-1", "order-1")
        assert selected is not None and selected.username is not None and selected.username.startswith("u-")
        assert selected.password == "s3cret"

    async def test_declared_countries_are_read_through_the_provider(self, harness: Harness) -> None:
        static = make_credential("credential-static")
        aws = make_credential("credential-aws", type_id=CredentialType.AWS.value)
        acme = make_credential("credential-acme", type_id="acme", config={"username": "u", "password": "p"})
        on_acme = make_connector("connector-acme", acme, config={"num_proxies": 1, "locations": ["de", "fr"]})
        await harness.save(
            make_project(),
            acme_record(),
            static,
            aws,
            acme,
            make_connector("connector-static", static, config={"countries": ["gb"]}),
            make_connector("connector-aws", aws, config={"region": "us-east-1", "countries": ["us"]}),
            on_acme,
        )

        compiled = await harness.compile()

        # The descriptor's own country field, which the connector model does not know.
        assert on_acme.countries == []
        assert compiled.connectors["connector-acme"].declared_countries == ["DE", "FR"]
        assert compiled.connectors["connector-static"].declared_countries == ["GB"]
        assert compiled.connectors["connector-aws"].declared_countries == ["US"]

    async def test_disabled_and_pending_deletion_connectors_are_compiled(self, harness: Harness) -> None:
        credential = make_credential()
        disabled = make_connector("connector-disabled", credential, enabled=False)
        leaving = make_connector("connector-leaving", credential, enabled=False, pending_deletion=True)
        await harness.save(make_project(), credential, disabled, leaving, make_proxy("proxy-1", leaving))

        compiled = await harness.compile()

        assert compiled.connectors["connector-disabled"].connector.enabled is False
        assert compiled.connectors["connector-leaving"].connector.pending_deletion is True
        assert [proxy.id for proxy in compiled.connectors["connector-leaving"].proxies] == ["proxy-1"]

    async def test_a_connector_whose_provider_is_gone_is_left_out_and_logged_once(
        self, harness: Harness, log: MagicMock
    ) -> None:
        acme = make_credential(type_id="acme", config={"username": "u", "password": "p"})
        connector = make_connector("connector-1", acme, config={"num_proxies": 1})
        await harness.save(make_project(), acme_record(), acme, connector, make_proxy("proxy-1", connector))
        assert "connector-1" in (await harness.compile()).connectors

        await harness.save_descriptor(acme_record(enabled=False))
        gone = await harness.compile()
        await harness.compile()

        assert "connector-1" not in gone.connectors
        assert connector_section("connector-1") not in gone.sections
        assert warned(log) == [
            (
                "Connector left out of the feed: its provider is gone",
                {
                    "connector_id": "connector-1",
                    "connector": "connector-1",
                    "project_id": "project-1",
                    "provider": "acme",
                },
            )
        ]
        await harness.save_descriptor(acme_record())
        assert "connector-1" in (await harness.compile()).connectors

    async def test_a_provider_that_cannot_be_built_serves_its_rows_as_stored(
        self, harness: Harness, log: MagicMock
    ) -> None:
        credential = make_credential(
            type_id="oxylabs", config={"proxy_type": "nonsense", "username": "u", "password": "s3cret"}
        )
        connector = make_connector("connector-1", credential, config={"country_code": ["US"]})
        await harness.save(
            make_project(), credential, connector, make_proxy("proxy-1", connector, password="{password}")
        )

        compiled = await harness.compile()
        await harness.compile()

        section = compiled.connectors["connector-1"]
        assert section.credential is None
        assert section.declared_countries == ["US"]
        assert section.proxies[0].password == "s3cret"
        assert [event for event, _ in warned(log)] == [
            "Connector provider cannot be built: its proxies are served as stored"
        ]


    async def test_any_error_building_a_provider_is_a_workaround(self, harness: Harness, log: MagicMock) -> None:
        """A plugin's provider class failing its own way serves the rows as stored too, and is logged with its error."""
        fail_to_build(harness.registry, "oxylabs", RuntimeError("the vendor SDK is broken"))
        config = {"proxy_type": "residential", "username": "u", "password": "p"}
        credential = make_credential(type_id="oxylabs", config=config)
        connector = make_connector("connector-1", credential, config={"country_code": ["US"]})
        await harness.save(
            make_project(), credential, connector, make_proxy("proxy-1", connector, password="{password}")
        )

        section = (await harness.compile()).connectors["connector-1"]

        assert (section.credential, section.declared_countries, section.proxies[0].password) == (None, ["US"], "p")
        assert warned(log) == [
            (
                "Connector provider cannot be built: its proxies are served as stored",
                {
                    "connector_id": "connector-1",
                    "connector": "connector-1",
                    "provider": "oxylabs",
                    "error": "the vendor SDK is broken",
                },
            )
        ]

    async def test_a_provider_error_without_text_is_logged_by_its_type(self, harness: Harness, log: MagicMock) -> None:
        """A plugin's bare NotImplementedError has no text: the workaround's line names its type."""
        fail_to_build(harness.registry, "oxylabs", NotImplementedError())
        config = {"proxy_type": "residential", "username": "u", "password": "p"}
        credential = make_credential(type_id="oxylabs", config=config)
        await harness.save(make_project(), credential, make_connector("connector-1", credential))

        await harness.compile()

        assert [fields["error"] for _, fields in warned(log)] == ["NotImplementedError"]


class TestProjects:
    async def test_projects_carry_no_description(self, harness: Harness) -> None:
        await harness.save(make_project(description="The scrapers' pool"))

        compiled = await harness.compile()

        assert global_of(compiled.sections).projects[0].description == ""

    async def test_an_unknown_strategy_is_served_as_the_installs_default_and_logged_once(
        self, harness: Harness, log: MagicMock
    ) -> None:
        await harness.save(
            make_project("project-1", routing_strategy="fastest"),
            make_project("project-2", routing_strategy="least_used"),
        )

        compiled = await harness.compile()
        await harness.compile()

        assert [project.routing_strategy for project in global_of(compiled.sections).projects] == [
            DEFAULT_STRATEGY,
            "least_used",
        ]
        assert warned(log) == [
            (
                "Unknown routing strategy served as the default",
                {"project_id": "project-1", "project": "project-1", "strategy": "fastest", "served_as": "health_based"},
            )
        ]
        # The api's previews serve it alike.
        assert [project.routing_strategy for project in global_of(await project_feed(harness, "project-1")).projects] == [
            DEFAULT_STRATEGY
        ]

    async def test_a_strategy_stored_in_another_case_is_served_by_the_name_routers_know(
        self, harness: Harness, log: MagicMock
    ) -> None:
        await harness.save(make_project("project-1", routing_strategy="Sticky"))

        compiled = await harness.compile()

        assert [project.routing_strategy for project in global_of(compiled.sections).projects] == ["sticky"]
        assert warned(log) == []


class TestGlobal:
    async def test_the_sinks_get_the_transactions_rows_before_sections_are_built(self, harness: Harness) -> None:
        stored = GeoSettings(echo_url="https://echo.stored.test/ip", preflight_max_attempts=5)
        acme = make_credential(type_id="acme", config={"username": "u", "password": "p"})
        await harness.save(
            make_project(),
            acme_record(),
            ProviderRecord(id="zeta", name="Zeta", spec={**ACME, "id": "zeta", "name": "Zeta"}, enabled=False),
            stored,
            acme,
            make_connector("connector-1", acme, config={"num_proxies": 1}),
        )
        assert harness.registry.get("acme") is None

        compiled = await harness.compile()

        global_section = global_of(compiled.sections)
        assert harness.descriptors.calls == [[ACME]] and global_section.descriptors == [ACME]
        assert harness.geo_settings.calls == [(stored, True)]
        assert global_section.geo_settings == stored and global_section.geo_settings_stored
        # Applied before the connectors were compiled, or this one would have been left out.
        assert "connector-1" in compiled.connectors

    async def test_the_geo_defaults_stand_in_for_a_missing_settings_row(self, harness: Harness) -> None:
        compiled = await harness.compile()

        global_section = global_of(compiled.sections)
        assert global_section.geo_settings == GEO_DEFAULTS and not global_section.geo_settings_stored
        assert harness.geo_settings.calls == [(GEO_DEFAULTS, False)]

    async def test_only_stored_databases_with_a_file_travel_without_their_credentials(self, harness: Harness) -> None:
        uploaded = GeoDatabaseRecord(id="db-b", name="City", sha256="b" * 64, created_at=CREATED)
        downloaded = GeoDatabaseRecord(
            id="db-a", name="Country", source=GeoDatabaseSource.URL, sha256="a" * 64, enabled=False,
            update_url="https://vendor.test/country.mmdb", update_auth={"token": "vendor-secret"}, created_at=CREATED,
        )
        # Registered, but its first download failed: no file to fetch.
        pending = GeoDatabaseRecord(
            id="db-c", name="Pending", source=GeoDatabaseSource.URL, update_url="https://vendor.test/asn.mmdb",
            created_at=CREATED,
        )
        await harness.save_database(uploaded, b"city")
        await harness.save_database(downloaded, b"country")
        await harness.save_database(pending, None)

        compiled = await harness.compile()

        databases = global_of(compiled.sections).geo_databases
        assert [(database.id, database.sha256, database.enabled) for database in databases] == [
            ("db-a", "a" * 64, False),
            ("db-b", "b" * 64, True),
        ]
        assert "vendor-secret" not in global_of(compiled.sections).model_dump_json()

    async def test_tunnels_without_settings_are_absent(self, harness: Harness) -> None:
        compiled = await harness.compile()

        assert (global_of(compiled.sections).wireguard, global_of(compiled.sections).openvpn) == (None, None)

    async def test_tunnel_feeds_carry_no_device_keys_and_no_ca_key(self, harness: Harness) -> None:
        await harness.save(
            make_project(),
            WireGuardServerSettings(private_key="wg-server-private", public_key="wg-server-public"),
            WireGuardPeer(
                id="wg-b", project_id="project-1", name="laptop", public_key="wg-peer-public-b",
                private_key="wg-device-private", preshared_key="wg-psk", address="10.13.13.3",
            ),
            WireGuardPeer(
                id="wg-a", project_id="project-1", name="phone", public_key="wg-peer-public-a", address="10.13.13.2"
            ),
            OpenVpnServerSettings(
                ca_cert="ovpn-ca-cert", ca_key="ovpn-ca-private", server_cert="ovpn-server-cert",
                server_key="ovpn-server-key", tls_crypt_key="ovpn-tls-crypt",
            ),
            OpenVpnPeer(
                id="ovpn-a", project_id="project-1", name="router", certificate="ovpn-device-cert",
                private_key="ovpn-device-private", serial="1001", certificate_expires_at=CREATED, address="10.14.14.2",
            ),
        )

        compiled = await harness.compile()

        global_section = global_of(compiled.sections)
        assert global_section.wireguard is not None and global_section.openvpn is not None
        assert global_section.wireguard.server.private_key == "wg-server-private"
        assert [(peer.id, peer.public_key, peer.preshared_key) for peer in global_section.wireguard.peers] == [
            ("wg-a", "wg-peer-public-a", None),
            ("wg-b", "wg-peer-public-b", "wg-psk"),
        ]
        assert [(peer.id, peer.serial) for peer in global_section.openvpn.peers] == [("ovpn-a", "1001")]
        dumped = global_section.model_dump_json()
        for secret in ("wg-device-private", "ovpn-ca-private", "ovpn-device-private", "ovpn-device-cert"):
            assert secret not in dumped


class TestRuntime:
    async def test_quarantines_and_traffic_blocks_travel_in_the_runtime_section(self, harness: Harness) -> None:
        """Not in the connectors' sections: a trip would send every gateway the whole pool again."""
        credential = make_credential()
        first = make_connector("connector-1", credential)
        second = make_connector("connector-2", credential)
        await harness.save(
            make_project(), credential, first, second, make_proxy("proxy-1", first), make_proxy("proxy-2", second)
        )
        until = datetime(2026, 10, 17, 12, 30)
        blocked = datetime(2026, 11, 1, 0, 0)

        compiled = await harness.compile(
            quarantined={"proxy-gone": until, "proxy-1": until}, blocked_until={"gone": blocked, "connector-1": blocked}
        )

        # Only what the compile holds, by id.
        assert compiled.sections[RUNTIME_SECTION] == RuntimeSection(
            quarantined={"proxy-1": until}, traffic_blocked_until={"connector-1": blocked}
        )
        # The connectors' sections are the definitions alone, the very objects the job keys are made from.
        assert compiled.sections[connector_section("connector-1")] is compiled.connectors["connector-1"]

    async def test_the_runtime_section_is_made_from_the_compile_without_reading_postgres(
        self, harness: Harness
    ) -> None:
        credential = make_credential()
        first = make_connector("connector-1", credential)
        second = make_connector("connector-2", credential)
        await harness.save(
            make_project(), credential, first, second, make_proxy("proxy-1", first), make_proxy("proxy-2", second)
        )
        await harness.compile()
        # Gone from Postgres, still in the compile the section is made from.
        async with harness.session_factory() as session:
            await ProxyRepository(session).delete("proxy-1")
            await session.commit()
        until = datetime(2026, 10, 17, 12, 30)
        blocked = datetime(2026, 11, 1, 0, 0)

        section = harness.compiler.runtime_section(
            {"proxy-unknown": until, "proxy-2": until, "proxy-1": until},
            {"connector-unknown": blocked, "connector-2": blocked},
        )

        assert section == RuntimeSection(
            quarantined={"proxy-1": until, "proxy-2": until}, traffic_blocked_until={"connector-2": blocked}
        )
        # In ID order whatever order they came in, so the same state gives the same digest.
        assert list(section.quarantined) == ["proxy-1", "proxy-2"]

    def test_the_runtime_section_is_made_from_a_compile(self, harness: Harness) -> None:
        with pytest.raises(RuntimeError):
            harness.compiler.runtime_section({}, {})


async def project_feed(
    harness: Harness,
    project_id: str,
    quarantined: dict[str, datetime] | None = None,
    blocked_until: dict[str, datetime] | None = None,
) -> dict[str, Section]:
    """One project's feed as the api compiles it for a preview, through the harness's api compiler."""
    return await harness.preview.compile_project(
        project_id, quarantined=quarantined or {}, blocked_until=blocked_until or {}
    )


class TestProjectCompile:
    @pytest.fixture
    async def two_projects(self, harness: Harness) -> None:
        for project_id in ("project-1", "project-2"):
            credential = make_credential(f"credential-{project_id}", project_id=project_id)
            connector = make_connector(f"connector-{project_id}", credential)
            await harness.save(
                make_project(project_id), credential, connector, make_proxy(f"proxy-{project_id}", connector)
            )

    async def test_a_project_compile_holds_only_that_projects_rows(self, harness: Harness, two_projects: None) -> None:
        await harness.save(acme_record(), GeoSettings(echo_url="https://echo.stored.test/ip"))
        sections = await project_feed(harness, "project-1")

        global_section = global_of(sections)
        assert [project.id for project in global_section.projects] == ["project-1"]
        assert global_section.descriptors == [ACME]
        assert global_section.geo_settings.echo_url == "https://echo.stored.test/ip"
        assert (global_section.geo_databases, global_section.wireguard, global_section.openvpn) == ([], None, None)
        assert list(connectors_of(sections)) == ["connector-project-1"]

    async def test_the_quarantines_and_traffic_blocks_given_travel_with_the_projects_rows(
        self, harness: Harness, two_projects: None, redis_client: RedisClient
    ) -> None:
        """The api reads both (none while Redis is away) and the compile reads no Redis itself."""
        until = datetime(2026, 10, 17, 12, 30)
        # Ended already: the preview's router judges a block as gateways do, so the compile passes it on.
        ended = datetime(2026, 10, 1)
        await redis_client.client.zadd(QUARANTINE_KEY, {"proxy-project-1": time.time() + 600})
        await redis_client.client.hset(TRAFFIC_BLOCKED_KEY, "connector-project-1", "2099-01-01T00:00:00")
        # The other project's quarantine and block are left out with its rows.
        sections = await project_feed(
            harness,
            "project-1",
            {"proxy-project-2": until, "proxy-project-1": until},
            {"connector-project-2": until, "connector-project-1": ended},
        )

        runtime = sections[RUNTIME_SECTION]
        assert isinstance(runtime, RuntimeSection)
        assert runtime.quarantined == {"proxy-project-1": until}
        assert runtime.traffic_blocked_until == {"connector-project-1": ended}
        # Nothing given: nothing in quarantine and nothing blocked, whatever Redis holds.
        runtime = (await project_feed(harness, "project-1"))[RUNTIME_SECTION]
        assert isinstance(runtime, RuntimeSection)
        assert (runtime.quarantined, runtime.traffic_blocked_until) == ({}, {})

    async def test_a_project_compile_applies_nothing(self, harness: Harness) -> None:
        """The api's registry and stores follow Postgres on their own; a preview never moves them."""
        acme = make_credential(type_id="acme", config={"username": "u", "password": "p"})
        await harness.save(make_project(), acme_record(), acme, make_connector("connector-1", acme))

        sections = await project_feed(harness, "project-1")

        # This registry has not loaded the descriptor, so the preview leaves its connector out.
        assert harness.registry.get("acme") is None
        assert connectors_of(sections) == {}

    async def test_a_project_compile_applies_nothing_to_the_sinks_and_logs_nothing(
        self, harness: Harness, log: MagicMock
    ) -> None:
        """The stores follow Postgres on their own, and the workarounds are the full compile's to log, once each."""
        await harness.save(
            make_project("project-1", routing_strategy="fastest"),
            make_project("project-2", routing_strategy="fastest"),
            acme_record(),
            GeoSettings(echo_url="https://echo.stored.test/ip"),
        )
        await harness.compile()
        logged = warned(log)
        harness.descriptors.calls.clear()
        harness.geo_settings.calls.clear()

        sections = await project_feed(harness, "project-1")

        assert global_of(sections).descriptors == [ACME] and global_of(sections).geo_settings_stored
        assert (harness.descriptors.calls, harness.geo_settings.calls) == ([], [])
        assert warned(log) == logged
        # What the full compile logged stays its record: the next logs neither project's strategy again.
        await harness.compile()
        assert [event for event, _ in logged] == ["Unknown routing strategy served as the default"] * 2
        assert warned(log) == logged

    def test_the_api_compiler_holds_no_store_and_compiles_no_whole_feed(self, harness: Harness) -> None:
        """Only the controller's compiler applies what it reads to stores: nothing on the api's can write them."""
        assert not hasattr(harness.preview, "compile")
        assert not hasattr(harness.preview, "runtime_section")

    async def test_a_project_compile_keeps_the_full_compiles_ids_for_the_runtime_section(
        self, harness: Harness
    ) -> None:
        credential = make_credential()
        connector = make_connector("connector-1", credential)
        await harness.save(make_project(), credential, connector, make_proxy("proxy-1", connector))
        await harness.compile()
        later = make_connector("connector-2", credential)
        await harness.save(later, make_proxy("proxy-2", later))
        until = datetime(2026, 10, 17, 12, 30)

        preview = (await project_feed(harness, "project-1", {"proxy-2": until}, {"connector-2": until}))[
            RUNTIME_SECTION
        ]
        section = harness.compiler.runtime_section(
            {"proxy-1": until, "proxy-2": until}, {"connector-1": until, "connector-2": until}
        )

        assert preview == RuntimeSection(quarantined={"proxy-2": until}, traffic_blocked_until={"connector-2": until})
        assert section == RuntimeSection(quarantined={"proxy-1": until}, traffic_blocked_until={"connector-1": until})

    async def test_the_block_of_a_connector_the_compile_left_out_travels_too(self, harness: Harness) -> None:
        """The traffic split asks the preview about every connector of the project, those left out of the feed included."""
        acme = make_credential(type_id="acme", config={"username": "u", "password": "p"})
        await harness.save(make_project(), acme_record(), acme, make_connector("connector-1", acme))
        until = datetime(2026, 11, 1)

        sections = await project_feed(harness, "project-1", blocked_until={"connector-1": until})

        assert connectors_of(sections) == {}
        runtime = sections[RUNTIME_SECTION]
        assert isinstance(runtime, RuntimeSection) and runtime.traffic_blocked_until == {"connector-1": until}

    async def test_a_project_compile_builds_what_the_full_compile_builds(
        self, harness: Harness, two_projects: None
    ) -> None:
        compiled = await harness.compile()
        sections = await project_feed(harness, "project-1")

        name = connector_section("connector-project-1")
        assert sections[name].model_dump_json() == compiled.sections[name].model_dump_json()
        assert global_of(sections).projects == global_of(compiled.sections).projects[:1]

    async def test_a_missing_project_compiles_to_a_feed_without_it(self, harness: Harness, two_projects: None) -> None:
        sections = await project_feed(harness, "project-missing")

        assert list(sections) == [GLOBAL_SECTION, RUNTIME_SECTION]
        assert global_of(sections).projects == []
