# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy providers.

Cloud (AWS/GCP/Azure) and static providers are implemented in code. Every
other vendor is a declarative descriptor executed by the provider SDK; see
:mod:`api.providers.sdk` and the built-ins in ``api/providers/builtin``.
"""

from api.providers.base import ProxyProvider, SyncableProvider
from api.providers.registry import ProviderRegistry, get_provider_registry
from api.providers.sdk.descriptor import ProviderDescriptor
from api.providers.sdk.provider import DescriptorProvider
from api.providers.static import StaticProvider

__all__ = [
    "ProxyProvider",
    "SyncableProvider",
    "StaticProvider",
    "ProviderDescriptor",
    "DescriptorProvider",
    "ProviderRegistry",
    "get_provider_registry",
]
