# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Shared BrightData API client.

Centralizes all HTTP calls to the BrightData API (api.brightdata.com).
Used by both the routes layer (zone listing, credential validation) and
the provider layer (IP discovery, sync, refresh).
"""

import httpx
import structlog
from pydantic import BaseModel

logger = structlog.get_logger()

BRIGHTDATA_API_BASE = "https://api.brightdata.com"
BRIGHTDATA_API_TIMEOUT = 30.0


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
                f"{BRIGHTDATA_API_BASE}/status",
                headers={"Authorization": f"Bearer {token}"},
                timeout=BRIGHTDATA_API_TIMEOUT,
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
                f"{BRIGHTDATA_API_BASE}/zone",
                params={"zone": zone_name},
                headers={"Authorization": f"Bearer {token}"},
                timeout=BRIGHTDATA_API_TIMEOUT,
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


async def fetch_route_ips(
    token: str, zone_name: str, country: str | None = None
) -> list[dict[str, str]]:
    """Fetch route IPs for a BrightData zone (ISP/DC zones only).

    Args:
        token: BrightData API token
        zone_name: Zone name
        country: Optional 2-letter country code to filter IPs server-side

    Returns:
        List of dicts with 'ip' and 'country' keys, or empty list on failure.
    """
    try:
        params: dict[str, str] = {"zone": zone_name, "list_countries": "true"}
        if country:
            params["country"] = country.lower()

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BRIGHTDATA_API_BASE}/zone/route_ips",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=BRIGHTDATA_API_TIMEOUT,
            )

            if response.status_code != 200:
                logger.warning(
                    "Failed to fetch route IPs",
                    zone_name=zone_name,
                    status_code=response.status_code,
                )
                return []

            data = response.json()
            if not isinstance(data, list):
                logger.warning("Unexpected route_ips response format", zone_name=zone_name)
                return []

            logger.debug("Fetched route IPs", zone_name=zone_name, count=len(data))
            return data
    except httpx.HTTPError as e:
        logger.error("BrightData API error fetching route IPs", zone_name=zone_name, error=str(e))
        return []
    except Exception as e:
        logger.error("Unexpected error fetching route IPs", zone_name=zone_name, error=str(e))
        return []


async def fetch_active_zones(token: str) -> list[dict[str, str]]:
    """Fetch active zones from BrightData API.

    Args:
        token: BrightData API token

    Returns:
        List of zone dicts from the API, or empty list on failure.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BRIGHTDATA_API_BASE}/zone/get_active_zones",
                headers={"Authorization": f"Bearer {token}"},
                timeout=BRIGHTDATA_API_TIMEOUT,
            )

            if response.status_code != 200:
                logger.warning("Failed to fetch zones", status_code=response.status_code)
                return []

            data: list[dict[str, str]] = response.json()
            return data
    except httpx.HTTPError as e:
        logger.error("BrightData API error fetching zones", error=str(e))
        return []
    except Exception as e:
        logger.error("Unexpected error fetching zones", error=str(e))
        return []
