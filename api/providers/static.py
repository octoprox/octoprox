"""Static proxy provider for file-based or manual proxy lists."""

from api.models.connector import Connector
from api.models.credential import Credential
from api.models.proxy import Proxy, ProxyProtocol
from api.providers.base import ProxyProvider


class StaticProvider(ProxyProvider):
    """Provider for static proxy lists defined in configuration."""

    def __init__(self, connector: Connector, credential: Credential | None = None) -> None:
        super().__init__(connector, credential)

    def get_proxies(self) -> list[Proxy]:
        """Get proxies from static configuration."""
        proxies: list[Proxy] = []

        proxy_list = self.connector.config.get("proxies", [])

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
                connector_id=self.connector.id,
                tags=proxy_data.get("tags", []),
            )
            proxies.append(proxy)

        return proxies
