# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Where in a health check response the exit IP is.

The worker's health job asks :class:`EchoExtractionRules`, which finds it
in the answer of a connector's own check URL, the echo endpoint or httpbin;
a check URL that echoes nothing reports no exit, and the proxy's location is
left alone.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from api.geo.service import GeoService

# The URL checked when neither the connector nor the attribution settings
# name one: it answers the caller's IP under ``origin``.
DEFAULT_HEALTHCHECK_URL = "https://httpbin.org/ip"
# Connector config keys: where in the health check response the exit IP (and
# its place) can be read. Set them when a connector uses a custom check URL
# that echoes the caller's IP; leave them unset for URLs that do not.
HEALTHCHECK_IP_PATH_KEY = "healthcheck_ip_path"
HEALTHCHECK_COUNTRY_PATH_KEY = "healthcheck_country_path"
HEALTHCHECK_STATE_PATH_KEY = "healthcheck_state_path"
HEALTHCHECK_CITY_PATH_KEY = "healthcheck_city_path"


class ExtractionPaths(NamedTuple):
    """JMESPaths into a check response: the caller's IP, and its place where the response has it."""

    ip: str
    country: str | None = None
    state: str | None = None
    city: str | None = None


def _clean_path(config: dict[str, Any], key: str) -> str | None:
    value = config.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def default_ip_paths(
    url: str, connector_config: dict[str, Any] | None, *, include_default_url: bool = True
) -> ExtractionPaths | None:
    """Paths from the connector's config, else the httpbin default, else None."""
    config = connector_config or {}
    custom = _clean_path(config, HEALTHCHECK_IP_PATH_KEY)
    if custom:
        return ExtractionPaths(
            custom,
            _clean_path(config, HEALTHCHECK_COUNTRY_PATH_KEY),
            _clean_path(config, HEALTHCHECK_STATE_PATH_KEY),
            _clean_path(config, HEALTHCHECK_CITY_PATH_KEY),
        )
    if include_default_url and url.rstrip("/") == DEFAULT_HEALTHCHECK_URL:
        return ExtractionPaths("origin")
    return None


class EchoExtractionRules:
    """The health job's extraction rules, on top of the attribution settings.

    Order: a connector's own ``healthcheck_ip_path``, then the echo endpoint's
    paths when the check URL is the echo URL and health check attribution is
    on, then the httpbin default; any other URL reports no IP.
    """

    def __init__(self, geo_service: GeoService) -> None:
        self._geo_service = geo_service

    @property
    def default_check_url(self) -> str:
        """Health checks with no connector URL go to the echo endpoint, so every check reports the exit IP."""
        return self._geo_service.settings.echo_url

    def ip_paths(self, url: str, connector_config: dict[str, Any]) -> ExtractionPaths | None:
        configured = default_ip_paths(url, connector_config, include_default_url=False)
        if configured is not None:
            return configured
        if self._geo_service.settings.health_check_attribution and self._geo_service.is_echo_url(url):
            spec = self._geo_service.echo_spec()
            return ExtractionPaths(spec.ip_path, spec.country_path, spec.state_path, spec.city_path)
        return default_ip_paths(url, connector_config)
