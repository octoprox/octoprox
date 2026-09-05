# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Provider SDK: declarative proxy-provider descriptors and the engine that runs them.

A :class:`~api.providers.sdk.descriptor.ProviderDescriptor` describes a proxy
vendor without code: which fields a credential and a connector need, how to
turn those into proxy endpoints (gateway sessions, port-mapped IPs, or an
API-served list), and which vendor API calls discover options or validate a
credential. :class:`~api.providers.sdk.provider.DescriptorProvider` executes a
descriptor and plugs into the same ``SyncableProvider`` contract the provider
syncer already uses.
"""

from api.providers.sdk.descriptor import ProviderDescriptor
from api.providers.sdk.provider import DescriptorProvider

__all__ = ["ProviderDescriptor", "DescriptorProvider"]
