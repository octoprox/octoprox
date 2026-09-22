# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for GeoService: applying resolutions to proxies and recording observations."""

from pathlib import Path

import pytest

from api.core.config import Settings
from api.geo.models import (
    META_COUNTRY_SOURCE,
    META_LOCATION,
    META_LOCATION_CANDIDATES,
    META_LOCATION_CONFLICT,
    META_VENDOR_COUNTRY,
    ExitJudgement,
    GeoSourceKind,
    ObservationSource,
    SourcePolicy,
)
from api.geo.observations import ObservationRecorder
from api.geo.service import MANUAL_SOURCE, GeoService
from api.geo.settings import GeoSettingsStore
from api.geo.store import GeoDatabaseStore
from api.models.proxy import Proxy
from api.providers.sdk.strategies import META_COUNTRY, META_DISCOVERED_IP, META_GEO
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb

GB_IP = "81.2.69.160"
UNKNOWN_IP = "203.0.113.9"


def _settings() -> Settings:
    return Settings(instance_id="test-instance", geo_lookup_ip_path="ip", geo_lookup_country_path="country")  # type: ignore[call-arg]


@pytest.fixture
async def geo_service(tmp_path: Path) -> GeoService:
    db = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
    database_store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(db), "name": "test-city"}])
    await database_store.sync_all()
    settings = _settings()
    return GeoService(settings, database_store, GeoSettingsStore(settings, None), ObservationRecorder(None))


def _proxy(**metadata: object) -> Proxy:
    return Proxy(host="gw.example", port=1, connector_id="conn-1", metadata=dict(metadata))


class TestApplyObservation:
    async def test_database_answer_becomes_country_and_location(self, geo_service: GeoService) -> None:
        proxy = _proxy()
        resolution, changed = geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.DISCOVERY)
        assert changed
        assert resolution.country == "GB" and resolution.source == GeoSourceKind.DATABASE
        assert proxy.country == "GB"
        assert proxy.display_host == GB_IP
        assert proxy.metadata[META_DISCOVERED_IP] == GB_IP
        assert proxy.metadata[META_COUNTRY_SOURCE] == "database"
        assert proxy.metadata[META_LOCATION]["city"] == "London"
        assert proxy.metadata[META_LOCATION_CONFLICT] is False
        assert proxy.metadata[META_LOCATION_CANDIDATES][0]["source"] == "database"
        assert geo_service.observation_recorder.pending == 1

    async def test_vendor_claim_contradicted(self, geo_service: GeoService) -> None:
        proxy = _proxy(**{META_GEO: "US"})
        resolution, _ = geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.DISCOVERY)
        assert resolution.conflict and resolution.claimed_country == "US"
        assert proxy.metadata[META_LOCATION_CONFLICT] is True
        assert proxy.metadata[META_VENDOR_COUNTRY] == "US"
        assert proxy.country == "GB"  # routing follows the resolved country

    async def test_vendor_claim_confirmed(self, geo_service: GeoService) -> None:
        proxy = _proxy(**{META_VENDOR_COUNTRY: "gb"})
        resolution, _ = geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.DISCOVERY)
        assert not resolution.conflict and proxy.metadata[META_LOCATION_CONFLICT] is False

    async def test_unknown_ip_falls_back_to_endpoint_then_vendor(self, geo_service: GeoService) -> None:
        proxy = _proxy()
        resolution, _ = geo_service.apply_observation(
            proxy, UNKNOWN_IP, source=ObservationSource.GEO_LOOKUP, endpoint_country="de"
        )
        assert resolution.country == "DE" and resolution.source == GeoSourceKind.ENDPOINT
        assert META_LOCATION not in proxy.metadata

        proxy2 = _proxy(**{META_GEO: "FR"})
        resolution2, _ = geo_service.apply_observation(proxy2, UNKNOWN_IP, source=ObservationSource.DISCOVERY)
        assert resolution2.country == "FR" and resolution2.source == GeoSourceKind.VENDOR and not resolution2.conflict

    async def test_nothing_known_keeps_existing_country(self, geo_service: GeoService) -> None:
        proxy = _proxy(**{META_COUNTRY: "NL"})
        resolution, _ = geo_service.apply_observation(proxy, UNKNOWN_IP, source=ObservationSource.GEO_LOOKUP)
        assert resolution.country is None
        assert proxy.country == "NL"

    async def test_manual_country_is_kept_and_verified(self, geo_service: GeoService) -> None:
        proxy = _proxy(**{META_COUNTRY: "US", META_COUNTRY_SOURCE: MANUAL_SOURCE})
        resolution, _ = geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.MANUAL)
        assert proxy.country == "US"  # manual wins for routing
        assert proxy.metadata[META_COUNTRY_SOURCE] == MANUAL_SOURCE
        assert resolution.conflict and resolution.claimed_country == "US"

    async def test_second_identical_observation_reports_no_change(self, geo_service: GeoService) -> None:
        proxy = _proxy()
        _, first = geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.HEALTH_CHECK)
        _, second = geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.HEALTH_CHECK)
        assert first and not second
        assert geo_service.observation_recorder.pending == 2  # observations are always recorded

    async def test_policy_excluding_databases(self, geo_service: GeoService) -> None:
        geo_service.settings_store._settings = geo_service.settings.model_copy(
            update={"default_sources": [GeoSourceKind.VENDOR, GeoSourceKind.ENDPOINT]}
        )
        proxy = _proxy(**{META_GEO: "US"})
        resolution, _ = geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.DISCOVERY)
        assert resolution.country == "US" and resolution.source == GeoSourceKind.VENDOR
        # The database still counts as evidence against the claim.
        assert resolution.conflict

    async def test_explicit_policy_argument(self, geo_service: GeoService) -> None:
        vendor_first = SourcePolicy(sources=[GeoSourceKind.VENDOR, GeoSourceKind.DATABASE])
        trusting = _proxy(**{META_GEO: "US"})
        resolution, _ = geo_service.apply_observation(trusting, GB_IP, source=ObservationSource.DISCOVERY, policy=vendor_first)
        assert resolution.country == "US" and resolution.source == GeoSourceKind.VENDOR and resolution.conflict
        other = _proxy(**{META_GEO: "US"})
        resolution2, _ = geo_service.apply_observation(other, GB_IP, source=ObservationSource.DISCOVERY)
        assert resolution2.country == "GB" and resolution2.source == GeoSourceKind.DATABASE

    async def test_rejudge_records_a_judgement_not_a_sighting(self, geo_service: GeoService) -> None:
        proxy = _proxy(**{META_GEO: "US"})
        resolution, changed = geo_service.rejudge(proxy, GB_IP)
        assert changed and resolution.conflict and proxy.metadata[META_LOCATION_CONFLICT] is True
        recorded = geo_service.observation_recorder._buffer[-1]
        assert isinstance(recorded, ExitJudgement)
        assert recorded.proxy_id == proxy.id and recorded.ip == GB_IP
        assert recorded.claimed_country == "US" and recorded.resolved_country == "GB" and recorded.conflict is True

    async def test_flag_preflight_mismatch(self, geo_service: GeoService) -> None:
        proxy = _proxy()
        assert geo_service.flag_preflight_mismatch(proxy, GB_IP, "US", "GB")
        assert proxy.metadata[META_LOCATION_CONFLICT] is True
        # The checked country came from the request; it is not recorded as a vendor claim.
        assert META_VENDOR_COUNTRY not in proxy.metadata and proxy.display_host == GB_IP
        assert proxy.metadata[META_LOCATION_CANDIDATES][-1] == {"source": "preflight", "origin": "endpoint", "country": "GB"}
        assert not geo_service.flag_preflight_mismatch(proxy, GB_IP, "US", "GB")

    async def test_observation_payload(self, geo_service: GeoService) -> None:
        proxy = _proxy(**{META_GEO: "US"})
        geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.DISCOVERY, project_id="proj", endpoint_country="GB")
        observation = geo_service.observation_recorder._buffer[-1]
        assert observation.proxy_id == proxy.id and observation.connector_id == "conn-1"
        assert observation.project_id == "proj" and observation.session_id is None
        assert observation.claimed_country == "US" and observation.resolved_country == "GB"
        assert observation.endpoint_country == "GB" and observation.conflict
        assert observation.instance_id == "test-instance"


class TestEchoSpec:
    async def test_echo_spec_follows_policy(self, geo_service: GeoService) -> None:
        spec = geo_service.echo_spec()
        assert spec.url == geo_service.settings.echo_url and spec.ip_path == "ip" and spec.country_path == "country"
        assert geo_service.is_echo_url(geo_service.settings.echo_url + "/")
        assert geo_service.discoverer() is geo_service.discoverer()  # cached until the spec moves
        geo_service.settings_store._settings = geo_service.settings.model_copy(update={"echo_url": "https://other.example/ip"})
        assert geo_service.discoverer()._spec.url == "https://other.example/ip"
        assert not geo_service.is_echo_url("https://httpbin.org/ip")

    async def test_attribute_rejects_garbage(self, geo_service: GeoService) -> None:
        assert geo_service.attribute("not an ip") == []
        assert geo_service.attribute(GB_IP)[0].country == "GB"
