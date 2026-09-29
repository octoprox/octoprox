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
from api.models.location import LocationTarget
from api.models.proxy import Proxy
from api.providers.sdk.strategies import META_COUNTRY, META_DISCOVERED_IP, META_GEO_COUNTRY
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
        assert resolution.resolved_country == "GB" and resolution.resolved_source == GeoSourceKind.DATABASE
        assert proxy.country == "GB"
        assert proxy.display_host == GB_IP
        assert proxy.metadata[META_DISCOVERED_IP] == GB_IP
        assert proxy.metadata[META_COUNTRY_SOURCE] == "database"
        assert proxy.metadata[META_LOCATION]["city"] == "London"
        assert proxy.metadata[META_LOCATION_CONFLICT] is False
        assert proxy.metadata[META_LOCATION_CANDIDATES][0]["source"] == "database"
        assert geo_service.observation_recorder.pending == 1

    async def test_vendor_claim_contradicted(self, geo_service: GeoService) -> None:
        proxy = _proxy(**{META_GEO_COUNTRY: "US"})
        resolution, _ = geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.DISCOVERY)
        assert resolution.country_conflict and resolution.claimed_country == "US"
        assert proxy.metadata[META_LOCATION_CONFLICT] is True
        assert proxy.metadata[META_VENDOR_COUNTRY] == "US"
        assert proxy.country == "GB"  # routing follows the resolved country

    async def test_vendor_claim_confirmed(self, geo_service: GeoService) -> None:
        proxy = _proxy(**{META_VENDOR_COUNTRY: "gb"})
        resolution, _ = geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.DISCOVERY)
        assert not resolution.country_conflict and proxy.metadata[META_LOCATION_CONFLICT] is False

    async def test_unknown_ip_falls_back_to_endpoint_then_vendor(self, geo_service: GeoService) -> None:
        proxy = _proxy()
        resolution, _ = geo_service.apply_observation(
            proxy, UNKNOWN_IP, source=ObservationSource.GEO_LOOKUP, endpoint=LocationTarget.reported("de")
        )
        assert resolution.resolved_country == "DE" and resolution.resolved_source == GeoSourceKind.ENDPOINT
        assert META_LOCATION not in proxy.metadata

        proxy2 = _proxy(**{META_GEO_COUNTRY: "FR"})
        resolution2, _ = geo_service.apply_observation(proxy2, UNKNOWN_IP, source=ObservationSource.DISCOVERY)
        assert resolution2.resolved_country == "FR" and resolution2.resolved_source == GeoSourceKind.VENDOR and not resolution2.country_conflict

    async def test_nothing_known_keeps_existing_country(self, geo_service: GeoService) -> None:
        proxy = _proxy(**{META_COUNTRY: "NL"})
        resolution, _ = geo_service.apply_observation(proxy, UNKNOWN_IP, source=ObservationSource.GEO_LOOKUP)
        assert resolution.resolved_country is None
        assert proxy.country == "NL"

    async def test_manual_country_is_kept_and_verified(self, geo_service: GeoService) -> None:
        proxy = _proxy(**{META_COUNTRY: "US", META_COUNTRY_SOURCE: MANUAL_SOURCE})
        resolution, _ = geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.MANUAL)
        assert proxy.country == "US"  # manual wins for routing
        assert proxy.metadata[META_COUNTRY_SOURCE] == MANUAL_SOURCE
        assert resolution.country_conflict and resolution.claimed_country == "US"

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
        proxy = _proxy(**{META_GEO_COUNTRY: "US"})
        resolution, _ = geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.DISCOVERY)
        assert resolution.resolved_country == "US" and resolution.resolved_source == GeoSourceKind.VENDOR
        # The database still counts as evidence against the claim.
        assert resolution.country_conflict

    async def test_explicit_policy_argument(self, geo_service: GeoService) -> None:
        vendor_first = SourcePolicy(sources=[GeoSourceKind.VENDOR, GeoSourceKind.DATABASE])
        trusting = _proxy(**{META_GEO_COUNTRY: "US"})
        resolution, _ = geo_service.apply_observation(trusting, GB_IP, source=ObservationSource.DISCOVERY, policy=vendor_first)
        assert resolution.resolved_country == "US" and resolution.resolved_source == GeoSourceKind.VENDOR and resolution.country_conflict
        other = _proxy(**{META_GEO_COUNTRY: "US"})
        resolution2, _ = geo_service.apply_observation(other, GB_IP, source=ObservationSource.DISCOVERY)
        assert resolution2.resolved_country == "GB" and resolution2.resolved_source == GeoSourceKind.DATABASE

    async def test_rejudge_records_a_judgement_not_a_sighting(self, geo_service: GeoService) -> None:
        proxy = _proxy(**{META_GEO_COUNTRY: "US"})
        resolution, changed = geo_service.rejudge(proxy, GB_IP)
        assert changed and resolution.country_conflict and proxy.metadata[META_LOCATION_CONFLICT] is True
        recorded = geo_service.observation_recorder._buffer[-1]
        assert isinstance(recorded, ExitJudgement)
        assert recorded.proxy_id == proxy.id and recorded.ip == GB_IP
        assert recorded.claimed_country == "US" and recorded.resolved_country == "GB" and recorded.country_conflict is True

    async def test_observation_payload(self, geo_service: GeoService) -> None:
        proxy = _proxy(**{META_GEO_COUNTRY: "US"})
        geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.DISCOVERY, project_id="proj", endpoint=LocationTarget(country="GB"))
        observation = geo_service.observation_recorder._buffer[-1]
        assert observation.proxy_id == proxy.id and observation.connector_id == "conn-1"
        assert observation.project_id == "proj" and observation.session_id is None
        assert observation.claimed_country == "US" and observation.resolved_country == "GB"
        assert observation.endpoint_country == "GB" and observation.country_conflict
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


class TestPlaceClaims:
    def test_claimed_place_of(self) -> None:
        assert GeoService.claimed_place_of(_proxy()) is None
        assert GeoService.claimed_place_of(_proxy(geo="US", geo_state="CA", geo_city="los_angeles")) == LocationTarget(country="US", state="CA", city="los_angeles")
        assert GeoService.claimed_place_of(_proxy(manual_location={"state_code": "NY", "city": "buffalo"})) == LocationTarget(state="NY", city="buffalo")
        # What the vendor's list or discovery endpoint said outranks a pin at the same level.
        assert GeoService.claimed_place_of(_proxy(vendor_country="US", vendor_city="austin", manual_location={"city": "dallas"})) == LocationTarget(country="US", city="austin")
        assert GeoService.endpoint_place_of(_proxy(endpoint_country="GB", endpoint_city="london")) == LocationTarget(country="GB", city="london")
        assert GeoService.endpoint_place_of(_proxy()) is None

    async def test_manual_place_is_verified_and_kept(self, geo_service: GeoService) -> None:
        proxy = _proxy(manual_location={"state_code": "SCT", "city": "london"})
        resolution, _ = geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.DISCOVERY)
        assert resolution.state_conflict is True and resolution.city_conflict is False and not resolution.country_conflict
        observation = geo_service.observation_recorder._buffer[-1]
        assert (observation.claimed_state, observation.claimed_city) == ("SCT", "london")
        assert (observation.resolved_state, observation.resolved_city) == ("ENG", "london")
        assert observation.state_conflict is True and observation.city_conflict is False
        # The databases' place is recorded on the row; the pin stays what routing matches.
        assert proxy.metadata[META_LOCATION]["state_code"] == "ENG"
        assert proxy.state_code == "SCT" and proxy.city_slug == "london"


class TestResolvedPlaceOnTheRow:
    async def test_vendor_city_stands_where_no_database_knows_one(self, geo_service: GeoService) -> None:
        # No database covers the IP: under the default policy the vendor's word is what routing gets, at every level.
        proxy = _proxy(vendor_country="FR", vendor_city="paris")
        resolution, _ = geo_service.apply_observation(proxy, UNKNOWN_IP, source=ObservationSource.DISCOVERY)
        assert resolution.resolved_city == "paris" and resolution.resolved_city_source == GeoSourceKind.VENDOR
        assert resolution.city_conflict is None  # nothing independent to judge it by
        assert proxy.city_slug == "paris" and proxy.city_source == "vendor" and proxy.state_code is None

    async def test_databases_outrank_the_vendor_and_a_pin_outranks_both(self, geo_service: GeoService) -> None:
        proxy = _proxy(vendor_country="GB", vendor_city="manchester")
        resolution, _ = geo_service.apply_observation(proxy, GB_IP, source=ObservationSource.DISCOVERY)
        assert resolution.city_conflict is True and resolution.observed_city == "london" and resolution.resolved_city == "london"
        assert proxy.city_slug == "london" and proxy.city_source == "database"
        pinned = _proxy(manual_location={"city": "manchester"})
        geo_service.apply_observation(pinned, GB_IP, source=ObservationSource.DISCOVERY)
        assert pinned.city_slug == "manchester" and pinned.city_source == "manual"
        assert geo_service.observation_recorder._buffer[-1].city_conflict is True  # the pin is verified, not obeyed blindly
