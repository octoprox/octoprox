# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Provider registry for mapping credential types to provider classes.

This module provides a centralized registry for proxy providers, enabling
the generic ProxyProviderSyncer to work with any registered provider type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from api.models.credential import CredentialType
from api.models.proxy import Proxy

if TYPE_CHECKING:
    from api.models.connector import Connector
    from api.models.credential import Credential


class SyncableProvider(Protocol):
    """Protocol for providers that support syncing and IP refresh.

    Providers implementing this protocol can be used with ProxyProviderSyncer.
    """

    def is_session_based(self) -> bool:
        """Check if this provider uses session-based proxies."""
        ...

    async def sync_proxies(
        self, existing_proxies: list[Proxy]
    ) -> tuple[list[Proxy], list[str]]:
        """Sync proxies to match configuration.

        Returns:
            Tuple of (proxies_to_add, proxy_ids_to_remove)
        """
        ...

    async def refresh_ips(self, proxies: list[Proxy]) -> list[Proxy]:
        """Refresh IPs for port-based proxies.

        Returns:
            List of proxies with updated IP metadata
        """
        ...


# Type alias for provider factory function
ProviderFactory = type["SyncableProvider"]

# Registry mapping credential types to provider classes
_PROVIDER_REGISTRY: dict[CredentialType, ProviderFactory] = {}


def register_provider(credential_type: CredentialType, provider_class: ProviderFactory) -> None:
    """Register a provider class for a credential type.

    Args:
        credential_type: The credential type this provider handles
        provider_class: The provider class to instantiate
    """
    _PROVIDER_REGISTRY[credential_type] = provider_class


def get_provider(
    credential_type: CredentialType,
    connector: Connector,
    credential: Credential,
) -> SyncableProvider | None:
    """Get a provider instance for the given credential type.

    Args:
        credential_type: The type of credential
        connector: The connector configuration
        credential: The credential for authentication

    Returns:
        A provider instance, or None if no provider is registered for this type
    """
    provider_class = _PROVIDER_REGISTRY.get(credential_type)
    if provider_class is None:
        return None
    return provider_class(connector, credential)  # type: ignore[call-arg]


def is_syncable_credential_type(credential_type: CredentialType) -> bool:
    """Check if a credential type has a registered syncable provider.

    Args:
        credential_type: The credential type to check

    Returns:
        True if a provider is registered for this type
    """
    return credential_type in _PROVIDER_REGISTRY


# Register built-in providers
def _register_builtin_providers() -> None:
    """Register the built-in proxy providers."""
    from api.providers.brightdata import BrightDataProvider
    from api.providers.oxylabs import OxylabsProvider

    register_provider(CredentialType.BRIGHTDATA, BrightDataProvider)
    register_provider(CredentialType.OXYLABS, OxylabsProvider)


# Auto-register on module import
_register_builtin_providers()

