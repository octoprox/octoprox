# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the preflight check and the observation pipeline's pure parts."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from api.core.config import Settings
from api.core.event_bus import event_bus
from api.core.signals import exit_ip_changed
from api.db.redis import GEO_PREFLIGHT_KEY, RedisClient
from api.geo.models import IpObservation, ObservationSource, PreflightMode
from api.geo.observations import (
    ObservationRecorder,
    aggregate_exits,
    decode_batch,
    dedupe_sightings,
)
from api.geo.preflight import PreflightChecker, PreflightVerdict
from api.geo.service import GeoService
from api.geo.settings import GeoSettingsStore
from api.geo.store import GeoDatabaseStore
from api.models.project import Project
from api.models.proxy import Proxy
from api.providers.sdk.sources import IpDiscoverer
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb

GB_IP = "81.2.69.160"


def _checker(geo_service: GeoService, payload: object, status: int = 200, calls: list[str] | None = None) -> PreflightChecker:
    """A checker whose echo requests answer ``payload`` without a network."""

    def factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
        if calls is not None:
            calls.append(proxy_url)
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload)), timeout=timeout
        )

    geo_service.discoverer = lambda: IpDiscoverer(geo_service.echo_spec(), client_factory=factory)  # type: ignore[method-assign]
    return PreflightChecker(geo_service, None)


@pytest.fixture
async def geo_service(tmp_path: Path) -> GeoService:
    db = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
    database_store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(db)}])
    await database_store.sync_all()
    settings = Settings(instance_id="t", geo_lookup_ip_path="ip", geo_lookup_country_path="country")  # type: ignore[call-arg]
    return GeoService(settings, database_store, GeoSettingsStore(settings, None), ObservationRecorder(None))


def _project(mode: PreflightMode) -> Project:
    return Project(id="proj", name="P", username="u", password="p", location_preflight=mode)


def _proxy(**metadata: object) -> Proxy:
    return Proxy(host="gw", port=8000, connector_id="conn", username="u", password="p", metadata=dict(metadata))


class TestPreflight:
    def test_applies(self) -> None:
        proxy = _proxy()
        assert not PreflightChecker.applies(_project(PreflightMode.OFF), "GB", proxy)
        assert PreflightChecker.applies(_project(PreflightMode.REPORT), "GB", proxy)
        assert PreflightChecker.applies(_project(PreflightMode.RETRY), "GB", proxy)
        assert not PreflightChecker.applies(_project(PreflightMode.REJECT), None, proxy)
        # A promised country counts; a merely attributed one does not.
        assert PreflightChecker.applies(_project(PreflightMode.REJECT), None, _proxy(geo="US"))
        assert PreflightChecker.applies(_project(PreflightMode.REJECT), None, _proxy(vendor_country="US"))
        assert PreflightChecker.applies(_project(PreflightMode.REJECT), None, _proxy(country="US", country_source="manual"))
        assert not PreflightChecker.applies(_project(PreflightMode.REJECT), None, _proxy(country="US", country_source="database"))

    async def test_match(self, geo_service: GeoService) -> None:
        calls: list[str] = []
        checker = _checker(geo_service, {"ip": GB_IP, "country": "GB"}, calls=calls)
        verdict = await checker.check(_project(PreflightMode.REJECT), _proxy(), session_id="s", requested_country="gb")
        assert verdict.ok and verdict.observed == "GB" and verdict.expected == "GB" and verdict.reason == "match"
        assert calls == ["http://u:p@gw:8000"]
        assert checker.checks == 1 and checker.rejections == 0
        observation = geo_service.observation_recorder._buffer[-1]
        assert observation.source == ObservationSource.PREFLIGHT and observation.session_id == "s"
        assert observation.project_id == "proj" and not observation.conflict

    async def test_mismatch(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": GB_IP, "country": "GB"})
        verdict = await checker.check(_project(PreflightMode.REJECT), _proxy(), session_id=None, requested_country="US")
        assert not verdict.ok and verdict.observed == "GB" and verdict.expected == "US"
        assert verdict.endpoint_country == "GB" and checker.max_attempts == 3
        assert checker.rejections == 1
        assert "requested US" in PreflightChecker.rejection_message(verdict)
        assert geo_service.observation_recorder._buffer[-1].conflict

    async def test_expected_from_vendor_claim(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": GB_IP})
        verdict = await checker.check(_project(PreflightMode.REPORT), _proxy(geo="US"), session_id=None, requested_country=None)
        assert not verdict.ok and verdict.expected == "US"

    async def test_untargeted_slot_with_observed_country_is_not_verified(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": GB_IP})
        verdict = await checker.check(_project(PreflightMode.REJECT), _proxy(country="US", country_source="database"), session_id=None, requested_country=None)
        assert verdict.ok and verdict.reason == "not applicable" and checker.checks == 0

    async def test_unknown_location_is_ok(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": "203.0.113.5"})
        verdict = await checker.check(_project(PreflightMode.REJECT), _proxy(), session_id=None, requested_country="US")
        assert verdict.ok and verdict.reason == "location unknown"

    async def test_echo_failure_is_ok(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {}, status=503)
        verdict = await checker.check(_project(PreflightMode.REJECT), _proxy(), session_id=None, requested_country="US")
        assert verdict.ok and verdict.reason == "echo request failed"
        assert geo_service.observation_recorder.pending == 0

    async def test_no_expected_country_skips(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": GB_IP})
        verdict = await checker.check(_project(PreflightMode.REJECT), _proxy(), session_id=None, requested_country=None)
        assert verdict.ok and verdict.reason == "not applicable" and checker.checks == 0

    def test_verdict_roundtrip(self) -> None:
        verdict = PreflightVerdict(ok=False, expected="US", observed="GB", ip=GB_IP, reason="mismatch", endpoint_country="GB")
        parsed = PreflightVerdict.from_json(verdict.to_json())
        assert parsed == verdict
        assert PreflightVerdict.from_json("garbage") is None


@pytest.mark.usefixtures("db_engine")
class TestPreflightCache:
    async def test_exit_change_drops_the_cached_verdict(self, redis_client: RedisClient) -> None:
        checker = PreflightChecker(MagicMock(), redis_client)
        checker.start()
        try:
            key = GEO_PREFLIGHT_KEY.format(project_id="p", proxy_id="x", session_id="-")
            verdict = PreflightVerdict(ok=True, expected="GB", observed="GB", ip="81.2.69.160", reason="match")
            await checker._store(key, verdict, ttl=600)
            assert await checker._cached(key) is not None
            # Another proxy moving leaves this verdict alone.
            await event_bus.publish(exit_ip_changed, None, proxy_id="other", project_id="p", old_ip="1.1.1.1", new_ip="2.2.2.2")
            assert await checker._cached(key) is not None
            await event_bus.publish(exit_ip_changed, None, proxy_id="x", project_id="p", old_ip="81.2.69.160", new_ip="2.2.2.2")
            assert await checker._cached(key) is None
            # No owning project: nothing was ever cached under a project-less key; must not raise.
            await event_bus.publish(exit_ip_changed, None, proxy_id="x", project_id=None, old_ip="a", new_ip="b")
        finally:
            checker.stop()


class TestRecorder:
    def test_buffer_is_bounded(self) -> None:
        recorder = ObservationRecorder(None, max_buffer=3)
        for i in range(5):
            recorder.record(IpObservation(source=ObservationSource.MANUAL, ip=f"10.0.0.{i}"))
        assert recorder.pending == 3 and recorder.dropped == 2
        assert recorder._buffer[0].ip == "10.0.0.2"

    async def test_publish_without_redis(self) -> None:
        recorder = ObservationRecorder(None)
        recorder.record(IpObservation(source=ObservationSource.MANUAL, ip="10.0.0.1"))
        assert await recorder.publish() == 0
        assert recorder.pending == 1


class TestAggregate:
    def test_exits_fold_per_connector_and_ip(self) -> None:
        early = datetime(2026, 9, 21, 10, 0, 0)
        late = datetime(2026, 9, 21, 12, 0, 0)
        observations = [
            IpObservation(observed_at=late, connector_id="c1", source=ObservationSource.HEALTH_CHECK, ip="1.1.1.1", resolved_country="GB", new_exit=True),
            IpObservation(observed_at=early, connector_id="c1", source=ObservationSource.DISCOVERY, ip="1.1.1.1", resolved_country="DE", new_exit=True),
            IpObservation(observed_at=early, connector_id="c1", source=ObservationSource.DISCOVERY, ip="1.1.1.2", new_exit=True),
            IpObservation(observed_at=early, connector_id=None, source=ObservationSource.MANUAL, ip="1.1.1.3", new_exit=True),
            # Re-judging an exit the proxy already had is not a hand-out, but it
            # is the exit's latest state.
            IpObservation(observed_at=late, connector_id="c1", source=ObservationSource.REATTRIBUTE, ip="1.1.1.9", claimed_country="US", resolved_country="US"),
            IpObservation(observed_at=late, connector_id="c1", source=ObservationSource.PREFLIGHT, ip="1.1.1.1", proxy_id="p1"),
        ]
        exits = aggregate_exits(observations)
        assert set(exits) == {("c1", "1.1.1.1"), ("c1", "1.1.1.2"), ("c1", "1.1.1.9")}
        first = exits[("c1", "1.1.1.1")]
        assert first.count == 2 and first.first_seen == early and first.last_seen == late and first.country == "GB"
        assert first.source == "preflight" and first.proxy_id == "p1"  # newest observation wins the state
        assert exits[("c1", "1.1.1.2")].count == 1 and exits[("c1", "1.1.1.2")].country is None
        rejudged = exits[("c1", "1.1.1.9")]
        assert rejudged.count == 0 and rejudged.source == "reattribute" and rejudged.claimed_country == "US"

    def test_dedupe_same_sighting_from_two_instances(self) -> None:
        t = datetime(2026, 9, 22, 9, 29, 9)
        a = IpObservation(observed_at=t, proxy_id="p1", connector_id="c1", source=ObservationSource.HEALTH_CHECK, ip="1.1.1.1", instance_id="octoprox-2", new_exit=True)
        b = IpObservation(observed_at=t + timedelta(milliseconds=8), proxy_id="p1", connector_id="c1", source=ObservationSource.HEALTH_CHECK, ip="1.1.1.1", instance_id="octoprox-3", new_exit=True)
        later = IpObservation(observed_at=t + timedelta(minutes=5), proxy_id="p1", connector_id="c1", source=ObservationSource.HEALTH_CHECK, ip="1.1.1.1", new_exit=True)
        other_proxy = IpObservation(observed_at=t, proxy_id="p2", connector_id="c1", source=ObservationSource.HEALTH_CHECK, ip="1.1.1.1", new_exit=True)
        other_source = IpObservation(observed_at=t, proxy_id="p1", connector_id="c1", source=ObservationSource.DISCOVERY, ip="1.1.1.1", new_exit=True)
        kept = dedupe_sightings([b, later, a, other_proxy, other_source])
        assert b not in kept and {id(o) for o in kept} == {id(a), id(later), id(other_proxy), id(other_source)}
        # The exits aggregate sees one hand-out to p1 at t, one later, and one to p2.
        assert aggregate_exits(kept)[("c1", "1.1.1.1")].count == 4  # a, later, other_proxy, other_source
        assert aggregate_exits(dedupe_sightings([a, b]))[("c1", "1.1.1.1")].count == 1

    def test_decode_batch_skips_garbage(self) -> None:
        good = IpObservation(source=ObservationSource.MANUAL, ip="1.1.1.1").model_dump_json()
        parsed = decode_batch([good.encode(), b"not json", json.dumps({"ip": "x"}).encode()])
        assert len(parsed) == 1 and parsed[0].ip == "1.1.1.1"
