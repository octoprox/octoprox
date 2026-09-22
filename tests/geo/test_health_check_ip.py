# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the health checker reporting the exit IP it saw."""

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from api.core.config import Settings
from api.core.health_checker import DEFAULT_HEALTHCHECK_URL, HealthChecker
from api.geo.extraction import EchoExtractionRules
from api.geo.observations import ObservationRecorder
from api.geo.service import GeoService
from api.geo.settings import GeoSettingsStore
from api.geo.store import GeoDatabaseStore
from api.models.connector import Connector
from api.models.credential import CredentialType
from api.models.proxy import Proxy


def _connector(config: dict) -> Connector:
    return Connector(
        id="conn", name="c", credential_id="cred", credential_type=CredentialType.STATIC_PROXY_PROVIDER,
        project_id="p", config=config,
    )


def _checker(config: dict, geo_service: GeoService | None) -> HealthChecker:
    provider = MagicMock()
    provider.get_connector = MagicMock(return_value=_connector(config))
    rules = EchoExtractionRules(geo_service) if geo_service is not None else None
    return HealthChecker(provider, MagicMock(), "inst", extraction_rules=rules)


@pytest.fixture
def geo_service(tmp_path: Path) -> GeoService:
    settings = Settings(instance_id="t", geo_lookup_url="https://echo.example/ip", geo_lookup_ip_path="ip", geo_lookup_country_path="country")  # type: ignore[call-arg]
    return GeoService(settings, GeoDatabaseStore(None, tmp_path), GeoSettingsStore(settings, None), ObservationRecorder(None))


def _proxy() -> Proxy:
    return Proxy(host="h", port=1, connector_id="conn")


class TestIpPaths:
    def test_default_httpbin_url(self, geo_service: GeoService) -> None:
        assert _checker({}, geo_service)._ip_paths(_proxy(), DEFAULT_HEALTHCHECK_URL) == ("origin", None)

    def test_echo_url_uses_policy_paths(self, geo_service: GeoService) -> None:
        assert _checker({"healthcheck_url": "https://echo.example/ip"}, geo_service)._ip_paths(_proxy(), "https://echo.example/ip") == ("ip", "country")

    def test_custom_url_without_path_reports_nothing(self, geo_service: GeoService) -> None:
        assert _checker({"healthcheck_url": "https://www.bing.com"}, geo_service)._ip_paths(_proxy(), "https://www.bing.com") is None

    def test_connector_paths_win(self, geo_service: GeoService) -> None:
        checker = _checker({"healthcheck_url": "https://x/ip", "healthcheck_ip_path": "data.addr", "healthcheck_country_path": "data.cc"}, geo_service)
        assert checker._ip_paths(_proxy(), "https://x/ip") == ("data.addr", "data.cc")

    def test_policy_can_disable_echo_attribution(self, geo_service: GeoService) -> None:
        geo_service.settings_store._settings = geo_service.settings.model_copy(update={"health_check_attribution": False})
        assert _checker({}, geo_service)._ip_paths(_proxy(), "https://echo.example/ip") is None

    def test_without_geo_only_the_default_is_known(self) -> None:
        assert _checker({}, None)._ip_paths(_proxy(), DEFAULT_HEALTHCHECK_URL) == ("origin", None)
        assert _checker({}, None)._ip_paths(_proxy(), "https://echo.example/ip") is None


class TestCheckUrl:
    def test_echo_is_the_default_when_rules_are_present(self, geo_service: GeoService) -> None:
        assert _checker({}, geo_service)._get_healthcheck_url(_proxy()) == "https://echo.example/ip"
        assert _checker({"healthcheck_url": "https://www.bing.com"}, geo_service)._get_healthcheck_url(_proxy()) == "https://www.bing.com"

    def test_httpbin_without_rules(self) -> None:
        assert _checker({}, None)._get_healthcheck_url(_proxy()) == DEFAULT_HEALTHCHECK_URL


class TestObservedIp:
    def test_extracts_first_origin(self, geo_service: GeoService) -> None:
        response = httpx.Response(200, json={"origin": "203.0.113.5, 10.0.0.1"})
        assert _checker({}, geo_service)._observed_ip(_proxy(), DEFAULT_HEALTHCHECK_URL, response) == ("203.0.113.5", None)

    def test_extracts_ip_and_country(self, geo_service: GeoService) -> None:
        response = httpx.Response(200, json={"ip": "203.0.113.5", "country": "de"})
        assert _checker({}, geo_service)._observed_ip(_proxy(), "https://echo.example/ip", response) == ("203.0.113.5", "DE")

    def test_non_json_body(self, geo_service: GeoService) -> None:
        response = httpx.Response(200, text="<html>ok</html>")
        assert _checker({}, geo_service)._observed_ip(_proxy(), DEFAULT_HEALTHCHECK_URL, response) == (None, None)

    def test_text_path(self, geo_service: GeoService) -> None:
        response = httpx.Response(200, text="203.0.113.5\n")
        checker = _checker({"healthcheck_url": "https://x/raw", "healthcheck_ip_path": "@text"}, geo_service)
        assert checker._observed_ip(_proxy(), "https://x/raw", response) == ("203.0.113.5", None)
