# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""BrightData-specific API endpoints."""

import structlog
from fastapi import APIRouter, HTTPException, Request

from api.models.cloud_options import BrightDataZone
from api.models.connector import BrightDataProxyType
from api.models.credential import CredentialType
from api.services.brightdata_api import (
    fetch_active_zones,
    fetch_route_ips,
    fetch_zone_password,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/brightdata", tags=["brightdata"])


async def fetch_zones(token: str) -> list[BrightDataZone]:
    """Fetch available zones from BrightData API.

    Args:
        token: BrightData API token

    Returns:
        List of BrightDataZone objects
    """
    zones_data = await fetch_active_zones(token)
    if not zones_data:
        return []

    zones = []

    # Filter and map zones
    for zone in zones_data:
        zone_type = zone.get("type", "")
        zone_name = zone.get("name", "")
        proxy_type = None

        # Map zone type to proxy type (only supported types)
        # res_static zones have static IPs like ISP, so map them to ISP
        if zone_type == "res_static":
            proxy_type = BrightDataProxyType.ISP.value
        elif zone_type.startswith("res"):
            proxy_type = BrightDataProxyType.RESIDENTIAL.value
        elif zone_type.startswith("mobile"):
            proxy_type = BrightDataProxyType.MOBILE.value
        elif zone_type.startswith("isp"):
            proxy_type = BrightDataProxyType.ISP.value
        elif zone_type.startswith("dc"):
            proxy_type = BrightDataProxyType.DATACENTER.value

        # Only include supported types
        if proxy_type and zone_name:
            # Fetch zone password
            password = await fetch_zone_password(token, zone_name)

            if password:
                # For ISP/DC zones, fetch route IPs to enrich with IP counts
                country_counts = None
                total_ips = None
                if proxy_type in (BrightDataProxyType.ISP.value, BrightDataProxyType.DATACENTER.value):
                    route_ip_data = await fetch_route_ips(token, zone_name)
                    if route_ip_data:
                        counts: dict[str, int] = {}
                        for entry in route_ip_data:
                            cc = entry.get("country", "").lower()
                            if cc:
                                counts[cc] = counts.get(cc, 0) + 1
                        country_counts = counts
                        total_ips = len(route_ip_data)

                zones.append(BrightDataZone(
                    name=zone_name,
                    type=zone_type,
                    proxy_type=proxy_type,
                    password=password,
                    country_counts=country_counts,
                    total_ips=total_ips,
                ))
            else:
                logger.warning("Skipping zone without password", zone_name=zone_name)

    logger.info("Fetched BrightData zones", count=len(zones))
    return zones


@router.get("/zones/{credential_id}")
async def get_brightdata_zones(request: Request, credential_id: str) -> list[BrightDataZone]:
    """Get available zones for a BrightData credential.

    Args:
        request: FastAPI request object
        credential_id: Credential ID

    Returns:
        List of BrightDataZone objects

    Raises:
        HTTPException: If credential not found or invalid
    """
    proxy_manager = request.app.state.proxy_manager
    credential = proxy_manager.get_credential(credential_id)

    if not credential or credential.type != CredentialType.BRIGHTDATA:
        raise HTTPException(status_code=404, detail="BrightData credential not found")

    token = credential.config.get("token")
    if not token:
        raise HTTPException(status_code=400, detail="Credential has no token")

    zones = await fetch_zones(token)
    return zones
