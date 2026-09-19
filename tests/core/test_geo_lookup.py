# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the static proxy exit-location lookup."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from api.core.geo_lookup import GeoLookup
from api.models.proxy import Proxy, ProxyProtocol


def _settings(**overrides: object) -> MagicMock:
    settings = MagicMock()
    settings.geo_lookup_enabled = True
    settings.geo_lookup_url = "https://geo.example.test/myip.json"
    settings.geo_lookup_ip_path = "ip"
    settings.geo_lookup_country_path = "country"
    settings.geo_lookup_timeout_seconds = 5.0
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _factory(payload: object, status: int = 200, seen: list[str] | None = None):
    def client_factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
        if seen is not None:
            seen.append(proxy_url)
        transport = httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
        return httpx.AsyncClient(transport=transport, timeout=timeout)
    return client_factory


def _proxy(**kwargs: object) -> Proxy:
    kwargs.setdefault("connector_id", "c")
    return Proxy(host="203.0.113.10", port=8080, protocol=ProxyProtocol.HTTP, username="u", password="p", **kwargs)  # type: ignore[arg-type]


class TestGeoLookup:
    async def test_locate_requests_through_the_proxy(self) -> None:
        seen: list[str] = []
        lookup = GeoLookup(_settings(), client_factory=_factory({"ip": "198.51.100.7", "country": "de"}, seen=seen))
        ip, country = await lookup.locate(_proxy())
        assert (ip, country) == ("198.51.100.7", "DE")
        assert seen == ["http://u:p@203.0.113.10:8080"]

    async def test_locate_without_country_path(self) -> None:
        lookup = GeoLookup(_settings(geo_lookup_country_path=""), client_factory=_factory({"ip": "198.51.100.7", "country": "DE"}))
        assert await lookup.locate(_proxy()) == ("198.51.100.7", "")

    async def test_locate_failure(self) -> None:
        lookup = GeoLookup(_settings(), client_factory=_factory({}, status=500))
        assert await lookup.locate(_proxy()) == (None, "")

    async def test_enrich_records_ip_and_country(self) -> None:
        proxy = _proxy()
        manager = MagicMock()
        manager.get_proxy.return_value = proxy
        manager.resolve_proxy_credentials.side_effect = lambda p: p
        manager.update_proxy = AsyncMock()
        lookup = GeoLookup(_settings(), client_factory=_factory({"ip": "198.51.100.7", "country": "GB"}))

        updated = await lookup.enrich(manager, proxy.id)

        assert updated is proxy
        assert proxy.display_host == "198.51.100.7"
        assert proxy.metadata["discovered_ip"] == "198.51.100.7"
        assert proxy.country == "GB"
        manager.update_proxy.assert_awaited_once_with(proxy)

    async def test_enrich_keeps_manual_country_when_endpoint_has_none(self) -> None:
        proxy = _proxy(metadata={"country": "FR"})
        manager = MagicMock()
        manager.get_proxy.return_value = proxy
        manager.resolve_proxy_credentials.side_effect = lambda p: p
        manager.update_proxy = AsyncMock()
        lookup = GeoLookup(_settings(), client_factory=_factory({"ip": "198.51.100.7"}))
        await lookup.enrich(manager, proxy.id)
        assert proxy.country == "FR"
        assert proxy.display_host == "198.51.100.7"

    async def test_enrich_failure_changes_nothing(self) -> None:
        proxy = _proxy()
        manager = MagicMock()
        manager.get_proxy.return_value = proxy
        manager.resolve_proxy_credentials.side_effect = lambda p: p
        manager.update_proxy = AsyncMock()
        lookup = GeoLookup(_settings(), client_factory=_factory({}, status=502))
        assert await lookup.enrich(manager, proxy.id) is None
        assert proxy.display_host is None
        manager.update_proxy.assert_not_awaited()

    async def test_enrich_missing_proxy(self) -> None:
        manager = MagicMock()
        manager.get_proxy.return_value = None
        lookup = GeoLookup(_settings(), client_factory=_factory({"ip": "1.1.1.1"}))
        assert await lookup.enrich(manager, "gone") is None

    async def test_proxy_added_triggers_lookup_for_static_proxies_only(self) -> None:
        from api.core.signals import proxy_added
        from api.models.connector import Connector
        from api.models.credential import CredentialType

        static = Connector(id="static", name="s", credential_id="c", credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="p")
        provider = Connector(id="oxy", name="o", credential_id="c", credential_type="oxylabs", project_id="p")
        proxies = {
            "plain": _proxy(id="plain", connector_id="static"),
            "manual": _proxy(id="manual", connector_id="static", metadata={"country": "GB"}),
            "vendor": _proxy(id="vendor", connector_id="oxy"),
        }
        manager = MagicMock()
        manager.get_connector.side_effect = lambda cid: {"static": static, "oxy": provider}.get(cid)
        manager.get_proxy.side_effect = lambda pid: proxies.get(pid)
        manager.resolve_proxy_credentials.side_effect = lambda p: p
        manager.update_proxy = AsyncMock()

        lookup = GeoLookup(_settings(), client_factory=_factory({"ip": "198.51.100.7", "country": "US"}))
        await lookup.start(manager)
        try:
            for pid, proxy in proxies.items():
                await proxy_added.send_async(manager, proxy_id=pid, connector_id=proxy.connector_id)
            await asyncio.gather(*lookup._tasks)
        finally:
            await lookup.stop()

        assert proxies["plain"].country == "US" and proxies["plain"].display_host == "198.51.100.7"
        assert proxies["manual"].country == "GB" and proxies["manual"].display_host is None
        assert proxies["vendor"].country is None
        manager.update_proxy.assert_awaited_once_with(proxies["plain"])

    async def test_disabled_service_ignores_proxy_added(self) -> None:
        from api.core.signals import proxy_added

        manager = MagicMock()
        lookup = GeoLookup(_settings(geo_lookup_enabled=False), client_factory=_factory({"ip": "1.1.1.1"}))
        await lookup.start(manager)
        try:
            await proxy_added.send_async(manager, proxy_id="x", connector_id="c")
            assert lookup._tasks == set()
        finally:
            await lookup.stop()
        manager.get_proxy.assert_not_called()

    def test_disabled_flag(self) -> None:
        assert GeoLookup(_settings(geo_lookup_enabled=False)).enabled is False
        with pytest.raises(ValueError):
            GeoLookup(_settings(geo_lookup_url="not-a-url"))
