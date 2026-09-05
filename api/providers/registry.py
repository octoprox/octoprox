# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Provider registry: the single lookup table for every provider type.

A *provider type* is anything a credential's ``type`` can point at: the four
code-implemented types (static, AWS, GCP, Azure), the descriptors shipped in
``api/providers/builtin``, descriptors mounted by the operator, plugin entry
points, and admin-authored descriptors stored in Postgres. The registry hides
those origins behind one interface used by routes (form schemas, validation),
the provider syncer (provider factories) and the proxy manager (cloud checks).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import structlog

from api.models.cloud_options import COUNTRIES
from api.models.connector import Connector
from api.models.credential import Credential
from api.providers.base import SyncableProvider
from api.providers.code_types import code_provider_definitions, code_validators
from api.providers.sdk.descriptor import FieldSpec, OptionSpec, ProviderDescriptor
from api.providers.sdk.egress import EgressPolicy
from api.providers.sdk.loader import (
    load_builtin_descriptors,
    load_directory,
    load_entry_points,
)
from api.providers.sdk.provider import DescriptorProvider, SdkRuntime
from api.providers.sdk.validation import FieldSetValidator

logger = structlog.get_logger()

ProviderKind = Literal["code", "descriptor"]
ProviderSource = Literal["builtin", "file", "plugin", "custom"]
ProviderFactory = Callable[[Connector, Credential], SyncableProvider]
CredentialValidatorFn = Callable[[dict[str, Any]], dict[str, Any]]
ConnectorValidatorFn = Callable[[dict[str, Any], dict[str, Any] | None], dict[str, Any]]


class UnknownProviderError(ValueError):
    """The credential type has no registered provider."""


@dataclass
class ProviderType:
    """Everything the application needs to know about one provider type."""

    id: str
    name: str
    description: str
    kind: ProviderKind
    source: ProviderSource
    credential_fields: list[FieldSpec]
    connector_fields: list[FieldSpec]
    credential_validator: CredentialValidatorFn
    connector_validator: ConnectorValidatorFn
    syncable: bool = False
    cloud: bool = False
    descriptor: ProviderDescriptor | None = None
    factory: ProviderFactory | None = None
    origin: str = ""
    logo: str | None = None
    docs_url: str | None = None
    beta: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def editable(self) -> bool:
        return self.source == "custom"

    def egress_hosts(self) -> list[str]:
        return self.descriptor.egress_hosts() if self.descriptor else []

    def gateway_hosts(self) -> list[str]:
        return self.descriptor.gateway_hosts() if self.descriptor else []

    def discovery_hosts(self) -> list[str]:
        return self.descriptor.discovery_hosts() if self.descriptor else []


def countries_preset() -> list[OptionSpec]:
    return [OptionSpec(value=c.code, label=c.name) for c in COUNTRIES]


class ProviderRegistry:
    """Mutable, process-local registry of provider types."""

    def __init__(self, runtime: SdkRuntime | None = None) -> None:
        self._types: dict[str, ProviderType] = {}
        self._runtime = runtime or SdkRuntime()
        self._presets: dict[str, list[OptionSpec]] = {"countries": countries_preset()}

    # --- runtime --------------------------------------------------------------------

    @property
    def runtime(self) -> SdkRuntime:
        return self._runtime

    @property
    def presets(self) -> dict[str, list[OptionSpec]]:
        return self._presets

    # --- registration -------------------------------------------------------------

    def register(self, ptype: ProviderType) -> ProviderType:
        existing = self._types.get(ptype.id)
        if existing is not None and existing.source != ptype.source:
            raise ValueError(
                f"provider id '{ptype.id}' is already taken by a {existing.source} provider"
            )
        self._types[ptype.id] = ptype
        logger.debug("Registered provider type", provider_id=ptype.id, source=ptype.source, kind=ptype.kind)
        return ptype

    def unregister(self, type_id: str) -> bool:
        return self._types.pop(type_id, None) is not None

    def register_code_type(
        self,
        *,
        type_id: str,
        name: str,
        description: str,
        credential_fields: list[FieldSpec],
        connector_fields: list[FieldSpec],
        cloud: bool,
    ) -> ProviderType:
        credential_validator, connector_validator = code_validators(type_id)
        return self.register(
            ProviderType(
                id=type_id,
                name=name,
                description=description,
                kind="code",
                source="builtin",
                credential_fields=credential_fields,
                connector_fields=connector_fields,
                credential_validator=credential_validator,
                connector_validator=connector_validator,
                syncable=False,
                cloud=cloud,
                origin="code",
            )
        )

    def register_descriptor(
        self,
        descriptor: ProviderDescriptor,
        source: ProviderSource,
        origin: str = "",
        provider_class: type[Any] | None = None,
    ) -> ProviderType:
        """Register a descriptor-driven provider (optionally backed by a plugin class)."""
        capture_keys = set(descriptor.validation.capture) if descriptor.validation else set()
        credential_validator = FieldSetValidator(
            descriptor.credential_fields, "credential", presets=self._presets, extra_allowed=capture_keys
        )
        connector_validator = FieldSetValidator(
            descriptor.connector_fields, "connector", presets=self._presets
        )
        runtime = self._runtime

        def factory(connector: Connector, credential: Credential) -> SyncableProvider:
            if provider_class is not None:
                provider: SyncableProvider = provider_class(connector, credential)
                return provider
            return DescriptorProvider(descriptor, connector, credential, runtime)

        def validate_credential(config: dict[str, Any]) -> dict[str, Any]:
            return credential_validator.validate(config)

        def validate_connector(config: dict[str, Any], credential_config: dict[str, Any] | None) -> dict[str, Any]:
            return connector_validator.validate(config, credential_config)

        return self.register(
            ProviderType(
                id=descriptor.id,
                name=descriptor.name,
                description=descriptor.description,
                kind="descriptor",
                source=source,
                credential_fields=descriptor.credential_fields,
                connector_fields=descriptor.connector_fields,
                credential_validator=validate_credential,
                connector_validator=validate_connector,
                syncable=True,
                cloud=False,
                descriptor=descriptor,
                factory=factory,
                origin=origin,
                logo=descriptor.logo,
                docs_url=descriptor.docs_url,
                beta=descriptor.beta,
            )
        )

    def replace_custom(self, descriptors: list[ProviderDescriptor]) -> None:
        """Make the set of ``custom`` (database) providers exactly ``descriptors``."""
        wanted = {d.id for d in descriptors}
        for type_id in [t.id for t in self._types.values() if t.source == "custom" and t.id not in wanted]:
            self.unregister(type_id)
        for descriptor in descriptors:
            existing = self._types.get(descriptor.id)
            if existing is not None and existing.source != "custom":
                logger.warning(
                    "Custom provider shadows a built-in id and was skipped", provider_id=descriptor.id
                )
                continue
            self.register_descriptor(descriptor, "custom", origin="database")

    # --- lookup --------------------------------------------------------------------

    def get(self, type_id: str) -> ProviderType | None:
        return self._types.get(type_id)

    def require(self, type_id: str) -> ProviderType:
        ptype = self._types.get(type_id)
        if ptype is None:
            raise UnknownProviderError(f"Unknown credential type: {type_id}")
        return ptype

    def list(self) -> list[ProviderType]:
        return sorted(self._types.values(), key=lambda t: (t.kind != "code", t.name.lower()))

    def ids(self) -> set[str]:
        return set(self._types)

    def is_known(self, type_id: str) -> bool:
        return type_id in self._types

    def is_syncable(self, type_id: str) -> bool:
        ptype = self._types.get(type_id)
        return bool(ptype and ptype.syncable)

    def is_cloud(self, type_id: str) -> bool:
        ptype = self._types.get(type_id)
        return bool(ptype and ptype.cloud)

    def create_provider(self, connector: Connector, credential: Credential) -> SyncableProvider | None:
        ptype = self._types.get(credential.type)
        if ptype is None or ptype.factory is None:
            return None
        return ptype.factory(connector, credential)

    # --- validation -------------------------------------------------------------

    def validate_credential_config(self, type_id: str, config: dict[str, Any]) -> dict[str, Any]:
        return self.require(type_id).credential_validator(config)

    def validate_connector_config(
        self, type_id: str, config: dict[str, Any], credential_config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.require(type_id).connector_validator(config, credential_config)

    def reset(self) -> None:
        self._types.clear()


def build_registry(
    *,
    runtime: SdkRuntime | None = None,
    providers_dir: Path | None = None,
    include_plugins: bool = True,
) -> ProviderRegistry:
    """Assemble a registry with code types, built-ins, operator files and plugins."""
    registry = ProviderRegistry(runtime)
    for definition in code_provider_definitions():
        registry.register_code_type(
            type_id=definition["id"],
            name=definition["name"],
            description=definition["description"],
            credential_fields=definition["credential_fields"],
            connector_fields=definition["connector_fields"],
            cloud=definition["cloud"],
        )
    for loaded in load_builtin_descriptors():
        registry.register_descriptor(loaded.descriptor, "builtin", loaded.origin)
    if providers_dir is not None:
        for loaded in load_directory(providers_dir):
            try:
                registry.register_descriptor(loaded.descriptor, "file", loaded.origin)
                logger.info(
                    "Loaded operator provider descriptor",
                    provider_id=loaded.descriptor.id,
                    path=loaded.origin,
                    egress_hosts=loaded.descriptor.egress_hosts(),
                )
            except ValueError as exc:
                logger.error("Skipping provider descriptor", path=loaded.origin, error=str(exc))
    if include_plugins:
        for plugin in load_entry_points():
            try:
                registry.register_descriptor(
                    plugin.descriptor, "plugin", plugin.origin, provider_class=plugin.provider_class
                )
                logger.info("Loaded provider plugin", provider_id=plugin.descriptor.id, origin=plugin.origin)
            except ValueError as exc:
                logger.error("Skipping provider plugin", origin=plugin.origin, error=str(exc))
    return registry


def runtime_from_settings(settings: Any) -> SdkRuntime:
    """Build the SDK runtime from application settings."""
    return SdkRuntime(
        egress_policy=EgressPolicy(
            allow_http=bool(settings.provider_egress_allow_http),
            allow_private=bool(settings.provider_egress_allow_private),
            pin_dns=not bool(settings.provider_egress_allow_private),
        ),
        http_timeout_seconds=float(settings.provider_http_timeout_seconds),
        max_response_bytes=int(settings.provider_http_max_response_bytes),
    )


@lru_cache
def get_provider_registry() -> ProviderRegistry:
    """Process-wide registry, built from settings on first use.

    Custom (database) descriptors are loaded separately by
    :class:`api.providers.store.ProviderStore` once a session factory exists.
    """
    from api.core.config import settings

    providers_dir = Path(settings.providers_dir) if settings.providers_dir else None
    return build_registry(runtime=runtime_from_settings(settings), providers_dir=providers_dir)


def reset_provider_registry() -> None:
    """Drop the cached registry (tests)."""
    get_provider_registry.cache_clear()


# --- compatibility shims used by the syncer -------------------------------------


def get_provider(
    credential_type: str, connector: Connector, credential: Credential
) -> SyncableProvider | None:
    """Instantiate a provider for ``credential_type`` (``None`` when not syncable)."""
    if credential.type != credential_type:
        credential = credential.model_copy(update={"type": credential_type})
    return get_provider_registry().create_provider(connector, credential)


def is_syncable_credential_type(credential_type: str) -> bool:
    return get_provider_registry().is_syncable(credential_type)
