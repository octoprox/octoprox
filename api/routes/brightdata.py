# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""BrightData-specific API endpoints."""

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api.models.cloud_options import BrightDataZone
from api.models.connector import BrightDataProxyType
from api.models.credential import CredentialType

logger = structlog.get_logger()

router = APIRouter(prefix="/brightdata", tags=["brightdata"])


class TokenValidationResponse(BaseModel):
    """Response for token validation."""
    valid: bool
    customer_id: str | None = None
    status: str | None = None


async def validate_token(token: str) -> TokenValidationResponse:
    """Validate BrightData token and get customer ID.

    Args:
        token: BrightData API token

    Returns:
        TokenValidationResponse with validation result
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.brightdata.com/status",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.warning("BrightData token validation failed", status_code=response.status_code)
                return TokenValidationResponse(valid=False)

            data = response.json()
            status = data.get("status")

            # Only accept active status
            if status != "active":
                logger.warning("BrightData account not active", status=status)
                return TokenValidationResponse(valid=False, status=status)

            customer_id = data.get("customer")
            logger.info("BrightData token validated successfully", customer_id=customer_id)
            return TokenValidationResponse(
                valid=True,
                customer_id=customer_id,
                status=status,
            )
    except httpx.HTTPError as e:
        logger.error("BrightData API error during token validation", error=str(e))
        return TokenValidationResponse(valid=False)
    except Exception as e:
        logger.error("Unexpected error during token validation", error=str(e))
        return TokenValidationResponse(valid=False)


async def fetch_zone_password(token: str, zone_name: str) -> str | None:
    """Fetch password for a specific zone.

    Args:
        token: BrightData API token
        zone_name: Zone name

    Returns:
        Zone password or None if not found
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://api.brightdata.com/zone?zone={zone_name}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.warning("Failed to fetch zone password", zone_name=zone_name, status_code=response.status_code)
                return None

            data = response.json()
            passwords = data.get("password", [])

            # Return first password if available
            password = passwords[0] if passwords else None
            if password:
                logger.debug("Fetched zone password", zone_name=zone_name)
            else:
                logger.warning("No password found for zone", zone_name=zone_name)
            return password
    except httpx.HTTPError as e:
        logger.error("BrightData API error fetching zone password", zone_name=zone_name, error=str(e))
        return None
    except Exception as e:
        logger.error("Unexpected error fetching zone password", zone_name=zone_name, error=str(e))
        return None


async def fetch_zones(token: str) -> list[BrightDataZone]:
    """Fetch available zones from BrightData API.

    Args:
        token: BrightData API token

    Returns:
        List of BrightDataZone objects
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.brightdata.com/zone/get_active_zones",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30.0,
            )

            if response.status_code != 200:
                logger.warning("Failed to fetch zones", status_code=response.status_code)
                return []

            zones_data = response.json()
            zones = []

            # Filter and map zones
            for zone in zones_data:
                zone_type = zone.get("type", "")
                zone_name = zone.get("name", "")
                proxy_type = None

                # Map zone type to proxy type (only supported types)
                if zone_type.startswith("res"):
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
                        zones.append(BrightDataZone(
                            name=zone_name,
                            type=zone_type,
                            proxy_type=proxy_type,
                            password=password,
                        ))
                    else:
                        logger.warning("Skipping zone without password", zone_name=zone_name)

            logger.info("Fetched BrightData zones", count=len(zones))
            return zones
    except httpx.HTTPError as e:
        logger.error("BrightData API error fetching zones", error=str(e))
        return []
    except Exception as e:
        logger.error("Unexpected error fetching zones", error=str(e))
        return []


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

