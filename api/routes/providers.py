# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Provider catalog and admin-managed provider descriptors.

Read endpoints serve the catalog every authenticated user needs to render
credential/connector forms. Mutations are admin-only, require the caller to
confirm the vendor hosts a descriptor will send credentials to, are audited,
and fan out to peer instances through the event bus.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.core import utc_now
from api.core.auth import CurrentUser, RequireAdminDep, RequireEditorDep
from api.core.event_bus import event_bus
from api.core.proxy_manager import ProxyManager
from api.core.signals import provider_changed
from api.db.repository import ProviderAuditRepository, ProviderDescriptorRepository
from api.db.session import get_db
from api.models.cloud_options import COUNTRIES
from api.models.provider import (
    ProviderAuditEntry,
    ProviderAuditResponse,
    ProviderCreate,
    ProviderDetail,
    ProviderImportRequest,
    ProviderListResponse,
    ProviderOptionsRequest,
    ProviderOptionsResponse,
    ProviderRecord,
    ProviderSummary,
    ProviderTestRequest,
    ProviderTestResponse,
    ProviderUpdate,
    ProviderValidateRequest,
    ProviderValidateResponse,
)
from api.providers.registry import ProviderRegistry, ProviderType, get_provider_registry
from api.providers.sdk.descriptor import FieldSpec, ProviderDescriptor
from api.providers.sdk.discovery import DescriptorTester, OptionsResolver, ResolvedOption
from api.providers.sdk.egress import EgressDeniedError, EgressGuard
from api.providers.sdk.extract import ExtractionError, ValueExtractor
from api.providers.sdk.loader import (
    DescriptorLoadError,
    descriptor_from_dict,
    descriptor_from_yaml,
    descriptor_to_yaml,
)
from api.providers.sdk.templating import TemplateRenderer

logger = structlog.get_logger()

router = APIRouter(prefix="/providers")

DbDep = Annotated[AsyncSession, Depends(get_db)]


# --- helpers -----------------------------------------------------------------------


def _usage(proxy_manager: ProxyManager, type_id: str) -> tuple[int, int]:
    """Credential and connector counts for a provider type."""
    credential_ids = {c.id for c in proxy_manager.credentials if c.type == type_id}
    connectors = sum(1 for c in proxy_manager.connectors if c.credential_id in credential_ids)
    return len(credential_ids), connectors


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


def _summary(proxy_manager: ProxyManager, ptype: ProviderType, record: ProviderRecord | None = None) -> ProviderSummary:
    credential_count, connector_count = _usage(proxy_manager, ptype.id)
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
        docs_url=ptype.docs_url,
        credential_fields=_with_dependencies(ptype, ptype.credential_fields),
        connector_fields=_with_dependencies(ptype, ptype.connector_fields),
        proxy_type_field=descriptor.proxy_type_field if descriptor else None,
        proxy_types=[{"key": t.key, "label": t.label, "mode": t.mode} for t in descriptor.proxy_types]
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


def _detail(proxy_manager: ProxyManager, ptype: ProviderType, record: ProviderRecord | None = None) -> ProviderDetail:
    summary = _summary(proxy_manager, ptype, record)
    return ProviderDetail(**summary.model_dump(), spec=ptype.descriptor, origin=ptype.origin)


def _parse_spec(spec: dict[str, Any]) -> ProviderDescriptor:
    try:
        return descriptor_from_dict(spec)
    except DescriptorLoadError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid provider descriptor: {exc}") from None


def _static_checks(descriptor: ProviderDescriptor, registry: ProviderRegistry) -> tuple[list[str], list[str]]:
    """Egress/expression checks that need no network; returns ``(errors, warnings)``."""
    errors: list[str] = []
    warnings: list[str] = []
    guard = EgressGuard(registry.runtime.egress_policy)
    for call in descriptor.iter_calls():
        try:
            guard.check_static(call.url)
        except EgressDeniedError as exc:
            errors.append(str(exc))
    for host in descriptor.gateway_hosts():
        if "{" in host:
            continue
        try:
            EgressGuard(registry.runtime.egress_policy).check_static(f"https://{host}/")
        except EgressDeniedError as exc:
            errors.append(f"gateway host: {exc}")
    extractor = ValueExtractor()
    for path in _all_expressions(descriptor):
        try:
            extractor.validate_expression(path)
        except ExtractionError as exc:
            errors.append(str(exc))
    known_fields = {f"credential.{f.key}" for f in descriptor.credential_fields} | {
        f"connector.{f.key}" for f in descriptor.connector_fields
    }
    if descriptor.validation is not None:
        known_fields |= {f"credential.{k}" for k in descriptor.validation.capture}
    for ptype in descriptor.proxy_types:
        for template in (ptype.host, ptype.username, ptype.password):
            for path in TemplateRenderer.referenced_paths(template):
                if path.startswith(("credential.", "connector.")) and path not in known_fields:
                    warnings.append(f"proxy type '{ptype.key}' references unknown field '{path}'")
    if not descriptor.credential_fields:
        warnings.append("descriptor has no credential fields")
    return errors, warnings


def _all_expressions(descriptor: ProviderDescriptor) -> list[str]:
    paths: list[str] = []

    def add(source: Any) -> None:
        if source is None:
            return
        paths.append(source if isinstance(source, str) else source.path)

    for flow in descriptor.auth.values():
        add(flow.token_path)
    for source in descriptor.options.values():
        add(source.items)
        add(source.value)
        add(source.label)
        add(source.description)
        add(source.filter)
        for value in source.extra.values():
            add(value)
        for enrich in source.enrich:
            add(enrich.when)
            for value in enrich.merge.values():
                add(value)
    if descriptor.validation is not None:
        add(descriptor.validation.success)
        for value in descriptor.validation.capture.values():
            add(value)
    for ptype in descriptor.proxy_types:
        if ptype.known_ips is not None:
            add(ptype.known_ips.items)
            add(ptype.known_ips.ip)
            add(ptype.known_ips.country)
        if ptype.source is not None:
            list_sources: list[Any] = [
                ptype.source.items, ptype.source.host, ptype.source.port, ptype.source.username,
                ptype.source.password, ptype.source.protocol, ptype.source.country,
                ptype.source.identity, ptype.source.filter,
            ]
            for value in list_sources:
                add(value)
        if ptype.discovery is not None and ptype.discovery.ip_path != "@text":
            add(ptype.discovery.ip_path)
    return paths


def _require_host_confirmation(
    descriptor: ProviderDescriptor, confirmed: list[str], already_confirmed: set[str] | None = None
) -> None:
    """Refuse (409) unless every egress host not previously accepted is in ``confirmed``."""
    hosts = descriptor.egress_hosts()
    unconfirmed = sorted(set(hosts) - set(confirmed) - (already_confirmed or set()))
    if unconfirmed:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": "Confirm the hosts this provider will send credentials to",
                "egress_hosts": hosts,
                "unconfirmed_hosts": unconfirmed,
            },
        )


def _editable_type(registry: ProviderRegistry, provider_id: str) -> ProviderType:
    ptype = registry.get(provider_id)
    if ptype is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    if not ptype.editable:
        raise HTTPException(
            status_code=409, detail=f"Provider '{provider_id}' is {ptype.source} and cannot be modified"
        )
    return ptype


async def _audit(
    session: AsyncSession,
    provider_id: str,
    action: str,
    actor: CurrentUser,
    descriptor: ProviderDescriptor | None,
    hosts_changed: bool,
) -> None:
    await ProviderAuditRepository(session).add(
        ProviderAuditEntry(
            provider_id=provider_id,
            action=action,  # type: ignore[arg-type]
            actor=actor.username,
            egress_hosts=descriptor.egress_hosts() if descriptor else [],
            hosts_changed=hosts_changed,
            spec=descriptor.model_dump(mode="json", by_alias=True) if descriptor else None,
        )
    )


async def _publish(provider_id: str, op: str) -> None:
    await event_bus.publish(provider_changed, None, entity_id=provider_id, op=op)


# --- catalog (all authenticated users) ------------------------------------------------


@router.get("", response_model=ProviderListResponse)
async def list_providers(request: Request, session: DbDep) -> ProviderListResponse:
    """Catalog of every provider type with its form schemas."""
    registry = get_provider_registry()
    records = {r.id: r for r in await ProviderDescriptorRepository(session).get_all()}
    proxy_manager: ProxyManager = request.app.state.proxy_manager
    providers = [_summary(proxy_manager, t, records.get(t.id)) for t in registry.list()]
    return ProviderListResponse(
        total=len(providers), providers=providers, presets=registry.presets, countries=COUNTRIES
    )


@router.get("/{provider_id}", response_model=ProviderDetail)
async def get_provider(request: Request, provider_id: str, session: DbDep) -> ProviderDetail:
    registry = get_provider_registry()
    ptype = registry.get(provider_id)
    if ptype is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    record = await ProviderDescriptorRepository(session).get_by_id(provider_id)
    return _detail(request.app.state.proxy_manager, ptype, record)


@router.post("/{provider_id}/options/{option_name}", response_model=ProviderOptionsResponse)
async def resolve_options(
    request: Request,
    provider_id: str,
    option_name: str,
    body: ProviderOptionsRequest,
    _guard: RequireEditorDep,
) -> ProviderOptionsResponse:
    """Resolve a descriptor's dynamic select options for a credential.

    Editors may pass a saved ``credential_id`` or, while creating a credential,
    the in-progress ``credential_config``.
    """
    registry = get_provider_registry()
    ptype = registry.get(provider_id)
    if ptype is None or ptype.descriptor is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    if option_name not in ptype.descriptor.options:
        raise HTTPException(status_code=404, detail=f"Provider has no options source '{option_name}'")
    credential_config: dict[str, Any] | None = body.credential_config
    if body.credential_id:
        proxy_manager = request.app.state.proxy_manager
        credential = proxy_manager.get_credential(body.credential_id)
        if credential is None or credential.type != provider_id:
            raise HTTPException(status_code=404, detail="Credential not found for this provider")
        credential_config = credential.config
    if credential_config is None:
        raise HTTPException(status_code=422, detail="credential_id or credential_config is required")
    resolver = OptionsResolver(ptype.descriptor, registry.runtime)
    outcome = await resolver.resolve(option_name, credential_config, body.connector_config)
    if not outcome.ok:
        raise HTTPException(status_code=502, detail=outcome.message)
    options: list[ResolvedOption] = outcome.result
    return ProviderOptionsResponse(
        options=[o.model_dump() for o in options], cached=not outcome.traces
    )


# --- admin: authoring -----------------------------------------------------------------


@router.post("/validate", response_model=ProviderValidateResponse)
async def validate_provider(
    request: Request, body: ProviderValidateRequest, _admin: RequireAdminDep
) -> ProviderValidateResponse:
    """Dry-run: parse a descriptor and report hosts, errors and warnings."""
    registry = get_provider_registry()
    try:
        descriptor = descriptor_from_dict(body.spec)
    except DescriptorLoadError as exc:
        return ProviderValidateResponse(valid=False, errors=[str(exc)])
    errors, warnings = _static_checks(descriptor, registry)
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


async def _create(
    request: Request,
    session: AsyncSession,
    descriptor: ProviderDescriptor,
    confirmed_hosts: list[str],
    enabled: bool,
    actor: CurrentUser,
    action: str,
) -> ProviderDetail:
    registry = get_provider_registry()
    repo = ProviderDescriptorRepository(session)
    if registry.get(descriptor.id) is not None or await repo.get_by_id(descriptor.id) is not None:
        raise HTTPException(status_code=409, detail=f"Provider id '{descriptor.id}' already exists")
    errors, _warnings = _static_checks(descriptor, registry)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    _require_host_confirmation(descriptor, confirmed_hosts)

    record = ProviderRecord(
        id=descriptor.id,
        name=descriptor.name,
        spec=descriptor.model_dump(mode="json", by_alias=True),
        enabled=enabled,
        version=descriptor.version,
        created_by=actor.username,
    )
    await repo.create(record)
    await _audit(session, descriptor.id, action, actor, descriptor, hosts_changed=bool(descriptor.egress_hosts()))
    await session.commit()

    proxy_manager: ProxyManager = request.app.state.proxy_manager
    proxy_manager.provider_store.apply_record(record, descriptor.id)
    await _publish(descriptor.id, "added")
    logger.info("Provider descriptor created", provider_id=descriptor.id, actor=actor.username,
                egress_hosts=descriptor.egress_hosts())
    ptype = registry.get(descriptor.id)
    if ptype is None:
        raise HTTPException(status_code=500, detail="Provider was stored but could not be registered")
    return _detail(proxy_manager, ptype, record)


@router.post("", response_model=ProviderDetail, status_code=201)
async def create_provider(
    request: Request, session: DbDep, body: ProviderCreate, admin: RequireAdminDep
) -> ProviderDetail:
    """Create a custom provider descriptor (admin)."""
    descriptor = _parse_spec(body.spec)
    return await _create(request, session, descriptor, body.confirmed_hosts, body.enabled, admin, "created")


@router.post("/import", response_model=ProviderDetail, status_code=201)
async def import_provider(
    request: Request, session: DbDep, body: ProviderImportRequest, admin: RequireAdminDep
) -> ProviderDetail:
    """Create a custom provider from a YAML document (admin)."""
    try:
        descriptor = descriptor_from_yaml(body.yaml)
    except DescriptorLoadError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid provider descriptor: {exc}") from None
    registry = get_provider_registry()
    existing = registry.get(descriptor.id)
    if existing is not None and existing.editable and body.replace:
        return await _update(request, session, existing, descriptor, body.confirmed_hosts, None, admin)
    return await _create(request, session, descriptor, body.confirmed_hosts, True, admin, "imported")


async def _update(
    request: Request,
    session: AsyncSession,
    ptype: ProviderType,
    descriptor: ProviderDescriptor | None,
    confirmed_hosts: list[str],
    enabled: bool | None,
    actor: CurrentUser,
) -> ProviderDetail:
    registry = get_provider_registry()
    repo = ProviderDescriptorRepository(session)
    record = await repo.get_by_id(ptype.id)
    if record is None:
        raise HTTPException(status_code=404, detail="Provider not found")

    hosts_changed = False
    if descriptor is not None:
        if descriptor.id != ptype.id:
            raise HTTPException(status_code=422, detail="A provider's id cannot be changed")
        errors, _warnings = _static_checks(descriptor, registry)
        if errors:
            raise HTTPException(status_code=422, detail="; ".join(errors))
        old_hosts = set(ptype.egress_hosts())
        new_hosts = set(descriptor.egress_hosts())
        hosts_changed = new_hosts != old_hosts
        if new_hosts - old_hosts:
            _require_host_confirmation(descriptor, confirmed_hosts, already_confirmed=old_hosts)
        record.spec = descriptor.model_dump(mode="json", by_alias=True)
        record.name = descriptor.name
        record.version = record.version + 1
    if enabled is not None:
        record.enabled = enabled
    record.updated_at = utc_now()
    await repo.update(record)
    await _audit(session, ptype.id, "updated", actor, descriptor or ptype.descriptor, hosts_changed)
    await session.commit()

    proxy_manager: ProxyManager = request.app.state.proxy_manager
    proxy_manager.provider_store.apply_record(record, ptype.id)
    await _publish(ptype.id, "updated")
    logger.info("Provider descriptor updated", provider_id=ptype.id, actor=actor.username, hosts_changed=hosts_changed)
    refreshed = registry.get(ptype.id)
    if refreshed is None:
        # Disabled providers leave the registry; report from the stored record.
        return ProviderDetail(
            **_summary(proxy_manager, ptype, record).model_dump(), spec=descriptor or ptype.descriptor, origin="database"
        )
    return _detail(proxy_manager, refreshed, record)


@router.put("/{provider_id}", response_model=ProviderDetail)
async def update_provider(
    request: Request, session: DbDep, provider_id: str, body: ProviderUpdate, admin: RequireAdminDep
) -> ProviderDetail:
    """Update a custom provider descriptor (admin)."""
    ptype = _editable_type(get_provider_registry(), provider_id)
    descriptor = _parse_spec(body.spec) if body.spec is not None else None
    return await _update(request, session, ptype, descriptor, body.confirmed_hosts, body.enabled, admin)


@router.delete("/{provider_id}", status_code=204)
async def delete_provider(
    request: Request, session: DbDep, provider_id: str, admin: RequireAdminDep
) -> None:
    """Delete a custom provider descriptor (admin). Refused while credentials use it."""
    registry = get_provider_registry()
    ptype = _editable_type(registry, provider_id)
    credential_count, _ = _usage(request.app.state.proxy_manager, provider_id)
    if credential_count:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete provider: {credential_count} credential(s) use it",
        )
    await ProviderDescriptorRepository(session).delete(provider_id)
    await _audit(session, provider_id, "deleted", admin, ptype.descriptor, hosts_changed=False)
    await session.commit()
    registry.unregister(provider_id)
    await _publish(provider_id, "removed")
    logger.info("Provider descriptor deleted", provider_id=provider_id, actor=admin.username)


@router.get("/{provider_id}/export")
async def export_provider(request: Request, provider_id: str, _admin: RequireAdminDep) -> Response:
    """Download a descriptor as YAML (admin). Built-ins can be exported as a starting point."""
    ptype = get_provider_registry().get(provider_id)
    if ptype is None or ptype.descriptor is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    return Response(
        content=descriptor_to_yaml(ptype.descriptor),
        media_type="application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{provider_id}.yaml"'},
    )


@router.get("/{provider_id}/audit", response_model=ProviderAuditResponse)
async def provider_audit(
    request: Request, session: DbDep, provider_id: str, _admin: RequireAdminDep
) -> ProviderAuditResponse:
    entries = await ProviderAuditRepository(session).get_for_provider(provider_id)
    return ProviderAuditResponse(total=len(entries), entries=entries)


@router.post("/{provider_id}/test", response_model=ProviderTestResponse)
async def test_provider(
    request: Request, provider_id: str, body: ProviderTestRequest, _admin: RequireAdminDep
) -> ProviderTestResponse:
    """Exercise a descriptor's vendor calls with throwaway config (admin).

    ``spec`` lets the builder test an unsaved draft; static egress checks still
    apply, so a draft cannot be used to probe private networks.
    """
    registry = get_provider_registry()
    if body.spec is not None:
        descriptor = _parse_spec(body.spec)
        errors, _warnings = _static_checks(descriptor, registry)
        if errors:
            raise HTTPException(status_code=422, detail="; ".join(errors))
    else:
        ptype = registry.get(provider_id)
        if ptype is None or ptype.descriptor is None:
            raise HTTPException(status_code=404, detail="Provider not found")
        descriptor = ptype.descriptor
    tester = DescriptorTester(descriptor, registry.runtime)
    outcome = await tester.run(body.action, body.credential_config, body.connector_config, body.option_name)
    return ProviderTestResponse(
        ok=outcome.ok, message=outcome.message, result=outcome.result, traces=outcome.trace_dicts()
    )
