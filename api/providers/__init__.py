"""Proxy providers for different source types."""

from api.providers.base import ProxyProvider
from api.providers.static import StaticProvider

__all__ = [
    "ProxyProvider",
    "StaticProvider",
]

