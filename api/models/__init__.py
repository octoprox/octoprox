"""Data models for Octoprox."""

from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.models.source import ProxySource, SourceType

__all__ = [
    "Proxy",
    "ProxyProtocol",
    "ProxyStatus",
    "ProxySource",
    "SourceType",
]

