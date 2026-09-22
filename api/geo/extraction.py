# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Where in a health check response the exit IP is, when the check hits the echo endpoint."""

from __future__ import annotations

from typing import Any

from api.core.health_checker import default_ip_paths
from api.geo.service import GeoService


class EchoExtractionRules:
    """Implements :class:`api.core.health_checker.IpExtractionRules` on top of the attribution settings.

    Order: a connector's own ``healthcheck_ip_path``, then the echo endpoint's
    paths when the check URL is the echo URL and health check attribution is
    on, then the httpbin default. Any other URL reports no IP.
    """

    def __init__(self, geo_service: GeoService) -> None:
        self._geo_service = geo_service

    @property
    def default_check_url(self) -> str:
        """Health checks with no connector URL go to the echo endpoint, so every check reports the exit IP."""
        return self._geo_service.settings.echo_url

    def ip_paths(self, url: str, connector_config: dict[str, Any]) -> tuple[str, str | None] | None:
        configured = default_ip_paths(url, connector_config, include_default_url=False)
        if configured is not None:
            return configured
        if self._geo_service.settings.health_check_attribution and self._geo_service.is_echo_url(url):
            spec = self._geo_service.echo_spec()
            return spec.ip_path, spec.country_path
        return default_ip_paths(url, connector_config)
