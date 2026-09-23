# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ProxyAttributor: sightings in, attributed proxies out."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from api.core.config import Settings
from api.core.signals import exit_ip_changed, exit_ip_observed, exit_location_mismatch, proxy_added
from api.geo.attributor import ProxyAttributor
from api.geo.models import (
    META_LOCATION_CONFLICT,
    META_VENDOR_COUNTRY,
    ExitJudgement,
    GeoSourceKind,
    ObservationSource,
    SourcePolicy,
)
from api.geo.observations import ObservationRecorder
from api.geo.service import GeoService
from api.geo.settings import GeoSettingsStore
from api.geo.store import GeoDatabaseStore
from api.models.connector import Connector
from api.models.credential import CredentialType
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol
from api.providers.sdk.sources import IpDiscoverer
from api.providers.sdk.strategies import (
    META_DISCOVERED_IP,
    META_DYNAMIC_SESSIONS,
    META_GEO,
    META_SESSION_ID,
)
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb

GB_IP = "81.2.69.160"
ECHO_IP = "198.51.100.7"  # not in the database; the echo endpoint's country decides


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "geo_lookup_enabled": True,
        "geo_lookup_url": "https://geo.example.test/myip.json",
        "geo_lookup_ip_path": "ip",
        "geo_lookup_country_path": "country",
        "instance_id": "test-instance",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def use_echo(
    geo_service: GeoService, payload: object, status: int = 200, seen: list[str] | None = None
) -> None:
    """Make every echo request the service issues answer ``payload`` without a network."""

    def client_factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
        if seen is not None:
            seen.append(proxy_url)
        transport = httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    geo_service.discoverer = lambda: IpDiscoverer(
        geo_service.echo_spec(), client_factory=client_factory
    )  # type: ignore[method-assign]


class FakeStore:
    """Just enough of the proxy pool for the attributor."""

    def __init__(self) -> None:
        self.projects: dict[str, Project] = {
            "p": Project(id="p", name="P", username="u", password="pw"),
            "trusting": Project(
                id="trusting",
                name="T",
                username="t",
                password="pw",
                location_sources=[GeoSourceKind.VENDOR, GeoSourceKind.DATABASE],
            ),
        }
        self.connectors: dict[str, Connector] = {
            "static": Connector(
                id="static",
                name="s",
                credential_id="c",
                credential_type=CredentialType.STATIC_PROXY_PROVIDER,
                project_id="p",
            ),
            "oxy": Connector(
                id="oxy", name="o", credential_id="c", credential_type="oxylabs", project_id="p"
            ),
            "trusting-c": Connector(
                id="trusting-c",
                name="tc",
                credential_id="c",
                credential_type="oxylabs",
                project_id="trusting",
            ),
        }
        self._proxies: dict[str, Proxy] = {}
        self.update_proxy = AsyncMock()
        self.update_proxies = AsyncMock()
        self.remove_proxy = AsyncMock(return_value=True)

    @property
    def proxies(self) -> list[Proxy]:
        return list(self._proxies.values())

    def add(self, proxy: Proxy) -> Proxy:
        self._proxies[proxy.id] = proxy
        return proxy

    def get_proxy(self, proxy_id: str) -> Proxy | None:
        return self._proxies.get(proxy_id)

    def get_connector(self, connector_id: str) -> Connector | None:
        return self.connectors.get(connector_id)

    def get_project(self, project_id: str) -> Project | None:
        return self.projects.get(project_id)

    def resolve_proxy_credentials(self, proxy: Proxy) -> Proxy:
        return proxy


def _proxy(**kwargs: object) -> Proxy:
    kwargs.setdefault("connector_id", "static")
    return Proxy(
        host="203.0.113.10",
        port=8080,
        protocol=ProxyProtocol.HTTP,
        username="u",
        password="p",
        **kwargs,
    )  # type: ignore[arg-type]


@pytest.fixture
async def geo_service(tmp_path: Path) -> GeoService:
    db = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
    database_store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(db)}])
    await database_store.sync_all()
    settings = _settings()
    return GeoService(
        settings, database_store, GeoSettingsStore(settings, None), ObservationRecorder(None)
    )


async def _attributor(
    geo_service: GeoService,
    store: FakeStore,
    payload: object = None,
    status: int = 200,
    **overrides: object,
):
    use_echo(
        geo_service, payload if payload is not None else {"ip": ECHO_IP, "country": "de"}, status
    )
    attributor = ProxyAttributor(geo_service, _settings(**overrides))
    await attributor.start(store)
    return attributor


class TestLookup:
    async def test_locate_requests_through_the_proxy(self, geo_service: GeoService) -> None:
        seen: list[str] = []
        use_echo(geo_service, {"ip": ECHO_IP, "country": "de"}, seen=seen)
        attributor = ProxyAttributor(geo_service, _settings())
        assert await attributor.locate(_proxy()) == (ECHO_IP, "DE")
        assert seen == ["http://u:p@203.0.113.10:8080"]

    async def test_locate_failure(self, geo_service: GeoService) -> None:
        use_echo(geo_service, {}, status=500)
        attributor = ProxyAttributor(geo_service, _settings())
        assert await attributor.locate(_proxy()) == (None, "")

    async def test_enrich_records_ip_and_country(self, geo_service: GeoService) -> None:
        store = FakeStore()
        proxy = store.add(_proxy())
        attributor = await _attributor(geo_service, store)
        try:
            assert await attributor.enrich(proxy.id) is proxy
        finally:
            await attributor.stop()
        assert proxy.display_host == ECHO_IP and proxy.metadata[META_DISCOVERED_IP] == ECHO_IP
        assert proxy.country == "DE"  # nothing in the database; the endpoint decides
        store.update_proxy.assert_awaited_once_with(proxy)

    async def test_enrich_prefers_database_over_endpoint(self, geo_service: GeoService) -> None:
        store = FakeStore()
        proxy = store.add(_proxy())
        attributor = await _attributor(geo_service, store, payload={"ip": GB_IP, "country": "de"})
        try:
            await attributor.enrich(proxy.id)
        finally:
            await attributor.stop()
        assert proxy.country == "GB" and proxy.metadata["location"]["city"] == "London"

    async def test_enrich_keeps_manual_country_when_endpoint_has_none(
        self, geo_service: GeoService
    ) -> None:
        store = FakeStore()
        proxy = store.add(_proxy(metadata={"country": "FR", "country_source": "manual"}))
        attributor = await _attributor(geo_service, store, payload={"ip": ECHO_IP})
        try:
            await attributor.enrich(proxy.id)
        finally:
            await attributor.stop()
        assert proxy.country == "FR" and proxy.display_host == ECHO_IP

    async def test_enrich_failure_changes_nothing(self, geo_service: GeoService) -> None:
        store = FakeStore()
        proxy = store.add(_proxy())
        attributor = await _attributor(geo_service, store, payload={}, status=502)
        try:
            assert await attributor.enrich(proxy.id) is None
        finally:
            await attributor.stop()
        assert proxy.display_host is None
        store.update_proxy.assert_not_awaited()

    async def test_enrich_missing_proxy(self, geo_service: GeoService) -> None:
        attributor = await _attributor(geo_service, FakeStore())
        try:
            assert await attributor.enrich("gone") is None
        finally:
            await attributor.stop()

    async def test_proxy_added_triggers_lookup_for_static_proxies_only(
        self, geo_service: GeoService
    ) -> None:
        store = FakeStore()
        proxies = {
            "plain": store.add(_proxy(id="plain", connector_id="static")),
            "manual": store.add(
                _proxy(id="manual", connector_id="static", metadata={"country": "GB"})
            ),
            "vendor": store.add(_proxy(id="vendor", connector_id="oxy")),
        }
        attributor = await _attributor(geo_service, store, payload={"ip": ECHO_IP, "country": "US"})
        try:
            for pid, proxy in proxies.items():
                await proxy_added.send_async(store, proxy_id=pid, connector_id=proxy.connector_id)
            await asyncio.gather(*attributor._tasks)
        finally:
            await attributor.stop()
        assert proxies["plain"].country == "US" and proxies["plain"].display_host == ECHO_IP
        assert proxies["manual"].country == "GB" and proxies["manual"].display_host is None
        assert proxies["vendor"].country is None
        store.update_proxy.assert_awaited_once_with(proxies["plain"])

    async def test_disabled_lookup_ignores_proxy_added(self, geo_service: GeoService) -> None:
        store = FakeStore()
        proxy = store.add(_proxy())
        attributor = await _attributor(geo_service, store, geo_lookup_enabled=False)
        try:
            await proxy_added.send_async(store, proxy_id=proxy.id, connector_id="static")
            assert attributor.in_flight == 0 and not attributor.wants_lookup(proxy)
        finally:
            await attributor.stop()
        store.update_proxy.assert_not_awaited()


class TestObservations:
    async def test_exit_ip_observed_signal_attributes_and_persists(
        self, geo_service: GeoService
    ) -> None:
        store = FakeStore()
        proxy = store.add(_proxy(connector_id="oxy", metadata={META_GEO: "US"}))
        attributor = await _attributor(geo_service, store)
        try:
            await exit_ip_observed.send_async(
                None, proxy_id=proxy.id, ip=GB_IP, source="discovery", endpoint_country="GB"
            )
        finally:
            await attributor.stop()
        assert proxy.country == "GB" and proxy.metadata[META_LOCATION_CONFLICT] is True
        assert proxy.metadata[META_VENDOR_COUNTRY] == "US"
        store.update_proxy.assert_awaited_once_with(proxy)
        # The observation carries the owning project, so project-scoped views can filter on it.
        observation = geo_service.observation_recorder._buffer[-1]
        assert observation.project_id == "p" and observation.connector_id == "oxy"
        assert (
            attributor.project_id_for("trusting-c") == "trusting"
            and attributor.project_id_for("nope") is None
        )

    async def test_exit_moving_announces_the_change(self, geo_service: GeoService) -> None:
        """Preflight trusts a verdict for the session TTL; an IP change must cut that short."""
        store = FakeStore()
        proxy = store.add(_proxy(connector_id="oxy"))
        attributor = await _attributor(geo_service, store)
        changes: list[dict[str, object]] = []

        async def on_change(sender: object, **kwargs: object) -> None:
            changes.append(kwargs)

        exit_ip_changed.connect(on_change)
        try:
            await attributor.observe(proxy.id, GB_IP, source=ObservationSource.DISCOVERY)
            assert changes == []  # first sighting: nothing to invalidate
            await attributor.observe(proxy.id, GB_IP, source=ObservationSource.HEALTH_CHECK)
            assert changes == []  # same exit
            await attributor.observe(proxy.id, ECHO_IP, source=ObservationSource.HEALTH_CHECK)
            assert len(changes) == 1
            assert changes[0]["proxy_id"] == proxy.id and changes[0]["project_id"] == "p"
            assert changes[0]["old_ip"] == GB_IP and changes[0]["new_ip"] == ECHO_IP
        finally:
            exit_ip_changed.disconnect(on_change)
            await attributor.stop()

    async def test_health_check_sightings_only_write_on_change(
        self, geo_service: GeoService
    ) -> None:
        store = FakeStore()
        proxy = store.add(_proxy(connector_id="oxy"))
        attributor = await _attributor(geo_service, store)
        try:
            await attributor.observe(proxy.id, GB_IP, source=ObservationSource.HEALTH_CHECK)
            await attributor.observe(proxy.id, GB_IP, source=ObservationSource.HEALTH_CHECK)
            assert store.update_proxy.await_count == 1
            await attributor.observe(
                proxy.id, ECHO_IP, source=ObservationSource.HEALTH_CHECK, endpoint_country="DE"
            )
            assert store.update_proxy.await_count == 2 and proxy.country == "DE"
        finally:
            await attributor.stop()

    async def test_dynamic_gateway_is_recorded_but_never_written(self, geo_service: GeoService) -> None:
        store = FakeStore()
        gateway = store.add(_proxy(connector_id="oxy", metadata={META_DYNAMIC_SESSIONS: "true", META_SESSION_ID: "probe"}))
        attributor = await _attributor(geo_service, store)
        changes: list[dict[str, object]] = []

        async def on_change(sender: object, **kwargs: object) -> None:
            changes.append(kwargs)

        exit_ip_changed.connect(on_change)
        try:
            before = geo_service.observation_recorder.pending
            await attributor.observe(gateway.id, GB_IP, source=ObservationSource.MANUAL)
            await attributor.observe(gateway.id, ECHO_IP, source=ObservationSource.MANUAL, endpoint_country="DE")
            assert geo_service.observation_recorder.pending == before + 2
            sighting = geo_service.observation_recorder._buffer[-1]
            assert sighting.connector_id == "oxy" and sighting.project_id == "p" and sighting.session_id == "probe"
            assert sighting.resolved_country == "DE"
        finally:
            exit_ip_changed.disconnect(on_change)
            await attributor.stop()
        assert gateway.country is None and META_DISCOVERED_IP not in gateway.metadata
        assert META_LOCATION_CONFLICT not in gateway.metadata
        store.update_proxy.assert_not_awaited()
        assert changes == []

    async def test_garbage_ip_and_unknown_proxy_are_ignored(self, geo_service: GeoService) -> None:
        store = FakeStore()
        attributor = await _attributor(geo_service, store)
        try:
            assert (
                await attributor.observe("missing", GB_IP, source=ObservationSource.DISCOVERY)
                is None
            )
            store.add(_proxy(id="x"))
            assert (
                await attributor.observe("x", "not-an-ip", source=ObservationSource.DISCOVERY)
                is None
            )
        finally:
            await attributor.stop()
        store.update_proxy.assert_not_awaited()

    async def test_project_policy_decides(self, geo_service: GeoService) -> None:
        store = FakeStore()
        trusting = store.add(_proxy(id="t", connector_id="trusting-c", metadata={META_GEO: "US"}))
        default = store.add(_proxy(id="d", connector_id="oxy", metadata={META_GEO: "US"}))
        attributor = await _attributor(geo_service, store)
        try:
            assert attributor.policy_for("trusting-c") == SourcePolicy(
                sources=[GeoSourceKind.VENDOR, GeoSourceKind.DATABASE]
            )
            assert attributor.policy_for("oxy") == geo_service.default_policy
            assert attributor.policy_for("unknown") == geo_service.default_policy
            await attributor.observe(trusting.id, GB_IP, source=ObservationSource.DISCOVERY)
            await attributor.observe(default.id, GB_IP, source=ObservationSource.DISCOVERY)
        finally:
            await attributor.stop()
        assert trusting.country == "US" and trusting.metadata[META_LOCATION_CONFLICT] is True
        assert default.country == "GB"


class TestMismatch:
    async def test_fixed_exit_is_flagged(self, geo_service: GeoService) -> None:
        store = FakeStore()
        proxy = store.add(_proxy(connector_id="oxy"))
        attributor = await _attributor(geo_service, store)
        try:
            await exit_location_mismatch.send_async(
                None, proxy_id=proxy.id, project_id="p", expected="GB", observed="US", ip=ECHO_IP
            )
        finally:
            await attributor.stop()
        assert proxy.metadata[META_LOCATION_CONFLICT] is True and proxy.display_host == ECHO_IP
        store.update_proxy.assert_awaited_once_with(proxy)
        store.remove_proxy.assert_not_awaited()

    async def test_vendor_session_is_rotated(self, geo_service: GeoService) -> None:
        store = FakeStore()
        proxy = store.add(_proxy(connector_id="oxy", metadata={META_SESSION_ID: "abc"}))
        attributor = await _attributor(geo_service, store)
        try:
            await exit_location_mismatch.send_async(
                None, proxy_id=proxy.id, project_id="p", expected="GB", observed="US", ip=ECHO_IP
            )
        finally:
            await attributor.stop()
        store.remove_proxy.assert_awaited_once_with(proxy.id)
        store.update_proxy.assert_not_awaited()

    async def test_dynamic_gateway_is_left_alone(self, geo_service: GeoService) -> None:
        store = FakeStore()
        gateway = store.add(_proxy(connector_id="oxy", metadata={META_DYNAMIC_SESSIONS: "true", META_SESSION_ID: "probe"}))
        attributor = await _attributor(geo_service, store)
        try:
            await exit_location_mismatch.send_async(
                None, proxy_id=gateway.id, project_id="p", expected="GB", observed="US", ip=ECHO_IP
            )
        finally:
            await attributor.stop()
        store.remove_proxy.assert_not_awaited()
        store.update_proxy.assert_not_awaited()
        assert META_LOCATION_CONFLICT not in gateway.metadata

    async def test_unknown_proxy_is_ignored(self, geo_service: GeoService) -> None:
        store = FakeStore()
        attributor = await _attributor(geo_service, store)
        try:
            await exit_location_mismatch.send_async(
                None, proxy_id="gone", project_id="p", expected="GB", observed="US", ip=ECHO_IP
            )
        finally:
            await attributor.stop()
        store.update_proxy.assert_not_awaited()


class TestReattribute:
    async def test_reattributes_known_ips_only_and_writes_once(
        self, geo_service: GeoService
    ) -> None:
        store = FakeStore()
        known = store.add(
            _proxy(id="known", connector_id="oxy", metadata={META_DISCOVERED_IP: GB_IP})
        )
        by_display = store.add(
            Proxy(id="disp", host="gw", port=1, connector_id="oxy", display_host=GB_IP)
        )
        store.add(_proxy(id="nothing", connector_id="oxy"))
        store.add(
            Proxy(
                id="hostname",
                host="gw",
                port=1,
                connector_id="oxy",
                display_host="proxy.example.com",
            )
        )
        other = store.add(
            _proxy(id="other", connector_id="trusting-c", metadata={META_DISCOVERED_IP: GB_IP})
        )
        # No database covers this exit; the discovery endpoint's country, kept on the proxy, still decides.
        endpoint_only = store.add(
            _proxy(
                id="endpoint",
                connector_id="oxy",
                metadata={META_DISCOVERED_IP: "203.0.113.77", "endpoint_country": "NL"},
            )
        )
        attributor = await _attributor(geo_service, store)
        try:
            geo_service.observation_recorder._buffer.clear()
            scanned, changed = await attributor.reattribute_all()
            assert scanned >= 4 and changed == 4
            # Re-attribution records a judgement per proxy scanned and no sighting.
            recorded = geo_service.observation_recorder._buffer
            assert len(recorded) == scanned and all(isinstance(r, ExitJudgement) for r in recorded)
            assert endpoint_only.country == "NL"
            store.update_proxies.assert_awaited_once()
            assert {p.id for p in store.update_proxies.await_args.args[0]} == {
                "known",
                "disp",
                "other",
                "endpoint",
            }
            assert known.country == "GB" and by_display.country == "GB" and other.country == "GB"
            store.update_proxies.reset_mock()
            # A second run re-judges the same proxies and records observations
            # for each, but changes nothing.
            assert (await attributor.reattribute_all())[1] == 0
            store.update_proxies.assert_not_awaited()
            assert (await attributor.reattribute_all("trusting-c"))[1] == 0
        finally:
            await attributor.stop()

    async def test_without_store(self, geo_service: GeoService) -> None:
        attributor = ProxyAttributor(geo_service, _settings())
        assert await attributor.reattribute_all() == (0, 0)
        assert await attributor.enrich("x") is None
