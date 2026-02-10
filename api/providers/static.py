"""Static proxy provider for file-based or manual proxy lists."""

from api.models.proxy import Proxy, ProxyProtocol
from api.models.source import ProxySource
from api.providers.base import ProxyProvider


class StaticProvider(ProxyProvider):
    """Provider for static proxy lists defined in configuration."""
    
    def __init__(self, source: ProxySource) -> None:
        super().__init__(source)
    
    async def fetch_proxies(self) -> list[Proxy]:
        """Fetch proxies from static configuration."""
        proxies: list[Proxy] = []
        
        proxy_list = self.source.config.get("proxies", [])
        
        for proxy_data in proxy_list:
            protocol_str = proxy_data.get("protocol", "http")
            try:
                protocol = ProxyProtocol(protocol_str)
            except ValueError:
                protocol = ProxyProtocol.HTTP
            
            proxy = Proxy(
                host=proxy_data["host"],
                port=proxy_data["port"],
                protocol=protocol,
                username=proxy_data.get("username"),
                password=proxy_data.get("password"),
                source_id=self.source.id,
                tags=proxy_data.get("tags", []),
            )
            proxies.append(proxy)
        
        return proxies
    
    async def validate(self) -> bool:
        """Validate static configuration."""
        proxy_list = self.source.config.get("proxies", [])
        
        if not isinstance(proxy_list, list):
            return False
        
        for proxy_data in proxy_list:
            if not isinstance(proxy_data, dict):
                return False
            if "host" not in proxy_data or "port" not in proxy_data:
                return False
        
        return True

