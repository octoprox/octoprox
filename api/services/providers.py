# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The provider catalog and the admin-managed descriptors: reads, the authoring dry run, and the writes announced with a
``descriptor`` entry.

The catalog reads and the writes apply the stored rows to this registry first, so any api sees what another just wrote.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

import structlog

from api.core import utc_now
from api.core.logging import error_text
from api.db.repository import CredentialRepository, ProviderDescriptorRepository
from api.db.streams import ChangeKind
from api.models.audit import AuditAction
from api.models.cloud_options import COUNTRIES
from api.models.provider import (
    ProviderCreate,
    ProviderDetail,
    ProviderImportRequest,
    ProviderListResponse,
    ProviderRecord,
    ProviderSummary,
    ProviderUpdate,
    ProviderValidateResponse,
)
from api.providers.sdk.checks import static_checks
from api.providers.sdk.loader import (
    DescriptorLoadError,
    descriptor_from_dict,
    descriptor_from_yaml,
    descriptor_to_yaml,
)
from api.providers.sdk.templating import TemplateRenderer
from api.services.errors import (
    ConflictError,
    HostConfirmationError,
    InvalidConfigError,
    NotFoundError,
    WriteRejectedError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.core.audit import Actor
    from api.db.session import SessionFactory
    from api.db.streams import ChangeLog
    from api.providers.registry import ProviderType
    from api.providers.sdk.descriptor import FieldSpec, ProviderDescriptor
    from api.providers.store import ProviderStore

logger = structlog.get_logger()


class ProviderNotRegisteredError(Exception):
    """A descriptor was stored and announced, but this process's registry does not hold it after applying the rows."""


class ProviderService:
    """Reads the provider catalog, dry-runs a descriptor, and creates, imports, edits and deletes custom descriptors.

    Args:
        provider_store: applies the stored descriptors to this process's registry, before a catalog read and after
            each write.
    """

    def __init__(
        self, session_factory: SessionFactory, provider_store: ProviderStore, *, changes: ChangeLog
    ) -> None:
        self._session_factory = session_factory
        self._store = provider_store
        self._registry = provider_store.registry
        self._changes = changes

    async def every(self) -> ProviderListResponse:
        """Catalog of every provider type with its form schemas, the stored descriptors as Postgres has them."""
        async with self._session_factory() as session:
            records = await self._read_through(session)
            usage = await CredentialRepository(session).usage_by_type()
        registry = self._registry
        providers = [self._summary(t, usage, records.get(t.id)) for t in registry.list()]
        return ProviderListResponse(
            total=len(providers), providers=providers, presets=registry.presets, countries=COUNTRIES
        )

    async def get(self, provider_id: str) -> ProviderDetail:
        """One provider type's catalog entry with its full descriptor, a stored one as Postgres has it."""
        async with self._session_factory() as session:
            records = await self._read_through(session)
            ptype = self._registry.get(provider_id)
            if ptype is None:
                raise NotFoundError("Provider not found")
            usage = await CredentialRepository(session).usage_by_type(provider_id)
        return self._detail(ptype, usage, records.get(provider_id))

    async def export(self, provider_id: str) -> str:
        """A descriptor as YAML, as stored; a built-in's too, as a starting point."""
        async with self._session_factory() as session:
            await self._read_through(session)
        ptype = self._registry.get(provider_id)
        if ptype is None or ptype.descriptor is None:
            raise NotFoundError("Provider not found")
        return descriptor_to_yaml(ptype.descriptor)

    def validate(self, spec: dict[str, Any]) -> ProviderValidateResponse:
        """Dry-run: parse a descriptor and report hosts, errors and warnings."""
        registry = self._registry
        try:
            descriptor = descriptor_from_dict(spec)
        except DescriptorLoadError as exc:
            return ProviderValidateResponse(valid=False, errors=[str(exc)])
        errors, warnings = static_checks(descriptor, registry.runtime.egress_policy)
        existing = registry.get(descriptor.id)
        if existing is not None and not existing.editable:
            errors.append(f"id '{descriptor.id}' belongs to a {existing.source} provider")
        return ProviderValidateResponse(
            valid=not errors,
            errors=errors,
            warnings=warnings,
            spec=descriptor,
            egress_hosts=descriptor.egress_hosts(),
            gateway_hosts=descriptor.gateway_hosts(),
            discovery_hosts=descriptor.discovery_hosts(),
            yaml=descriptor_to_yaml(descriptor),
        )

    async def create(self, data: ProviderCreate, actor: Actor) -> ProviderDetail:
        """Create a custom provider descriptor; :class:`ProviderNotRegisteredError` when stored but not applied."""
        descriptor = _parse_spec(data.spec)
        async with self._session_factory() as session:
            return await self._store_new(
                session, descriptor, data.confirmed_hosts, data.enabled, actor, AuditAction.CREATED
            )

    async def import_yaml(self, data: ProviderImportRequest, actor: Actor) -> ProviderDetail:
        """Create a custom provider from a YAML document, or replace the editable one of its id when ``replace``."""
        try:
            descriptor = descriptor_from_yaml(data.yaml)
        except DescriptorLoadError as exc:
            raise InvalidConfigError(f"Invalid provider descriptor: {exc}") from None
        async with self._session_factory() as session:
            await self._read_through(session)
            existing = self._registry.get(descriptor.id)
            if existing is not None and existing.editable and data.replace:
                return await self._store_edit(
                    session, existing, descriptor, data.confirmed_hosts, None, actor
                )
            return await self._store_new(
                session, descriptor, data.confirmed_hosts, True, actor, AuditAction.IMPORTED
            )

    async def update(self, provider_id: str, data: ProviderUpdate, actor: Actor) -> ProviderDetail:
        """Update a custom provider descriptor's document or enabled flag."""
        async with self._session_factory() as session:
            await self._read_through(session)
            ptype = self._editable_type(provider_id)
            descriptor = _parse_spec(data.spec) if data.spec is not None else None
            return await self._store_edit(
                session, ptype, descriptor, data.confirmed_hosts, data.enabled, actor
            )

    async def delete(self, provider_id: str, actor: Actor) -> None:
        """Delete a custom provider descriptor; refused while credentials use it."""
        async with self._session_factory() as session:
            records = await self._read_through(session)
            self._editable_type(provider_id)
            usage = await CredentialRepository(session).usage_by_type(provider_id)
            credential_count, _ = usage.get(provider_id, (0, 0))
            if credential_count:
                raise WriteRejectedError(
                    f"Cannot delete provider: {credential_count} credential(s) use it"
                )
            record = records.get(provider_id)
            if record is None:
                raise NotFoundError("Provider not found")
            # A delete that lost a race with another finds the row gone: that one records it.
            if await ProviderDescriptorRepository(session).delete(provider_id):
                await actor.deleted(session, record)
            await session.commit()
        await self._changes.record(ChangeKind.DESCRIPTOR, provider_id)
        await self._apply_stored()
        logger.info("Provider descriptor deleted", provider_id=provider_id, actor=actor.name)

    # --- Helpers ------------------------------------------------------------------------

    async def _apply_stored(self) -> None:
        """Apply the stored descriptors to this process's registry now, so this api's next answer shows its own
        write; a failure is logged, not raised, since the write is committed and its change entry applies them
        anyway."""
        try:
            await self._store.sync_all()
        except Exception as exc:
            logger.warning("Provider descriptors not applied after a write", error=error_text(exc))

    async def _read_through(self, session: AsyncSession) -> dict[str, ProviderRecord]:
        """Every stored descriptor by id, the enabled ones applied to this process's registry first, so another api's
        write shows before the refresher brings it."""
        records = await ProviderDescriptorRepository(session).get_all()
        # The enabled rows' documents in the repository's order, as sync_all applies them.
        self._store.apply_specs([record.spec for record in records if record.enabled])
        return {record.id: record for record in records}

    def _editable_type(self, provider_id: str) -> ProviderType:
        """The registered custom type ``provider_id`` names; not found, or a conflict for a built-in, file or plugin."""
        ptype = self._registry.get(provider_id)
        if ptype is None:
            raise NotFoundError("Provider not found")
        if not ptype.editable:
            raise ConflictError(
                f"Provider '{provider_id}' is {ptype.source} and cannot be modified"
            )
        return ptype

    async def _store_new(
        self,
        session: AsyncSession,
        descriptor: ProviderDescriptor,
        confirmed_hosts: list[str],
        enabled: bool,
        actor: Actor,
        action: AuditAction,
    ) -> ProviderDetail:
        """Store a new custom descriptor, record it in the audit log, and apply it to this process's registry."""
        registry = self._registry
        repo = ProviderDescriptorRepository(session)
        if (
            registry.get(descriptor.id) is not None
            or await repo.get_by_id(descriptor.id) is not None
        ):
            raise ConflictError(f"Provider id '{descriptor.id}' already exists")
        errors, _warnings = static_checks(descriptor, registry.runtime.egress_policy)
        if errors:
            raise InvalidConfigError("; ".join(errors))
        _require_host_confirmation(descriptor, confirmed_hosts)

        record = ProviderRecord(
            id=descriptor.id,
            name=descriptor.name,
            spec=descriptor.model_dump(mode="json", by_alias=True),
            enabled=enabled,
            version=descriptor.version,
            created_by=actor.name,
        )
        await repo.create(record)
        await actor.created(
            session, record, action=action, details=_hosts_details((), descriptor.egress_hosts())
        )
        await session.commit()

        await self._changes.record(ChangeKind.DESCRIPTOR, descriptor.id)
        await self._apply_stored()
        logger.info(
            "Provider descriptor created",
            provider_id=descriptor.id,
            actor=actor.name,
            egress_hosts=descriptor.egress_hosts(),
        )
        ptype = registry.get(descriptor.id)
        if ptype is None:
            raise ProviderNotRegisteredError(descriptor.id)
        return self._detail(
            ptype, await CredentialRepository(session).usage_by_type(descriptor.id), record
        )

    async def _store_edit(
        self,
        session: AsyncSession,
        ptype: ProviderType,
        descriptor: ProviderDescriptor | None,
        confirmed_hosts: list[str],
        enabled: bool | None,
        actor: Actor,
    ) -> ProviderDetail:
        """Store a custom descriptor's new document or enabled flag, record the change, and apply it to this process's
        registry."""
        registry = self._registry
        repo = ProviderDescriptorRepository(session)
        record = await repo.get_by_id(ptype.id)
        if record is None:
            raise NotFoundError("Provider not found")
        # The record is changed in place below; its audit entry compares with this copy.
        before = record.model_copy(deep=True)

        hosts_changed = False
        hosts: dict[str, list[str]] = {}
        if descriptor is not None:
            if descriptor.id != ptype.id:
                raise InvalidConfigError("A provider's id cannot be changed")
            errors, _warnings = static_checks(descriptor, registry.runtime.egress_policy)
            if errors:
                raise InvalidConfigError("; ".join(errors))
            old_hosts = set(ptype.egress_hosts())
            new_hosts = set(descriptor.egress_hosts())
            hosts_changed = new_hosts != old_hosts
            hosts = _hosts_details(old_hosts, new_hosts)
            if new_hosts - old_hosts:
                _require_host_confirmation(descriptor, confirmed_hosts, already_confirmed=old_hosts)
            record.spec = descriptor.model_dump(mode="json", by_alias=True)
            record.name = descriptor.name
            record.version = record.version + 1
        if enabled is not None:
            record.enabled = enabled
        record.updated_at = utc_now()
        await repo.update(record)
        await actor.updated(session, before, record, details=hosts)
        await session.commit()

        await self._changes.record(ChangeKind.DESCRIPTOR, ptype.id)
        await self._apply_stored()
        logger.info(
            "Provider descriptor updated",
            provider_id=ptype.id,
            actor=actor.name,
            hosts_changed=hosts_changed,
        )
        usage = await CredentialRepository(session).usage_by_type(ptype.id)
        refreshed = registry.get(ptype.id)
        if refreshed is None:
            # Disabled providers leave the registry; report from the stored record.
            return ProviderDetail(
                **self._summary(ptype, usage, record).model_dump(),
                spec=descriptor or ptype.descriptor,
                origin="database",
            )
        return self._detail(refreshed, usage, record)

    @classmethod
    def _summary(
        cls,
        ptype: ProviderType,
        usage: Mapping[str, tuple[int, int]],
        record: ProviderRecord | None = None,
    ) -> ProviderSummary:
        """The catalog entry of a provider type, with how many credentials and connectors use it."""
        credential_count, connector_count = usage.get(ptype.id, (0, 0))
        descriptor = ptype.descriptor
        return ProviderSummary(
            id=ptype.id,
            name=ptype.name,
            description=ptype.description,
            kind=ptype.kind,
            source=ptype.source,
            editable=ptype.editable,
            syncable=ptype.syncable,
            cloud=ptype.cloud,
            beta=ptype.beta,
            logo=ptype.logo,
            logo_dark=ptype.logo_dark,
            docs_url=ptype.docs_url,
            credential_fields=cls._with_dependencies(ptype, ptype.credential_fields),
            connector_fields=cls._with_dependencies(ptype, ptype.connector_fields),
            proxy_type_field=descriptor.proxy_type_field if descriptor else None,
            proxy_types=[
                {"key": t.key, "label": t.label, "mode": t.mode} for t in descriptor.proxy_types
            ]
            if descriptor
            else [],
            egress_hosts=ptype.egress_hosts(),
            gateway_hosts=ptype.gateway_hosts(),
            has_validation=bool(descriptor and descriptor.validation is not None),
            credential_count=credential_count,
            connector_count=connector_count,
            version=descriptor.version if descriptor else 1,
            updated_at=record.updated_at if record else None,
        )

    @classmethod
    def _detail(
        cls,
        ptype: ProviderType,
        usage: Mapping[str, tuple[int, int]],
        record: ProviderRecord | None = None,
    ) -> ProviderDetail:
        """The catalog entry with the full descriptor."""
        summary = cls._summary(ptype, usage, record)
        return ProviderDetail(**summary.model_dump(), spec=ptype.descriptor, origin=ptype.origin)

    @staticmethod
    def _with_dependencies(ptype: ProviderType, fields: list[FieldSpec]) -> list[FieldSpec]:
        """Annotate dynamic selects with the connector keys their options source reads.

        The UI sends only those keys with option requests, so options refetch when a
        dependency (e.g. the zone) changes and not on every keystroke elsewhere.
        """
        descriptor = ptype.descriptor
        if descriptor is None:
            return fields
        annotated: list[FieldSpec] = []
        for field in fields:
            source = descriptor.options.get(field.options_from) if field.options_from else None
            if source is None:
                annotated.append(field)
                continue
            paths: set[str] = set()
            for call in (source.call, *(e.call for e in source.enrich)):
                for text in (call.url, *call.headers.values(), *call.params.values()):
                    paths |= TemplateRenderer.referenced_paths(text)
                if call.body is not None:
                    paths |= TemplateRenderer.referenced_paths(json.dumps(call.body))
            depends_on = sorted(p.split(".", 1)[1] for p in paths if p.startswith("connector."))
            annotated.append(field.model_copy(update={"depends_on": depends_on}))
        return annotated


def _parse_spec(spec: dict[str, Any]) -> ProviderDescriptor:
    """The descriptor a spec describes; refused with its load error when it is not one."""
    try:
        return descriptor_from_dict(spec)
    except DescriptorLoadError as exc:
        raise InvalidConfigError(f"Invalid provider descriptor: {exc}") from None


def _require_host_confirmation(
    descriptor: ProviderDescriptor, confirmed: list[str], already_confirmed: set[str] | None = None
) -> None:
    """Refuse (409) unless every egress host not previously accepted is in ``confirmed``."""
    hosts = descriptor.egress_hosts()
    unconfirmed = sorted(set(hosts) - set(confirmed) - (already_confirmed or set()))
    if unconfirmed:
        raise HostConfirmationError(hosts, unconfirmed)


def _hosts_details(before: Iterable[str], after: Iterable[str]) -> dict[str, list[str]]:
    """The egress hosts a write added and removed, for its audit entry: each list only when it is not empty."""
    old, new = set(before), set(after)
    details: dict[str, list[str]] = {}
    if added := sorted(new - old):
        details["hosts_added"] = added
    if removed := sorted(old - new):
        details["hosts_removed"] = removed
    return details
