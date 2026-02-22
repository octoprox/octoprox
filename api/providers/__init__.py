# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Proxy providers for different source types."""

from api.providers.base import ProxyProvider
from api.providers.brightdata import BrightDataProvider
from api.providers.oxylabs import OxylabsProvider
from api.providers.static import StaticProvider

__all__ = [
    "ProxyProvider",
    "StaticProvider",
    "OxylabsProvider",
    "BrightDataProvider",
]

