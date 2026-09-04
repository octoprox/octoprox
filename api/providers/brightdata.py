# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""BrightData proxy provider for residential, mobile, ISP, and datacenter proxies."""

import secrets
import string

import httpx
import structlog

from api.models.connector import BrightDataConnectorConfig, BrightDataProxyType, Connector
from api.models.credential import Credential
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.providers.base import ProxyProvider, _sort_proxies_healthy_first
from api.services import brightdata_api

logger = structlog.get_logger()

# BrightData endpoint configuration
BRIGHTDATA_HOST = "brd.superproxy.io"
BRIGHTDATA_PORT = 44445

# IP discovery endpoint
IP_DISCOVERY_URL = "https://lumtest.com/myip.json"

# Session-based proxy types use global session IDs
SESSION_BASED_TYPES = {BrightDataProxyType.RESIDENTIAL, BrightDataProxyType.MOBILE}

# Port-based proxy types use IP discovery
PORT_BASED_TYPES = {BrightDataProxyType.ISP, BrightDataProxyType.DATACENTER}


def _generate_session_id() -> str:
    """Generate global session ID: glob_<random-12-chars>."""
    alphabet = string.ascii_lowercase + string.digits
    random_part = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"glob_{random_part}"


class BrightDataProvider(ProxyProvider):
    """Provider for BrightData proxy services.

    Supports two modes:
    - Session-based (residential, mobile): Uses global session IDs
    - Port-based (isp, datacenter): Uses IP discovery with x-brd-ip header

    Connector config:
        - zone_name: BrightData zone name
        - zone_password: Zone password
        - proxy_type: Type of proxy (derived from zone)
        - num_proxies: Number of proxy slots
        - country_code: 2-letter country code (optional, all types)

    Credential config:
        - token: BrightData API token
        - customer_id: BrightData customer ID
    """

    def __init__(self, connector: Connector, credential: Credential) -> None:
        super().__init__(connector, credential)

        if not credential:
            raise ValueError("BrightDataProvider requires credentials")

        # Parse credential config
        cred_config = credential.config
        self._token = cred_config["token"]
        self._customer_id = cred_config["customer_id"]

        # Parse connector config
        brightdata_config = connector.brightdata_config
        if not brightdata_config:
            raise ValueError("BrightDataProvider requires BrightData connector configuration")

        self._config: BrightDataConnectorConfig = brightdata_config
        self._zone_name = self._config.zone_name
        self._zone_password = self._config.zone_password
        self._proxy_type = self._config.proxy_type

    def is_session_based(self) -> bool:
        """Check if this provider uses session-based proxies."""
        return self._proxy_type in SESSION_BASED_TYPES

    def _build_username(self, session_id: str | None = None, hashed_ip: str | None = None) -> str:
        """Build BrightData username with placeholder for customer_id.

        Uses {customer_id} placeholder that will be resolved at request time
        by the proxy manager.

        Format: brd-customer-{customer_id}-zone-<zone_name>[-session-<session_id>][-ip-<hashed_ip>][-country-<code>]
        """
        parts = [
            "brd-customer-{customer_id}",
            f"zone-{self._zone_name}",
        ]

        if session_id:
            parts.append(f"session-{session_id}")

        if hashed_ip:
            parts.append(f"ip-{hashed_ip}")

        if self._config.country_code:
            parts.append(f"country-{self._config.country_code.lower()}")

        return "-".join(parts)

    def _create_session_proxy(self, index: int) -> Proxy:
        """Create a session-based proxy with global session ID and placeholder credentials."""
        session_id = _generate_session_id()  # glob_<random-12-chars>
        username = self._build_username(session_id=session_id)

        return Proxy(
            host=BRIGHTDATA_HOST,
            port=BRIGHTDATA_PORT,
            protocol=ProxyProtocol.HTTP,
            username=username,
            password="{zone_password}",  # Placeholder resolved at request time
            connector_id=self.connector.id,
            status=ProxyStatus.HEALTHY,
            tags=["brightdata", self._proxy_type.value],
            metadata={
                "session_id": session_id,
                "proxy_type": self._proxy_type.value,
                "zone_name": self._zone_name,
                "country_code": self._config.country_code,
            },
        )

    def _create_port_proxy(self, index: int) -> Proxy:
        """Create a port-based proxy with placeholder credentials (needs IP discovery)."""
        username = self._build_username()

        return Proxy(
            host=BRIGHTDATA_HOST,
            port=BRIGHTDATA_PORT,
            protocol=ProxyProtocol.HTTP,
            username=username,
            password="{zone_password}",  # Placeholder resolved at request time
            connector_id=self.connector.id,
            status=ProxyStatus.INITIALIZING,
            tags=["brightdata", self._proxy_type.value],
            metadata={
                "proxy_type": self._proxy_type.value,
                "zone_name": self._zone_name,
                "country_code": self._config.country_code,
                "index": index,
            },
        )

    def _build_proxy_url_for_discovery(self, proxy: Proxy) -> str:
        """Build a proxy URL with resolved credentials for IP discovery.

        Since proxy credentials contain placeholders, we need to resolve
        them with actual credentials for making discovery requests.
        """
        # Start with proxy.url and resolve placeholders
        url = proxy.url
        url = url.replace("{customer_id}", self._customer_id)
        url = url.replace("{zone_password}", self._zone_password)
        return url

    async def discover_ip(self, proxy: Proxy) -> tuple[str | None, str | None]:
        """Discover IP for port-based proxy.

        Returns:
            Tuple of (display_ip, hashed_ip) where both are the discovered IP.
            The x-brd-ip header is in the proxy CONNECT response which httpx
            doesn't expose, so we use the IP from the JSON body for both.
        """
        if self.is_session_based():
            return None, None

        try:
            # Build URL with resolved credentials for discovery
            proxy_url = self._build_proxy_url_for_discovery(proxy)
            async with httpx.AsyncClient(proxy=proxy_url, timeout=30.0) as client:
                response = await client.get(IP_DISCOVERY_URL)
                if response.status_code == 200:
                    # Get IP from JSON response body
                    data = response.json()
                    ip = data.get("ip")

                    if ip:
                        logger.debug(
                            "Discovered IP for BrightData proxy",
                            proxy_id=proxy.id,
                            ip=ip,
                        )
                        # Use the same IP for both display and routing
                        return ip, ip

                    logger.warning(
                        "No IP in response",
                        proxy_id=proxy.id,
                    )
                    return None, None
                else:
                    logger.warning(
                        "IP discovery failed",
                        proxy_id=proxy.id,
                        status_code=response.status_code,
                    )
                    return None, None
        except httpx.TimeoutException:
            logger.warning("IP discovery timed out", proxy_id=proxy.id)
            return None, None
        except httpx.ProxyError as e:
            logger.warning("IP discovery proxy error", proxy_id=proxy.id, error=str(e))
            return None, None
        except Exception as e:
            logger.error("IP discovery error", proxy_id=proxy.id, error=str(e))
            return None, None

    async def _fetch_route_ips(self, country: str | None = None) -> list[tuple[str, str]]:
        """Fetch route IPs from BrightData API for this zone.

        Args:
            country: Optional 2-letter country code to filter IPs server-side.

        Returns:
            List of (ip, country) tuples, or empty list on failure.
        """
        data = await brightdata_api.fetch_route_ips(
            self._token, self._zone_name, country=country
        )
        if not data:
            return []

        result = [(entry["ip"], entry.get("country", "")) for entry in data if "ip" in entry]
        logger.info("Fetched route IPs", zone_name=self._zone_name, count=len(result))
        return result

    def _assign_ip_to_proxy(self, proxy: Proxy, ip: str, country: str) -> None:
        """Assign a known IP to a port-based proxy."""
        proxy.display_host = ip
        proxy.metadata["discovered_ip"] = ip
        proxy.metadata["hashed_ip"] = ip
        proxy.metadata["country"] = country
        proxy.username = self._build_username(hashed_ip=ip)
        proxy.status = ProxyStatus.HEALTHY

    async def _sync_port_proxies_via_api(
        self, slots_needed: int, start_index: int, existing_ips: set[str]
    ) -> list[Proxy]:
        """Sync port-based proxies using the route_ips API.

        Returns:
            List of new proxies to add, or empty list if API call failed.
        """
        route_ips = await self._fetch_route_ips(country=self._config.country_code)
        if not route_ips:
            return []

        # Exclude IPs already assigned to existing proxies
        available_ips = [(ip, country) for ip, country in route_ips if ip not in existing_ips]

        ips_to_assign = available_ips[:slots_needed]

        if len(ips_to_assign) < slots_needed:
            logger.warning(
                "Not enough IPs available",
                connector_id=self.connector.id,
                needed=slots_needed,
                available=len(ips_to_assign),
            )

        proxies_to_add: list[Proxy] = []
        for i, (ip, country) in enumerate(ips_to_assign):
            proxy = self._create_port_proxy(start_index + i)
            self._assign_ip_to_proxy(proxy, ip, country)
            existing_ips.add(ip)
            proxies_to_add.append(proxy)

        return proxies_to_add

    async def _sync_port_proxies_via_discovery(
        self, slots_needed: int, start_index: int, existing_ips: set[str]
    ) -> list[Proxy]:
        """Sync port-based proxies using per-proxy IP discovery (fallback)."""
        proxies_to_add: list[Proxy] = []
        consecutive_failed_slots = 0
        max_retries_per_slot = 3
        max_consecutive_failed_slots = 3

        for i in range(slots_needed):
            proxy = self._create_port_proxy(start_index + i)
            added = False

            for attempt in range(max_retries_per_slot):
                display_ip, hashed_ip = await self.discover_ip(proxy)
                if display_ip and hashed_ip:
                    if display_ip not in existing_ips:
                        existing_ips.add(display_ip)
                        self._assign_ip_to_proxy(proxy, display_ip, "")
                        proxies_to_add.append(proxy)
                        added = True
                        break
                    logger.warning(
                        "Duplicate IP discovered, retrying",
                        connector_id=self.connector.id,
                        index=start_index + i,
                        ip=display_ip,
                        attempt=attempt + 1,
                    )
                else:
                    logger.warning(
                        "Failed to discover IP for new proxy",
                        index=start_index + i,
                    )
                    break

            if added:
                consecutive_failed_slots = 0
            else:
                consecutive_failed_slots += 1
                if consecutive_failed_slots >= max_consecutive_failed_slots:
                    logger.warning(
                        "Too many consecutive failed slots, stopping discovery",
                        connector_id=self.connector.id,
                    )
                    break

        return proxies_to_add

    async def sync_proxies(self, existing_proxies: list[Proxy]) -> tuple[list[Proxy], list[str]]:
        """Sync proxies to match num_proxies config."""
        num_proxies = self._config.num_proxies
        current_count = len(existing_proxies)

        proxies_to_add: list[Proxy] = []
        proxy_ids_to_remove: list[str] = []

        if current_count < num_proxies:
            slots_needed = num_proxies - current_count

            if self.is_session_based():
                for i in range(current_count, num_proxies):
                    proxy = self._create_session_proxy(i)
                    proxies_to_add.append(proxy)
            else:
                # Collect existing discovered IPs for dedup
                existing_ips: set[str] = {
                    p.metadata["discovered_ip"]
                    for p in existing_proxies
                    if p.metadata.get("discovered_ip")
                }

                # Primary: use route_ips API
                proxies_to_add = await self._sync_port_proxies_via_api(
                    slots_needed, current_count, existing_ips
                )

                # Fallback: if API returned nothing, use per-proxy discovery
                if not proxies_to_add:
                    logger.warning(
                        "Route IPs API unavailable, falling back to per-proxy discovery",
                        connector_id=self.connector.id,
                    )
                    proxies_to_add = await self._sync_port_proxies_via_discovery(
                        slots_needed, current_count, existing_ips
                    )

        elif current_count > num_proxies:
            # Remove excess proxies, prioritising unhealthy ones first
            sorted_proxies = _sort_proxies_healthy_first(existing_proxies)
            proxies_to_remove = sorted_proxies[num_proxies:]
            proxy_ids_to_remove = [p.id for p in proxies_to_remove]

        return proxies_to_add, proxy_ids_to_remove

    async def _refresh_ips_via_api(
        self, proxies: list[Proxy]
    ) -> tuple[list[Proxy], list[str]] | None:
        """Refresh IPs using route_ips API. Returns None if API call failed."""
        route_ips = await self._fetch_route_ips()
        if not route_ips:
            return None

        current_api_ips = {ip for ip, _ in route_ips}
        ip_to_country = dict(route_ips)

        updated_proxies: list[Proxy] = []
        proxy_ids_to_remove: list[str] = []
        seen_ips: set[str] = set()

        for proxy in proxies:
            old_ip = proxy.metadata.get("discovered_ip")

            if old_ip and old_ip in current_api_ips:
                # IP still valid
                if old_ip in seen_ips:
                    logger.warning(
                        "Duplicate IP on refresh, removing proxy",
                        proxy_id=proxy.id,
                        ip=old_ip,
                    )
                    proxy_ids_to_remove.append(proxy.id)
                    continue

                seen_ips.add(old_ip)
                # Update country metadata if available
                new_country = ip_to_country.get(old_ip, "")
                if new_country and proxy.metadata.get("country") != new_country:
                    proxy.metadata["country"] = new_country
                updated_proxies.append(proxy)
            else:
                # IP no longer in the zone's pool
                logger.info(
                    "Proxy IP no longer in route_ips, removing",
                    proxy_id=proxy.id,
                    old_ip=old_ip,
                )
                proxy_ids_to_remove.append(proxy.id)

        return updated_proxies, proxy_ids_to_remove

    async def _refresh_ips_via_discovery(
        self, proxies: list[Proxy]
    ) -> tuple[list[Proxy], list[str]]:
        """Refresh IPs using per-proxy discovery (fallback)."""
        updated_proxies: list[Proxy] = []
        proxy_ids_to_remove: list[str] = []
        seen_ips: set[str] = set()

        for proxy in proxies:
            display_ip, hashed_ip = await self.discover_ip(proxy)
            if display_ip and hashed_ip:
                if display_ip in seen_ips:
                    logger.warning(
                        "Duplicate IP on refresh, removing proxy",
                        proxy_id=proxy.id,
                        ip=display_ip,
                    )
                    proxy_ids_to_remove.append(proxy.id)
                    continue

                seen_ips.add(display_ip)
                old_ip = proxy.metadata.get("discovered_ip")
                old_hashed = proxy.metadata.get("hashed_ip")

                if display_ip != old_ip or hashed_ip != old_hashed:
                    logger.info(
                        "Proxy IP changed",
                        proxy_id=proxy.id,
                        old_ip=old_ip,
                        new_ip=display_ip,
                    )
                    proxy.display_host = display_ip
                    proxy.metadata["discovered_ip"] = display_ip
                    proxy.metadata["hashed_ip"] = hashed_ip
                    proxy.username = self._build_username(hashed_ip=hashed_ip)

                updated_proxies.append(proxy)

        return updated_proxies, proxy_ids_to_remove

    async def refresh_ips(
        self, proxies: list[Proxy]
    ) -> tuple[list[Proxy], list[str]]:
        """Refresh IPs for port-based proxies (called by syncer every 24h).

        Uses the route_ips API as the primary method, falling back to
        per-proxy discovery if the API call fails.

        Returns:
            Tuple of (updated_proxies, proxy_ids_to_remove)
        """
        if self.is_session_based():
            return [], []

        # Primary: use route_ips API
        result = await self._refresh_ips_via_api(proxies)
        if result is not None:
            return result

        # Fallback: per-proxy discovery
        logger.warning(
            "Route IPs API unavailable for refresh, falling back to per-proxy discovery",
            connector_id=self.connector.id,
        )
        return await self._refresh_ips_via_discovery(proxies)

