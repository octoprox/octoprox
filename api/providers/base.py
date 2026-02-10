"""Base class for proxy providers."""

from abc import ABC, abstractmethod

from api.models.proxy import Proxy
from api.models.source import ProxySource


class ProxyProvider(ABC):
    """Abstract base class for proxy providers."""
    
    def __init__(self, source: ProxySource) -> None:
        self.source = source
    
    @abstractmethod
    async def fetch_proxies(self) -> list[Proxy]:
        """Fetch proxies from the source.
        
        Returns:
            List of proxies from this source.
        """
        ...
    
    @abstractmethod
    async def validate(self) -> bool:
        """Validate the source configuration.
        
        Returns:
            True if configuration is valid.
        """
        ...

