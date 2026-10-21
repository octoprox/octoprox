# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The feed compiler: the definitions in Postgres, cut into the sections gateways route on.

The controller compiles the whole feed, the api one project for its previews; the caller passes the runtime state in.
"""

from __future__ import annotations

import time
from collections.abc import Container, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel

from api.core import utc_now
from api.core.logging import error_text
from api.db.geo_repository import GeoDatabaseRepository, GeoSettingsRepository
from api.db.openvpn_repository import OpenVpnPeerRepository, OpenVpnSettingsRepository
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    ProjectRepository,
    ProviderDescriptorRepository,
    ProxyRepository,
)
from api.db.wireguard_repository import WireGuardPeerRepository, WireGuardSettingsRepository
from api.feed.models import (
    GLOBAL_SECTION,
    RUNTIME_SECTION,
    ConnectorSection,
    GeoDatabaseFeed,
    GlobalSection,
    OpenVpnFeed,
    OpenVpnPeerFeed,
    OpenVpnServerFeed,
    RuntimeSection,
    Section,
    WireGuardFeed,
    WireGuardPeerFeed,
    WireGuardServerFeed,
    connector_section,
)
from api.feed.sections import resolve_credentials, routing_credential
from api.geo.models import (
    META_LOCATION_CANDIDATES,
    GeoDatabaseRecord,
    GeoDatabaseSource,
    GeoSettings,
)
from api.models.connector import Connector
from api.models.credential import Credential
from api.models.project import Project
from api.models.proxy import Proxy
from api.strategies import strategy_name

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.session import SessionFactory
    from api.geo.settings import GeoSettingsStore
    from api.models.provider import ProviderRecord
    from api.providers.registry import ProviderRegistry
    from api.providers.store import ProviderStore

logger = structlog.get_logger()

# Proxy metadata keys a section leaves out: attribution keeps them on the
# stored row, and nothing a gateway runs reads them. The candidate list, one
# entry per database and echo answer, is the largest value on a judged row.
# ``location`` stays: routing falls back on its state and city.
DISPLAY_ONLY_METADATA = frozenset({META_LOCATION_CANDIDATES})

# A connector's provider-error bookkeeping, as every section carries it:
# clear. A failing vendor call rewrites these fields on each attempt and no
# gateway reads them, so carried as stored they would republish the
# connector's section to every gateway on every failed sync.
_NO_PROVIDER_ERRORS: dict[str, Any] = {"last_error": None, "last_error_at": None, "consecutive_errors": 0}

# How the full compile reads: one snapshot for every table, taken by its
# first read, so no section runs ahead of another, in a transaction that
# cannot write.
_SNAPSHOT_READ: dict[str, Any] = {"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}

# What the full compile logs, once, when it starts working around a stored
# definition (see FullFeedCompiler._log_workarounds).
_PROVIDER_GONE = "Connector left out of the feed: its provider is gone"
_PROVIDER_UNBUILDABLE = "Connector provider cannot be built: its proxies are served as stored"
_UNKNOWN_STRATEGY = "Unknown routing strategy served as the default"

# The workarounds one compile made: (event, entity id) -> the fields to log with it.
type _Workarounds = dict[tuple[str, str], dict[str, Any]]


@dataclass(frozen=True)
class CompiledFeed:
    """One full compile: the whole feed from one Postgres snapshot, and each connector's section by ID for the job keys.

    A connector the compile left out is in neither map.
    """

    # How far the change stream had been read when the compile began (an
    # entry ID): every write announced up to it is in the sections. None for
    # a compile made without following the stream.
    changes: str | None
    compiled_at: datetime
    # The whole feed, ready for the publisher: the global section, every
    # connector section, and the runtime section with the quarantines and the
    # traffic blocks the compile was given.
    sections: dict[str, Section]
    # Every compiled connector's section, the very objects ``sections`` holds,
    # by connector ID: what the controller's job keys are made from.
    connectors: dict[str, ConnectorSection]


class FeedCompiler:
    """Compiles one project's routing feed from Postgres for the api's previews; it holds no store and keeps nothing.

    Args:
        geo_defaults: what the install runs with, and the feed carries, while no geo settings row is saved.
        default_strategy: the install's setting, which a project is served with when no router has the strategy it stores.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        provider_registry: ProviderRegistry,
        *,
        geo_defaults: GeoSettings,
        default_strategy: str,
    ) -> None:
        self._session_factory = session_factory
        self._registry = provider_registry
        self._geo_defaults = geo_defaults
        self._default_strategy = default_strategy

    async def compile_project(
        self, project_id: str, *, quarantined: Mapping[str, datetime], blocked_until: Mapping[str, datetime]
    ) -> dict[str, Section]:
        """One project's feed for the api's previews, built as the full compile builds it, in a session of its own.

        It applies nothing, keeps and logs nothing, and carries no database list and no tunnels.

        Args:
            quarantined: proxy ID -> its quarantine's end (naive UTC), as the api read it; other projects' are ignored.
            blocked_until: connector ID -> its block's end (naive UTC), as the api read it; other projects' are ignored.
        """
        async with self._session_factory() as session:
            project = await ProjectRepository(session).get_by_id(project_id)
            credentials = await CredentialRepository(session).get_by_project(project_id)
            connectors = await ConnectorRepository(session).get_by_project(project_id)
            proxies = await ProxyRepository(session).get_by_project(project_id)
            descriptors = await ProviderDescriptorRepository(session).get_all()
            stored_settings = await GeoSettingsRepository(session).get()
        # Not logged: a preview compiles per request, and the controller logs these once.
        workarounds: _Workarounds = {}
        global_section = self._global_section(
            [project] if project is not None else [], _enabled_specs(descriptors), stored_settings, workarounds
        )
        connector_sections = self._connector_sections(connectors, credentials, proxies, workarounds)
        held = {proxy.id for section in connector_sections.values() for proxy in section.proxies}
        # Every connector of the project keeps its block, those left out of the sections too: the traffic split asks.
        owned = {connector.id for connector in connectors}
        runtime = self._runtime_section(quarantined, blocked_until, held, owned)
        return self._sections(global_section, connector_sections, runtime)

    def _global_section(
        self,
        projects: Iterable[Project],
        specs: list[dict[str, Any]],
        stored_settings: GeoSettings | None,
        workarounds: _Workarounds,
        *,
        databases: Iterable[GeoDatabaseRecord] = (),
        wireguard: WireGuardFeed | None = None,
        openvpn: OpenVpnFeed | None = None,
    ) -> GlobalSection:
        """The global section of these rows, with the defaults while no geo settings row is saved.

        A preview passes no databases and no tunnels, so its section carries none.
        """
        return GlobalSection(
            projects=self._projects(projects, workarounds),
            descriptors=specs,
            geo_settings=self._geo_settings(stored_settings),
            geo_settings_stored=stored_settings is not None,
            geo_databases=_database_feeds(databases),
            wireguard=wireguard,
            openvpn=openvpn,
        )

    def _projects(self, projects: Iterable[Project], workarounds: _Workarounds) -> list[Project]:
        """The projects as the global section carries them: by id, no description, the strategy gateways route them with."""
        compiled: list[Project] = []
        for project in sorted(projects, key=lambda p: p.id):
            try:
                strategy = strategy_name(project.routing_strategy)
            except ValueError:
                # Routers have no strategy of the stored name: saved before names were checked, or since removed.
                strategy = self._default_strategy
                workarounds[_UNKNOWN_STRATEGY, project.id] = {
                    "project_id": project.id,
                    "project": project.name,
                    "strategy": project.routing_strategy,
                    "served_as": strategy,
                }
            compiled.append(project.model_copy(update={"description": "", "routing_strategy": strategy}))
        return compiled

    def _geo_settings(self, stored: GeoSettings | None) -> GeoSettings:
        """The geo settings the install runs with: the stored row, else the defaults."""
        return stored if stored is not None else self._geo_defaults

    def _connector_sections(
        self,
        connectors: Iterable[Connector],
        credentials: Iterable[Credential],
        proxies: Iterable[Proxy],
        workarounds: _Workarounds,
    ) -> dict[str, ConnectorSection]:
        """Each connector's section by ID, in ID order, with its proxies in the ``(created_at, id)`` order they come in.

        A connector whose provider type the registry no longer has is left out, noted as a workaround.
        """
        credential_of = {credential.id: credential for credential in credentials}
        rows: dict[str, list[Proxy]] = {}
        for proxy in proxies:
            rows.setdefault(proxy.connector_id, []).append(proxy)
        sections: dict[str, ConnectorSection] = {}
        for connector in sorted(connectors, key=lambda c: c.id):
            if self._registry.get(connector.credential_type) is None:
                workarounds[_PROVIDER_GONE, connector.id] = {
                    "connector_id": connector.id,
                    "connector": connector.name,
                    "project_id": connector.project_id,
                    "provider": connector.credential_type,
                }
                continue
            credential = credential_of.get(connector.credential_id)
            try:
                provider = self._registry.descriptor_provider(connector, credential)
            except Exception as exc:
                # Noted and served as none, so one bad connector never stops the feed.
                workarounds[_PROVIDER_UNBUILDABLE, connector.id] = {
                    "connector_id": connector.id,
                    "connector": connector.name,
                    "provider": connector.credential_type,
                    "error": error_text(exc),
                }
                provider = None
            shipped = (
                routing_credential(credential, provider) if credential is not None and provider is not None else None
            )
            sections[connector.id] = ConnectorSection(
                connector=connector.model_copy(update=_NO_PROVIDER_ERRORS),
                credential=shipped,
                declared_countries=provider.countries if provider is not None else connector.countries,
                proxies=[_section_proxy(proxy, credential, connector) for proxy in rows.get(connector.id, [])],
            )
        return sections

    @staticmethod
    def _runtime_section(
        quarantined: Mapping[str, datetime],
        blocked_until: Mapping[str, datetime],
        proxy_ids: Container[str],
        connector_ids: Container[str],
    ) -> RuntimeSection:
        """The quarantines of ``proxy_ids`` and the traffic blocks of ``connector_ids``, each map in ID order."""
        return RuntimeSection(
            quarantined={proxy_id: quarantined[proxy_id] for proxy_id in sorted(quarantined) if proxy_id in proxy_ids},
            traffic_blocked_until={
                connector_id: blocked_until[connector_id]
                for connector_id in sorted(blocked_until)
                if connector_id in connector_ids
            },
        )

    @staticmethod
    def _sections(
        global_section: GlobalSection,
        connector_sections: Mapping[str, ConnectorSection],
        runtime: RuntimeSection,
    ) -> dict[str, Section]:
        """The feed's sections by name: the global section, each connector's in order, then the runtime section."""
        sections: dict[str, Section] = {GLOBAL_SECTION: global_section}
        for connector_id, section in connector_sections.items():
            sections[connector_section(connector_id)] = section
        sections[RUNTIME_SECTION] = runtime
        return sections


class FullFeedCompiler(FeedCompiler):
    """Compiles the whole routing feed on the controller, applying the rows it reads to the process's stores first.

    It keeps the ids of its last compile for the runtime sections built between compiles, and logs each workaround once.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        provider_registry: ProviderRegistry,
        *,
        descriptor_sink: ProviderStore,
        geo_settings_sink: GeoSettingsStore,
        geo_defaults: GeoSettings,
        default_strategy: str,
    ) -> None:
        super().__init__(
            session_factory, provider_registry, geo_defaults=geo_defaults, default_strategy=default_strategy
        )
        self._descriptor_sink = descriptor_sink
        self._geo_settings_sink = geo_settings_sink
        # What the last full compile holds, for runtime_section: the ids a
        # quarantine or a traffic block may name. None before the first compile.
        self._proxy_ids: frozenset[str] | None = None
        self._connector_ids: frozenset[str] = frozenset()
        # The workarounds the last full compile made, so the next logs only new ones.
        self._logged: set[tuple[str, str]] = set()

    async def compile(
        self,
        *,
        quarantined: Mapping[str, datetime],
        blocked_until: Mapping[str, datetime],
        changes: str | None,
    ) -> CompiledFeed:
        """Every section as Postgres holds it now, read in one read-only snapshot, with these quarantines and traffic blocks.

        The rows read are applied to the sinks before any section is built; raises when Postgres cannot be read.

        Args:
            quarantined: proxy ID -> when its quarantine ends (naive UTC); an ID the feed does not hold is ignored.
            blocked_until: connector ID -> when its traffic block ends (naive UTC); an ID the feed does not hold is ignored.
            changes: the caller's position in the change stream, taken before this call; None for no stream.
        """
        started = time.monotonic()
        async with self._session_factory() as session:
            await session.connection(execution_options=_SNAPSHOT_READ)
            # The first read takes the snapshot every read below shares.
            projects = await ProjectRepository(session).get_all()
            credentials = await CredentialRepository(session).get_all()
            connectors = await ConnectorRepository(session).get_all()
            proxies = await ProxyRepository(session).get_all()
            descriptors = await ProviderDescriptorRepository(session).get_all()
            stored_settings = await GeoSettingsRepository(session).get()
            databases = await GeoDatabaseRepository(session).get_all()
            wireguard = await _wireguard_feed(session)
            openvpn = await _openvpn_feed(session)
        specs = _enabled_specs(descriptors)
        # Before any section: connectors compile against the providers this transaction saw.
        self._descriptor_sink.apply_specs(specs)
        self._geo_settings_sink.adopt(self._geo_settings(stored_settings), stored=stored_settings is not None)
        workarounds: _Workarounds = {}
        global_section = self._global_section(
            projects, specs, stored_settings, workarounds, databases=databases, wireguard=wireguard, openvpn=openvpn
        )
        connector_sections = self._connector_sections(connectors, credentials, proxies, workarounds)
        self._proxy_ids = frozenset(proxy.id for section in connector_sections.values() for proxy in section.proxies)
        self._connector_ids = frozenset(connector_sections)
        self._log_workarounds(workarounds)
        sections = self._sections(global_section, connector_sections, self.runtime_section(quarantined, blocked_until))
        logger.debug(
            "Feed compiled",
            changes=changes,
            connectors=len(connector_sections),
            proxies=sum(len(section.proxies) for section in connector_sections.values()),
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )
        return CompiledFeed(
            changes=changes, compiled_at=utc_now(), sections=sections, connectors=dict(connector_sections)
        )

    def runtime_section(
        self, quarantined: Mapping[str, datetime], blocked_until: Mapping[str, datetime]
    ) -> RuntimeSection:
        """The runtime section for these quarantines and traffic blocks, holding only what the last compile holds, by ID.

        Postgres is not read; RuntimeError before the first compile.
        """
        if self._proxy_ids is None:
            raise RuntimeError("The runtime section is built from a full compile, and none has run")
        return self._runtime_section(quarantined, blocked_until, self._proxy_ids, self._connector_ids)

    def _log_workarounds(self, workarounds: _Workarounds) -> None:
        """Log each workaround the previous full compile did not make: once when it starts, not every compile.

        A busy install compiles every few seconds, as its writes are
        announced; a workaround that ends and comes back is logged again.
        """
        for key, fields in workarounds.items():
            if key not in self._logged:
                logger.warning(key[0], **fields)
        self._logged = set(workarounds)


def _section_proxy(proxy: Proxy, credential: Credential | None, connector: Connector) -> Proxy:
    """A stored proxy as a section carries it: placeholders filled, the display-only metadata left out."""
    resolved = resolve_credentials(proxy, credential, connector)
    if not DISPLAY_ONLY_METADATA.isdisjoint(resolved.metadata):
        # A new dict: the copy shares the stored row's.
        resolved.metadata = {key: value for key, value in resolved.metadata.items() if key not in DISPLAY_ONLY_METADATA}
    return resolved


def _enabled_specs(records: Iterable[ProviderRecord]) -> list[dict[str, Any]]:
    """The enabled custom descriptors' documents, by id: what the registry runs and gateways load.

    The repository reads them by id too, so the provider stores that apply
    its list skip this one as unchanged.
    """
    return [record.spec for record in sorted(records, key=lambda r: r.id) if record.enabled]


def _database_feeds(records: Iterable[GeoDatabaseRecord]) -> list[GeoDatabaseFeed]:
    """The stored IP databases gateways fetch by checksum, by ID, without download credentials or schedule.

    Config-file databases and rows with no file yet are left out: there is nothing to fetch.
    """
    return [
        _narrow(GeoDatabaseFeed, record)
        for record in sorted(records, key=lambda r: r.id)
        if record.source != GeoDatabaseSource.PATH and record.sha256
    ]


async def _wireguard_feed(session: AsyncSession) -> WireGuardFeed | None:
    """The WireGuard endpoint as gateways run it, devices by id; None while the protocol has no settings row."""
    settings = await WireGuardSettingsRepository(session).get()
    if settings is None:
        return None
    peers = await WireGuardPeerRepository(session).get_all()
    return WireGuardFeed(
        server=_narrow(WireGuardServerFeed, settings),
        peers=[_narrow(WireGuardPeerFeed, peer) for peer in sorted(peers, key=lambda p: p.id)],
    )


async def _openvpn_feed(session: AsyncSession) -> OpenVpnFeed | None:
    """The OpenVPN endpoint as gateways run it, devices by id; None while the protocol has no settings row."""
    settings = await OpenVpnSettingsRepository(session).get()
    if settings is None:
        return None
    peers = await OpenVpnPeerRepository(session).get_all()
    return OpenVpnFeed(
        server=_narrow(OpenVpnServerFeed, settings),
        peers=[_narrow(OpenVpnPeerFeed, peer) for peer in sorted(peers, key=lambda p: p.id)],
    )


def _narrow[M: BaseModel](model: type[M], source: BaseModel) -> M:
    """``source`` cut down to ``model``'s fields: each feed model is a strict subset of the one it narrows.

    What the feed model has no field for (private keys, the CA key,
    download credentials, sightings) never leaves Postgres.
    """
    return model.model_validate(source.model_dump(include=set(model.model_fields)))
