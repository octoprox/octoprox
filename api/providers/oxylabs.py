# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Oxylabs proxy provider for residential, mobile, ISP, and datacenter proxies."""

import secrets
import string

import httpx
import structlog

from api.models.connector import Connector, OxylabsConnectorConfig
from api.models.credential import Credential, OxylabsProxyType
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.providers.base import ProxyProvider

logger = structlog.get_logger()

# Oxylabs endpoint configuration
OXYLABS_ENDPOINTS: dict[OxylabsProxyType, tuple[str, int]] = {
    OxylabsProxyType.RESIDENTIAL: ("pr.oxylabs.io", 7777),
    OxylabsProxyType.MOBILE: ("pr.oxylabs.io", 7777),
    OxylabsProxyType.ISP: ("isp.oxylabs.io", 8001),
    OxylabsProxyType.DEDICATED_ISP: ("disp.oxylabs.io", 8001),
    OxylabsProxyType.DATACENTER: ("dc.oxylabs.io", 8001),
    OxylabsProxyType.DATACENTER_DEDICATED: ("ddc.oxylabs.io", 8001),
}

# Session-based proxy types use random session IDs
SESSION_BASED_TYPES = {OxylabsProxyType.RESIDENTIAL, OxylabsProxyType.MOBILE}

# Port-based proxy types use sequential ports starting from 8001
PORT_BASED_TYPES = {
    OxylabsProxyType.ISP,
    OxylabsProxyType.DEDICATED_ISP,
    OxylabsProxyType.DATACENTER,
    OxylabsProxyType.DATACENTER_DEDICATED,
}

# IP discovery endpoint
IP_DISCOVERY_URL = "https://ip.oxylabs.io/location"


def _generate_session_id() -> str:
    """Generate a random session ID for session-based proxies."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12))


class OxylabsProvider(ProxyProvider):
    """Provider for Oxylabs proxy services.

    Supports two modes:
    - Session-based (residential, mobile): Uses random session IDs in username
    - Port-based (isp, dedicated_isp, datacenter, datacenter_dedicated): Uses sequential ports

    Connector config options:
        - num_proxies: Number of proxy slots to create
        - country_code: 2-letter country code (optional, session-based only)
        - session_duration_minutes: Session duration in minutes (1-30, default 10)

    Credential config:
        - proxy_type: Type of Oxylabs proxy
        - username: Oxylabs username
        - password: Oxylabs password
    """

    def __init__(self, connector: Connector, credential: Credential) -> None:
        super().__init__(connector, credential)

        if not credential:
            raise ValueError("OxylabsProvider requires credentials")

        # Parse credential config
        cred_config = credential.config
        self._proxy_type = OxylabsProxyType(cred_config["proxy_type"])
        self._username = cred_config["username"]
        self._password = cred_config["password"]

        # Parse connector config
        oxylabs_config = connector.oxylabs_config
        if not oxylabs_config:
            raise ValueError("OxylabsProvider requires Oxylabs connector configuration")

        self._config: OxylabsConnectorConfig = oxylabs_config
        self._endpoint, self._base_port = OXYLABS_ENDPOINTS[self._proxy_type]

    def is_session_based(self) -> bool:
        """Check if this provider uses session-based proxies."""
        return self._proxy_type in SESSION_BASED_TYPES

    def _build_username(self, session_id: str | None = None) -> str:
        """Build the Oxylabs username with optional session and country parameters.

        For session-based types (residential, mobile):
            customer-USER-cc-XX-sessid-YYY or customer-USER-sessid-YYY (no country)

        For port-based types (isp, dedicated_isp, datacenter, datacenter_dedicated):
            user-USER
        """
        if self.is_session_based():
            prefix = "customer"
            parts = [f"{prefix}-{self._username}"]

            # Add country code if specified
            if self._config.country_code:
                parts.append(f"cc-{self._config.country_code}")

            # Add session ID
            if session_id:
                parts.append(f"sessid-{session_id}")

            return "-".join(parts)
        else:
            # Port-based types use simple username
            return f"user-{self._username}"

    def get_proxies(self) -> list[Proxy]:
        """Get initial proxy list based on configuration.

        For session-based types: Creates proxies with random session IDs
        For port-based types: Creates proxies for sequential ports (8001, 8002, ...)
        """
        proxies: list[Proxy] = []
        num_proxies = self._config.num_proxies

        if self.is_session_based():
            # Session-based: generate random session IDs
            for i in range(num_proxies):
                session_id = _generate_session_id()
                proxy = self._create_session_proxy(session_id, i)
                proxies.append(proxy)
        else:
            # Port-based: use sequential ports
            for i in range(num_proxies):
                port = self._base_port + i
                proxy = self._create_port_proxy(port, i)
                proxies.append(proxy)

        return proxies

    def _create_session_proxy(self, session_id: str, index: int) -> Proxy:
        """Create a session-based proxy entry."""
        username = self._build_username(session_id)

        return Proxy(
            # Use default UUID for id (36 chars max in DB)
            host=self._endpoint,
            port=self._base_port,
            protocol=ProxyProtocol.HTTP,
            username=username,
            password=self._password,
            connector_id=self.connector.id,
            status=ProxyStatus.HEALTHY,  # Oxylabs proxies are immediately available
            tags=["oxylabs", self._proxy_type.value],
            metadata={
                "session_id": session_id,
                "proxy_type": self._proxy_type.value,
                "country_code": self._config.country_code,
                "session_duration_minutes": self._config.session_duration_minutes,
            },
        )

    def _create_port_proxy(self, port: int, index: int) -> Proxy:
        """Create a port-based proxy entry.

        For port-based proxies:
        - host: The Oxylabs endpoint (used for routing/health checks)
        - display_host: The discovered IP (shown in UI), initially None
        """
        username = self._build_username()

        return Proxy(
            # Use default UUID for id (36 chars max in DB)
            host=self._endpoint,
            port=port,
            protocol=ProxyProtocol.HTTP,
            username=username,
            password=self._password,
            connector_id=self.connector.id,
            status=ProxyStatus.INITIALIZING,  # Will be updated after IP discovery
            tags=["oxylabs", self._proxy_type.value],
            metadata={
                "proxy_type": self._proxy_type.value,
                "port": port,
            },
        )

    async def discover_ip(self, proxy: Proxy) -> str | None:
        """Discover the real IP address for a port-based proxy.

        Makes a request through the proxy to the Oxylabs IP discovery endpoint.
        This IP is for display purposes only - routing still uses the Oxylabs endpoint
        stored in proxy.host.

        Args:
            proxy: The proxy to discover the IP for

        Returns:
            The discovered IP address, or None if discovery failed
        """
        if self.is_session_based():
            # Session-based proxies don't have fixed IPs
            return None

        try:
            async with httpx.AsyncClient(
                proxy=proxy.url,
                timeout=30.0,
            ) as client:
                response = await client.get(IP_DISCOVERY_URL)
                if response.status_code == 200:
                    data = response.json()
                    ip: str | None = data.get("ip")
                    logger.debug(
                        "Discovered IP for Oxylabs proxy",
                        proxy_id=proxy.id,
                        port=proxy.port,
                        ip=ip,
                    )
                    return ip
                else:
                    logger.warning(
                        "IP discovery failed",
                        proxy_id=proxy.id,
                        status_code=response.status_code,
                    )
                    return None
        except httpx.TimeoutException:
            logger.warning("IP discovery timed out", proxy_id=proxy.id)
            return None
        except httpx.ProxyError as e:
            logger.warning("IP discovery proxy error", proxy_id=proxy.id, error=str(e))
            return None
        except Exception as e:
            logger.error("IP discovery error", proxy_id=proxy.id, error=str(e))
            return None

    async def sync_proxies(
        self, existing_proxies: list[Proxy]
    ) -> tuple[list[Proxy], list[str]]:
        """Synchronize proxies to match the configured num_proxies.

        Ensures the proxy count matches the connector configuration.
        For session-based types: Regenerates missing session IDs
        For port-based types: Discovers IPs for new ports, stops on first failure

        Args:
            existing_proxies: Current list of proxies for this connector

        Returns:
            Tuple of (proxies_to_add, proxy_ids_to_remove)
        """
        num_proxies = self._config.num_proxies
        current_count = len(existing_proxies)

        proxies_to_add: list[Proxy] = []
        proxy_ids_to_remove: list[str] = []

        if self.is_session_based():
            # Session-based: ensure we have exactly num_proxies
            if current_count < num_proxies:
                # Add more proxies
                for i in range(current_count, num_proxies):
                    session_id = _generate_session_id()
                    proxy = self._create_session_proxy(session_id, i)
                    proxies_to_add.append(proxy)
            elif current_count > num_proxies:
                # Remove excess proxies (from the end)
                for proxy in existing_proxies[num_proxies:]:
                    proxy_ids_to_remove.append(proxy.id)
        else:
            # Port-based: ensure we have proxies for ports 8001 to 8001+num_proxies-1
            existing_ports = {p.port for p in existing_proxies}
            target_ports = set(range(self._base_port, self._base_port + num_proxies))

            # Find ports to add
            ports_to_add = sorted(target_ports - existing_ports)

            # Find proxies to remove (ports beyond target range)
            for proxy in existing_proxies:
                if proxy.port not in target_ports:
                    proxy_ids_to_remove.append(proxy.id)

            # Add new proxies for missing ports, stop on first failure
            for port in ports_to_add:
                index = port - self._base_port
                proxy = self._create_port_proxy(port, index)

                # Try to discover IP
                ip = await self.discover_ip(proxy)
                if ip:
                    # Store discovered IP in display_host for UI display
                    # host remains the Oxylabs endpoint for routing
                    proxy.display_host = ip
                    proxy.metadata["discovered_ip"] = ip
                    proxy.status = ProxyStatus.HEALTHY
                    proxies_to_add.append(proxy)
                else:
                    # Port failed - stop trying higher ports
                    logger.warning(
                        "Port-based proxy failed, stopping discovery",
                        connector_id=self.connector.id,
                        port=port,
                    )
                    break

        return proxies_to_add, proxy_ids_to_remove

    async def refresh_ips(self, proxies: list[Proxy]) -> list[Proxy]:
        """Refresh discovered IPs for port-based proxies.

        Called periodically (e.g., every 24h) to update IP metadata.
        Only applicable to port-based proxy types.

        Args:
            proxies: List of proxies to refresh

        Returns:
            List of proxies with updated IP metadata
        """
        if self.is_session_based():
            return proxies

        updated_proxies: list[Proxy] = []
        for proxy in proxies:
            ip = await self.discover_ip(proxy)
            if ip:
                old_ip = proxy.metadata.get("discovered_ip")
                if ip != old_ip:
                    logger.info(
                        "Proxy IP changed",
                        proxy_id=proxy.id,
                        old_ip=old_ip,
                        new_ip=ip,
                    )
                    # Update display_host for UI display
                    # host remains the Oxylabs endpoint for routing
                    proxy.display_host = ip
                proxy.metadata["discovered_ip"] = ip
            updated_proxies.append(proxy)

        return updated_proxies
