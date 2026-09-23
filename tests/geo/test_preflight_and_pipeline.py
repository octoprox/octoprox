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
from api.geo.models import ExitJudgement, IpObservation, ObservationSource, PreflightMode
from api.geo.observations import (
    ObservationRecorder,
    aggregate_exits,
    decide_hand_outs,
    decode_batch,
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

    def test_dynamic_rows_always_apply(self) -> None:
        gateway = _proxy(dynamic_sessions="true")
        assert PreflightChecker.applies(_project(PreflightMode.REPORT), None, gateway)
        assert not PreflightChecker.applies(_project(PreflightMode.OFF), "GB", gateway)

    async def test_dynamic_session_is_verified_and_cached_per_vendor_session(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": GB_IP, "country": "GB"})
        stored: dict[str, str] = {}

        async def store(key: str, verdict: PreflightVerdict, ttl: int) -> None:
            stored[key] = verdict.to_json()

        async def cached(key: str) -> PreflightVerdict | None:
            raw = stored.get(key)
            return PreflightVerdict.from_json(raw) if raw else None

        checker._store = store  # type: ignore[method-assign]
        checker._cached = cached  # type: ignore[method-assign]
        rendered = _proxy(dynamic_sessions="true", session_id="abc123", geo="GB")
        verdict = await checker.check(_project(PreflightMode.REJECT), rendered, session_id="order-1", requested_country="gb")
        assert verdict.ok and verdict.reason == "match" and checker.checks == 1
        assert list(stored) == [GEO_PREFLIGHT_KEY.format(project_id="proj", proxy_id=rendered.id) + ":abc123:GB"]
        assert geo_service.observation_recorder._buffer[-1].session_id == "abc123"
        # Same vendor session again: served from the cache.
        again = await checker.check(_project(PreflightMode.REJECT), rendered, session_id="order-1", requested_country="gb")
        assert again.ok and checker.checks == 1
        # The same client session asking for another country is verified again, not served the GB verdict.
        moved = _proxy(dynamic_sessions="true", session_id="abc123", geo="US")
        moved.id = rendered.id
        mismatch = await checker.check(_project(PreflightMode.REJECT), moved, session_id="order-1", requested_country="us")
        assert not mismatch.ok and mismatch.expected == "US" and checker.checks == 2
        assert GEO_PREFLIGHT_KEY.format(project_id="proj", proxy_id=rendered.id) + ":abc123:US" in stored
        # Another session on the same gateway row is its own verification.
        other = _proxy(dynamic_sessions="true", session_id="zzz999", geo="US")
        other.id = rendered.id
        mismatch = await checker.check(_project(PreflightMode.REJECT), other, session_id="order-2", requested_country="us")
        assert not mismatch.ok and checker.checks == 3

    async def test_rotating_request_is_sampled_and_uncached(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": GB_IP, "country": "GB"})
        # No session_id: the request minted its own vendor session. The connector's sampling share rides along.
        skipped = await checker.check(
            _project(PreflightMode.REJECT), _proxy(dynamic_sessions="true", exit_sample_percent=0),
            session_id="1.2.3.4", requested_country=None,
        )
        assert skipped.reason == "not applicable" and checker.checks == 0
        rotating = _proxy(dynamic_sessions="true", exit_sample_percent=100)
        stored: list[str] = []

        async def store(key: str, verdict: PreflightVerdict, ttl: int) -> None:
            stored.append(key)

        checker._store = store  # type: ignore[method-assign]
        observed = await checker.check(_project(PreflightMode.REJECT), rotating, session_id="1.2.3.4", requested_country=None)
        assert observed.ok and observed.reason == "observed" and observed.observed == "GB" and checker.checks == 1
        assert stored == []  # nothing else will ever use this vendor session
        sighting = geo_service.observation_recorder._buffer[-1]
        assert sighting.claimed_country is None and sighting.resolved_country == "GB" and not sighting.conflict
        # A sampled rotating request that named a country is still judged.
        targeted = _proxy(dynamic_sessions="true", geo="US", exit_sample_percent=100)
        judged = await checker.check(_project(PreflightMode.REJECT), targeted, session_id=None, requested_country="US")
        assert not judged.ok and judged.reason == "mismatch" and checker.rejections == 1

    async def test_hard_modes_verify_every_targeted_rotating_request(self, geo_service: GeoService) -> None:
        """Sampling thins observation; it never thins a reject or retry guarantee."""
        checker = _checker(geo_service, {"ip": GB_IP, "country": "GB"})
        unsampled = _proxy(dynamic_sessions="true", exit_sample_percent=0)
        for mode in (PreflightMode.REJECT, PreflightMode.RETRY):
            verdict = await checker.check(_project(mode), unsampled, session_id=None, requested_country="US")
            assert not verdict.ok and verdict.reason == "mismatch"
        assert checker.checks == 2
        # A promised country (allow-list pick rendered into the request) counts as expected too.
        promised = _proxy(dynamic_sessions="true", geo="US", exit_sample_percent=0)
        assert not (await checker.check(_project(PreflightMode.REJECT), promised, session_id=None, requested_country=None)).ok
        # Report mode only observes, so the percentage applies even when a country was asked.
        report = await checker.check(_project(PreflightMode.REPORT), unsampled, session_id=None, requested_country="US")
        assert report.reason == "not applicable" and checker.checks == 3

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
            key = GEO_PREFLIGHT_KEY.format(project_id="p", proxy_id="x")
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
        sightings = [
            (IpObservation(observed_at=late, connector_id="c1", source=ObservationSource.HEALTH_CHECK, ip="1.1.1.1", resolved_country="GB"), True),
            (IpObservation(observed_at=early, connector_id="c1", source=ObservationSource.DISCOVERY, ip="1.1.1.1", resolved_country="DE"), True),
            (IpObservation(observed_at=early, connector_id="c1", source=ObservationSource.DISCOVERY, ip="1.1.1.2"), True),
            (IpObservation(observed_at=early, connector_id=None, source=ObservationSource.MANUAL, ip="1.1.1.3"), True),
            # Preflight re-verifying an exit the proxy already had: latest state, no hand-out.
            (IpObservation(observed_at=late, connector_id="c1", source=ObservationSource.PREFLIGHT, ip="1.1.1.1", proxy_id="p1"), False),
        ]
        exits = aggregate_exits(sightings)
        assert set(exits) == {("c1", "1.1.1.1"), ("c1", "1.1.1.2")}
        first = exits[("c1", "1.1.1.1")]
        assert first.count == 2 and first.first_seen == early and first.last_seen == late and first.country == "GB"
        assert first.source == "preflight" and first.proxy_id == "p1"  # newest observation wins the state
        assert exits[("c1", "1.1.1.2")].count == 1 and exits[("c1", "1.1.1.2")].country is None

    def test_hand_outs_are_decided_against_the_last_counted_exit(self) -> None:
        """Two instances reporting the same exit, however far apart, are one hand-out; a change is one more."""
        t = datetime(2026, 9, 22, 9, 29, 9)
        hc = ObservationSource.HEALTH_CHECK
        a = IpObservation(observed_at=t, proxy_id="p1", connector_id="c1", source=ObservationSource.DISCOVERY, ip="1.1.1.1", instance_id="octoprox-2")
        b = IpObservation(observed_at=t + timedelta(seconds=50), proxy_id="p1", connector_id="c1", source=hc, ip="1.1.1.1", instance_id="octoprox-3")
        moved = IpObservation(observed_at=t + timedelta(minutes=5), proxy_id="p1", connector_id="c1", source=hc, ip="2.2.2.2")
        back = IpObservation(observed_at=t + timedelta(minutes=9), proxy_id="p1", connector_id="c1", source=hc, ip="1.1.1.1")
        fresh = IpObservation(observed_at=t, proxy_id="p2", connector_id="c1", source=hc, ip="1.1.1.1")
        known = IpObservation(observed_at=t, proxy_id="p4", connector_id="c1", source=hc, ip="4.4.4.4")

        marked, latest = decide_hand_outs([back, b, moved, a, fresh, known], previous={"p4": "4.4.4.4"})
        by_obs = {id(o): hand_out for o, hand_out in marked}
        assert by_obs[id(a)] is True and by_obs[id(b)] is False  # the same exit, from another instance, later
        assert by_obs[id(moved)] is True and by_obs[id(back)] is True  # a real change, and a real change back
        assert by_obs[id(fresh)] is True  # never counted before
        assert by_obs[id(known)] is False  # already the last counted exit
        # A sighting with no proxy cannot be a hand-out to one.
        orphan = IpObservation(observed_at=t, connector_id="c1", source=hc, ip="9.9.9.9")
        assert decide_hand_outs([orphan], previous={})[0][0][1] is False
        # What to record: every proxy whose exit is new or moved, nothing for the unchanged one.
        assert latest == {"p1": "1.1.1.1", "p2": "1.1.1.1"}

        exits = aggregate_exits(marked)
        assert exits[("c1", "1.1.1.1")].count == 3  # a, back, fresh
        assert exits[("c1", "2.2.2.2")].count == 1

        # Redis knows nothing (upgrade, Redis loss): the exit table decides.
        legacy = IpObservation(observed_at=t, proxy_id="p5", connector_id="c1", source=hc, ip="5.5.5.5")
        on_record = {("c1", "1.1.1.1"): "p1", ("c1", "5.5.5.5"): None}
        cold, _ = decide_hand_outs([a, fresh, legacy], previous={}, on_record=on_record)
        cold_by_obs = {id(o): hand_out for o, hand_out in cold}
        assert cold_by_obs[id(a)] is False  # p1 already holds 1.1.1.1 on record
        assert cold_by_obs[id(fresh)] is True  # p2 taking an IP p1 held is a hand-out
        assert cold_by_obs[id(legacy)] is False  # a row from before per-proxy state: counted before

    def test_decode_batch_splits_sightings_from_judgements_and_skips_garbage(self) -> None:
        good = IpObservation(source=ObservationSource.MANUAL, ip="1.1.1.1").model_dump_json()
        judgement = ExitJudgement(proxy_id="p1", connector_id="c1", ip="1.1.1.1", conflict=True).model_dump_json()
        observations, judgements = decode_batch([good.encode(), judgement.encode(), b"not json", json.dumps({"ip": "x"}).encode()])
        assert len(observations) == 1 and observations[0].ip == "1.1.1.1"
        assert len(judgements) == 1 and judgements[0].proxy_id == "p1" and judgements[0].conflict is True
