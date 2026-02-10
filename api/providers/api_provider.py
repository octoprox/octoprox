"""API-based proxy provider for external proxy services."""

import httpx
import structlog

from api.models.proxy import Proxy, ProxyProtocol
from api.models.source import ProxySource
from api.providers.base import ProxyProvider

logger = structlog.get_logger()


class APIProvider(ProxyProvider):
    """Provider for API-based proxy services."""
    
    def __init__(self, source: ProxySource) -> None:
        super().__init__(source)
        self._url = source.config.get("url", "")
        self._api_key = source.config.get("api_key", "")
        self._headers = source.config.get("headers", {})
    
    async def fetch_proxies(self) -> list[Proxy]:
        """Fetch proxies from external API."""
        proxies: list[Proxy] = []
        
        if not self._url:
            logger.warning("API provider has no URL configured", source_id=self.source.id)
            return proxies
        
        headers = {**self._headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(self._url, headers=headers, timeout=30)
                response.raise_for_status()
                
                data = response.json()
                
                # Handle common API response formats
                proxy_list = self._extract_proxy_list(data)
                
                for proxy_data in proxy_list:
                    proxy = self._parse_proxy(proxy_data)
                    if proxy:
                        proxies.append(proxy)
                        
        except httpx.HTTPError as e:
            logger.error("Failed to fetch proxies from API", error=str(e), source_id=self.source.id)
        except Exception as e:
            logger.error("Unexpected error fetching proxies", error=str(e), source_id=self.source.id)
        
        return proxies
    
    def _extract_proxy_list(self, data: dict | list) -> list:
        """Extract proxy list from API response."""
        if isinstance(data, list):
            return data
        
        # Common response formats
        for key in ["proxies", "data", "results", "items"]:
            if key in data and isinstance(data[key], list):
                return data[key]
        
        return []
    
    def _parse_proxy(self, proxy_data: dict | str) -> Proxy | None:
        """Parse proxy data into a Proxy object."""
        try:
            if isinstance(proxy_data, str):
                # Parse "host:port" or "protocol://host:port" format
                return self._parse_proxy_string(proxy_data)
            
            if isinstance(proxy_data, dict):
                protocol_str = proxy_data.get("protocol", "http")
                try:
                    protocol = ProxyProtocol(protocol_str)
                except ValueError:
                    protocol = ProxyProtocol.HTTP
                
                return Proxy(
                    host=proxy_data.get("host", proxy_data.get("ip", "")),
                    port=int(proxy_data.get("port", 8080)),
                    protocol=protocol,
                    username=proxy_data.get("username"),
                    password=proxy_data.get("password"),
                    source_id=self.source.id,
                )
        except Exception as e:
            logger.warning("Failed to parse proxy data", error=str(e), data=str(proxy_data))
        
        return None
    
    def _parse_proxy_string(self, proxy_str: str) -> Proxy | None:
        """Parse a proxy string like 'host:port' or 'http://host:port'."""
        protocol = ProxyProtocol.HTTP
        
        if "://" in proxy_str:
            proto, rest = proxy_str.split("://", 1)
            try:
                protocol = ProxyProtocol(proto)
            except ValueError:
                pass
            proxy_str = rest
        
        if ":" in proxy_str:
            host, port_str = proxy_str.rsplit(":", 1)
            return Proxy(host=host, port=int(port_str), protocol=protocol, source_id=self.source.id)
        
        return None
    
    async def validate(self) -> bool:
        """Validate API configuration."""
        return bool(self._url)

