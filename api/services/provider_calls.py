# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Live vendor calls through a descriptor: the connector form's dynamic selects, and the builder's test panel.

A draft descriptor passes the static egress checks first, as on save, so it cannot probe private networks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from api.db.repository import CredentialRepository
from api.models.provider import (
    ProviderOptionsRequest,
    ProviderOptionsResponse,
    ProviderTestRequest,
    ProviderTestResponse,
)
from api.models.user import UserRole
from api.providers.sdk.checks import static_checks
from api.providers.sdk.discovery import DescriptorTester, OptionsResolver, ResolvedOption
from api.providers.sdk.loader import DescriptorLoadError, descriptor_from_dict
from api.services.errors import ForbiddenError, InvalidConfigError, NotFoundError, UpstreamError

if TYPE_CHECKING:
    from api.core.auth import CurrentUser
    from api.db.session import SessionFactory
    from api.providers.registry import ProviderRegistry
    from api.providers.sdk.descriptor import ProviderDescriptor


class ProviderCallService:
    """Resolves a descriptor's dynamic select options and exercises a descriptor with throwaway config."""

    def __init__(
        self, session_factory: SessionFactory, provider_registry: ProviderRegistry
    ) -> None:
        self._session_factory = session_factory
        self._registry = provider_registry

    async def options(
        self, provider_id: str, option_name: str, data: ProviderOptionsRequest, caller: CurrentUser
    ) -> ProviderOptionsResponse:
        """A descriptor's dynamic select options for a saved ``credential_id`` or an in-progress
        ``credential_config``. Only an admin may pass an unsaved ``spec``, which sends credentials wherever it says."""
        registry = self._registry
        if data.spec is not None:
            if caller.role != UserRole.ADMIN:
                raise ForbiddenError("Only admins can resolve options for an unsaved descriptor")
            descriptor = self._draft(data.spec)
        else:
            descriptor = self._stored(provider_id)
        if option_name not in descriptor.options:
            raise NotFoundError(f"Provider has no options source '{option_name}'")
        credential_config: dict[str, Any] | None = data.credential_config
        if data.credential_id and data.spec is None:
            async with self._session_factory() as session:
                credential = await CredentialRepository(session).get_by_id(data.credential_id)
            if credential is None or credential.type != provider_id:
                raise NotFoundError("Credential not found for this provider")
            credential_config = credential.config
        if credential_config is None:
            raise InvalidConfigError("credential_id or credential_config is required")
        resolver = OptionsResolver(descriptor, registry.runtime)
        outcome = await resolver.resolve(option_name, credential_config, data.connector_config)
        if not outcome.ok:
            raise UpstreamError(outcome.message)
        options: list[ResolvedOption] = outcome.result
        return ProviderOptionsResponse(
            options=[o.model_dump() for o in options], cached=not outcome.traces
        )

    async def exercise(self, provider_id: str, data: ProviderTestRequest) -> ProviderTestResponse:
        """Run one action of a shipped, custom or draft descriptor, ``proxy_request`` fetching a URL through it."""
        descriptor = self._draft(data.spec) if data.spec is not None else self._stored(provider_id)
        tester = DescriptorTester(descriptor, self._registry.runtime)
        outcome = await tester.run(
            data.action,
            data.credential_config,
            data.connector_config,
            data.option_name,
            target_url=data.target_url,
        )
        return ProviderTestResponse(
            ok=outcome.ok,
            message=outcome.message,
            result=outcome.result,
            traces=outcome.trace_dicts(),
        )

    def _draft(self, spec: dict[str, Any]) -> ProviderDescriptor:
        """An unsaved descriptor, parsed and under the static egress checks; invalid when either fails."""
        try:
            descriptor = descriptor_from_dict(spec)
        except DescriptorLoadError as exc:
            raise InvalidConfigError(f"Invalid provider descriptor: {exc}") from None
        errors, _warnings = static_checks(descriptor, self._registry.runtime.egress_policy)
        if errors:
            raise InvalidConfigError("; ".join(errors))
        return descriptor

    def _stored(self, provider_id: str) -> ProviderDescriptor:
        """The registered descriptor of ``provider_id``; not found for a code type or an unknown id."""
        ptype = self._registry.get(provider_id)
        if ptype is None or ptype.descriptor is None:
            raise NotFoundError("Provider not found")
        return ptype.descriptor
