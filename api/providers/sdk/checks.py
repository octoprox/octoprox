# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The checks a descriptor passes before it is saved, tested or asked for options: egress and expressions, no network."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from api.providers.sdk.egress import EgressDeniedError, EgressGuard
from api.providers.sdk.extract import ExtractionError, ValueExtractor
from api.providers.sdk.templating import TemplateRenderer

if TYPE_CHECKING:
    from api.providers.sdk.descriptor import ProviderDescriptor
    from api.providers.sdk.egress import EgressPolicy


def static_checks(
    descriptor: ProviderDescriptor, policy: EgressPolicy
) -> tuple[list[str], list[str]]:
    """Egress/expression checks that need no network; returns ``(errors, warnings)``."""
    errors: list[str] = []
    warnings: list[str] = []
    guard = EgressGuard(policy)
    for call in descriptor.iter_calls():
        try:
            guard.check_static(call.url)
        except EgressDeniedError as exc:
            errors.append(str(exc))
    for host in descriptor.gateway_hosts():
        if "{" in host:
            continue
        try:
            EgressGuard(policy).check_static(f"https://{host}/")
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
    country_field = descriptor.country_field()
    for ptype in descriptor.proxy_types:
        for template in (ptype.host, ptype.username, ptype.password):
            for path in TemplateRenderer.referenced_paths(template):
                if path.startswith(("credential.", "connector.")) and path not in known_fields:
                    warnings.append(f"proxy type '{ptype.key}' references unknown field '{path}'")
                elif country_field is not None and path == f"connector.{country_field.key}":
                    warnings.append(
                        f"proxy type '{ptype.key}' reads the connector's country list through '{path}'; "
                        "use {geo.country} for the country a slot or request targets"
                    )
    if not descriptor.credential_fields:
        warnings.append("descriptor has no credential fields")
    return errors, warnings


def _all_expressions(descriptor: ProviderDescriptor) -> list[str]:
    """Every extraction expression in the descriptor: its auth flows', options', validation's and proxy types'."""
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
                ptype.source.items,
                ptype.source.host,
                ptype.source.port,
                ptype.source.username,
                ptype.source.password,
                ptype.source.protocol,
                ptype.source.country,
                ptype.source.identity,
                ptype.source.filter,
            ]
            for value in list_sources:
                add(value)
        if ptype.discovery is not None and ptype.discovery.ip_path != "@text":
            add(ptype.discovery.ip_path)
    return paths
