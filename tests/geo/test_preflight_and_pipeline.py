# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the preflight check and the observation pipeline's pure parts."""

import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from api.core.config import Settings
from api.core.event_bus import event_bus
from api.core.signals import exit_ip_changed, exit_ip_observed
from api.db.redis import GEO_PREFLIGHT_KEY, RedisClient
from api.geo.models import (
    ContradictedAction,
    ExitJudgement,
    IpObservation,
    LocationRules,
    ObservationSource,
    UnknownAction,
)
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
from api.models.location import LocationTarget
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


# The four pairs of decisions the older tests were written against, by what they do at preflight.
TRUSTING = LocationRules(ContradictedAction.VENDOR, UnknownAction.VENDOR)  # never refuses
VERIFYING = LocationRules(ContradictedAction.REFUSE, UnknownAction.VENDOR)  # refuses contradictions
LOCATING = LocationRules(ContradictedAction.EVIDENCE, UnknownAction.REFUSE)  # refuses unknown exits
STRICT = LocationRules(ContradictedAction.REFUSE, UnknownAction.REFUSE)  # refuses both
ALL_RULES = [LocationRules(c, u) for c in ContradictedAction for u in UnknownAction]


def _project(rules: LocationRules = VERIFYING, preflight: bool = True) -> Project:
    return Project(
        id="proj", name="P", username="u", password="p", location_preflight=preflight,
        location_contradicted=rules.contradicted, location_unknown=rules.unknown,
    )


OFF = _project(preflight=False)
OBSERVE = _project(TRUSTING)
VERIFIED = _project(VERIFYING)
CONFIRMED = _project(STRICT)


def _proxy(**metadata: object) -> Proxy:
    return Proxy(host="gw", port=8000, connector_id="conn", username="u", password="p", metadata=dict(metadata))


@pytest.fixture
def sightings() -> Iterator[list[dict]]:
    """The ``exit_ip_observed`` sightings preflight hands to the attributor for pooled slots."""
    events: list[dict] = []

    async def receive(sender: object, **kwargs: object) -> None:
        events.append(kwargs)

    exit_ip_observed.connect(receive)
    yield events
    exit_ip_observed.disconnect(receive)


class TestPreflight:
    def test_applies(self) -> None:
        slot = _proxy(session_id="slot-1")
        assert not PreflightChecker.applies(OFF, LocationTarget(country="GB"), slot)
        assert PreflightChecker.applies(OBSERVE, LocationTarget(country="GB"), slot)
        assert PreflightChecker.applies(CONFIRMED, LocationTarget(country="GB"), slot)
        assert not PreflightChecker.applies(VERIFIED, None, slot)
        # A client that named nothing has no expectation: a pooled slot's own promise is judged on the row, not here.
        assert not PreflightChecker.applies(VERIFIED, None, _proxy(session_id="s", geo="US"))
        assert not PreflightChecker.applies(VERIFIED, None, _proxy(session_id="s", vendor_country="US"))
        assert not PreflightChecker.applies(VERIFIED, None, _proxy(session_id="s", country="US", country_source="manual"))
        # A dynamic gateway always applies: the rendered place is the vendor's promise, and sampling decides the rest.
        assert PreflightChecker.applies(VERIFIED, None, _proxy(dynamic_sessions="true", geo="US"))
        # A fixed exit (no vendor session) is never checked: discovery placed
        # it and health checks re-read it, whatever was asked or promised.
        assert not PreflightChecker.applies(VERIFIED, LocationTarget(country="GB"), _proxy())
        assert not PreflightChecker.applies(VERIFIED, None, _proxy(geo="US"))
        assert not PreflightChecker.applies(VERIFIED, LocationTarget(country="GB"), _proxy(country="GB", country_source="manual"))

    def test_requirement(self) -> None:
        assert PreflightChecker.requirement(None, _proxy()) == (None, None)
        assert PreflightChecker.requirement(LocationTarget(country="GB"), _proxy()) == (LocationTarget(country="GB"), "request")
        assert PreflightChecker.requirement(None, _proxy(geo="US")) == (None, None)
        assert PreflightChecker.requirement(None, _proxy(dynamic_sessions="true", geo="US")) == (LocationTarget(country="US"), "vendor")
        requested = LocationTarget(country="US", state="NY", city="new_york")
        assert PreflightChecker.requirement(requested, _proxy()) == (requested, "request")
        # What a dynamic request rendered counts like the vendor's promise.
        rendered = _proxy(dynamic_sessions="true", geo="US", geo_state="CA", geo_city="los_angeles")
        assert PreflightChecker.requirement(None, rendered) == (LocationTarget(country="US", state="CA", city="los_angeles"), "vendor")
        # A client that named the country alone is not held to the state or city the slot was provisioned for.
        assert PreflightChecker.requirement(LocationTarget(country="US"), rendered) == (LocationTarget(country="US"), "request")
        # A state or city with no country to hang on is nothing to verify.
        assert PreflightChecker.requirement(None, _proxy(geo_state="CA")) == (None, None)
        # A pin is judged on the row, never at preflight.
        assert PreflightChecker.requirement(None, _proxy(session_id="s", country="GB", country_source="manual")) == (None, None)

    async def test_match(self, geo_service: GeoService, sightings: list[dict]) -> None:
        calls: list[str] = []
        checker = _checker(geo_service, {"ip": GB_IP, "country": "GB"}, calls=calls)
        slot = _proxy()
        verdict = await checker.check(VERIFIED, slot, session_id="s", requested=LocationTarget(country="GB"))
        assert verdict.ok and verdict.observed == LocationTarget(country="GB") and verdict.expected == LocationTarget(country="GB") and verdict.reason == "match"
        assert calls == ["http://u:p@gw:8000"]
        assert checker.checks == 1 and checker.rejections == 0
        # The slot's sighting goes to the attributor, which judges the slot's own claim and records it.
        assert geo_service.observation_recorder.pending == 0
        assert len(sightings) == 1
        sighting = dict(sightings[0])
        # The databases' answer travels with the sighting, so the attributor does not look the IP up again.
        assert [c.country for c in sighting.pop("candidates")] == ["GB"]
        assert sighting == {
            "proxy_id": slot.id, "ip": GB_IP, "source": "preflight",
            "endpoint_country": "GB", "endpoint_state": None, "endpoint_city": None, "session_id": "s",
        }

    async def test_mismatch(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": GB_IP, "country": "GB"})
        verdict = await checker.check(VERIFIED, _proxy(), session_id=None, requested=LocationTarget(country="US"))
        assert not verdict.ok and verdict.observed == LocationTarget(country="GB") and verdict.expected == LocationTarget(country="US")
        assert verdict.country_conflict is True and verdict.failed_levels == ("country",)
        assert verdict.endpoint_country == "GB" and checker.max_attempts == 3
        assert checker.rejections == 1
        assert "requested US" in PreflightChecker.rejection_message(verdict, VERIFYING)

    async def test_pooled_promise_is_judged_on_the_row_not_at_preflight(self, geo_service: GeoService, sightings: list[dict]) -> None:
        checker = _checker(geo_service, {"ip": GB_IP})
        verdict = await checker.check(OBSERVE, _proxy(session_id="s", geo="US"), session_id=None, requested=None)
        assert verdict.reason == "not applicable" and checker.checks == 0 and sightings == []
        # A dynamic gateway's rendered country is the vendor's promise and is verified.
        rendered = _proxy(dynamic_sessions="true", session_id="abc", geo="US")
        verdict = await checker.check(OBSERVE, rendered, session_id="order-1", requested=None)
        assert not verdict.ok and verdict.expected == LocationTarget(country="US") and verdict.required_by == "vendor"

    async def test_untargeted_slot_with_observed_country_is_not_verified(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": GB_IP})
        verdict = await checker.check(VERIFIED, _proxy(country="US", country_source="database"), session_id=None, requested=None)
        assert verdict.ok and verdict.reason == "not applicable" and checker.checks == 0

    async def test_unknown_location_is_ok(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": "203.0.113.5"})
        verdict = await checker.check(VERIFIED, _proxy(), session_id=None, requested=LocationTarget(country="US"))
        assert verdict.ok and verdict.reason == "location unknown"
        # Open is a pass under verified and a refusal under confirmed, which wants the country placed.
        assert verdict.acceptable(TRUSTING) and verdict.acceptable(VERIFYING)
        assert not verdict.acceptable(LOCATING) and not verdict.acceptable(STRICT)
        assert PreflightChecker.rejection_message(verdict, STRICT) == "Exit country not confirmed: requested US, observed unknown (203.0.113.5)"

    async def test_echo_failure_is_ok(self, geo_service: GeoService, sightings: list[dict]) -> None:
        checker = _checker(geo_service, {}, status=503)
        verdict = await checker.check(VERIFIED, _proxy(), session_id=None, requested=LocationTarget(country="US"))
        assert verdict.ok and verdict.reason == "echo request failed"
        assert geo_service.observation_recorder.pending == 0 and sightings == []
        # An echo outage is never a refusal, whatever the level.
        assert all(verdict.acceptable(rules) for rules in ALL_RULES)

    def test_acceptable_per_level(self) -> None:
        expected = LocationTarget(country="US")
        contradicted = PreflightVerdict(ok=False, expected=expected, observed=LocationTarget(country="GB"), ip=GB_IP, reason="mismatch", country_conflict=True)
        # The client asked for US and the exit is in GB: only trusting the vendor's claim over the evidence forwards it.
        assert contradicted.acceptable(TRUSTING)
        assert not contradicted.acceptable(VERIFYING) and not contradicted.acceptable(LOCATING) and not contradicted.acceptable(STRICT)
        # A wrong city with the right country is a mismatch above observe.
        wrong_city = PreflightVerdict(ok=False, expected=LocationTarget(country="GB", city="manchester"), observed=LocationTarget(country="GB", city="london"), ip=GB_IP, reason="mismatch", country_conflict=False, city_conflict=True)
        assert wrong_city.acceptable(TRUSTING) and not wrong_city.acceptable(VERIFYING)
        # An open city under a confirmed country: the client asked for the city, so the levels that require placement refuse it.
        open_city = PreflightVerdict(ok=True, expected=LocationTarget(country="GB", city="manchester"), observed=LocationTarget(country="GB"), ip=GB_IP, reason="match", country_conflict=False, city_conflict=None, required_by="request")
        assert open_city.acceptable(TRUSTING) and open_city.acceptable(VERIFYING)
        assert not open_city.acceptable(LOCATING) and not open_city.acceptable(STRICT)
        assert open_city.open_levels == ("city",)
        # A sampled sighting with nothing required never refuses.
        sampled = PreflightVerdict(ok=True, expected=None, observed=LocationTarget(country="GB"), ip=GB_IP, reason="observed")
        assert all(sampled.acceptable(rules) for rules in ALL_RULES)

    async def test_no_expected_country_skips(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": GB_IP})
        verdict = await checker.check(VERIFIED, _proxy(), session_id=None, requested=None)
        assert verdict.ok and verdict.reason == "not applicable" and checker.checks == 0

    def test_dynamic_rows_always_apply(self) -> None:
        gateway = _proxy(dynamic_sessions="true")
        assert PreflightChecker.applies(OBSERVE, None, gateway)
        assert not PreflightChecker.applies(OFF, LocationTarget(country="GB"), gateway)

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
        verdict = await checker.check(VERIFIED, rendered, session_id="order-1", requested=LocationTarget(country="GB"))
        assert verdict.ok and verdict.reason == "match" and checker.checks == 1
        assert list(stored) == [GEO_PREFLIGHT_KEY.format(project_id="proj", proxy_id=rendered.id) + ":abc123:GB"]
        assert geo_service.observation_recorder._buffer[-1].session_id == "abc123"
        # Same vendor session again: served from the cache.
        again = await checker.check(VERIFIED, rendered, session_id="order-1", requested=LocationTarget(country="GB"))
        assert again.ok and checker.checks == 1
        # The same client session asking for another country is verified again, not served the GB verdict.
        moved = _proxy(dynamic_sessions="true", session_id="abc123", geo="US")
        moved.id = rendered.id
        mismatch = await checker.check(VERIFIED, moved, session_id="order-1", requested=LocationTarget(country="US"))
        assert not mismatch.ok and mismatch.expected == LocationTarget(country="US") and checker.checks == 2
        assert GEO_PREFLIGHT_KEY.format(project_id="proj", proxy_id=rendered.id) + ":abc123:US" in stored
        # Another session on the same gateway row is its own verification.
        other = _proxy(dynamic_sessions="true", session_id="zzz999", geo="US")
        other.id = rendered.id
        mismatch = await checker.check(VERIFIED, other, session_id="order-2", requested=LocationTarget(country="US"))
        assert not mismatch.ok and checker.checks == 3

    async def test_pooled_slot_verdict_is_reused_only_for_the_same_requirement(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": GB_IP, "country": "GB"})
        stored: dict[str, str] = {}

        async def store(key: str, verdict: PreflightVerdict, ttl: int) -> None:
            stored[key] = verdict.to_json()

        async def cached(key: str) -> PreflightVerdict | None:
            raw = stored.get(key)
            return PreflightVerdict.from_json(raw) if raw else None

        checker._store = store  # type: ignore[method-assign]
        checker._cached = cached  # type: ignore[method-assign]
        slot = _proxy(session_id="slot-1", geo="US")
        # A client asked for US: one echo, cached under the slot's key and reused for the same ask.
        asked = await checker.check(VERIFIED, slot, session_id="order-1", requested=LocationTarget(country="US"))
        assert asked.required_by == "request" and not asked.ok and checker.checks == 1
        assert list(stored) == [GEO_PREFLIGHT_KEY.format(project_id="proj", proxy_id=slot.id)]
        again = await checker.check(VERIFIED, slot, session_id="order-2", requested=LocationTarget(country="US"))
        assert again.expected == LocationTarget(country="US") and checker.checks == 1
        # A different ask through the same slot is judged afresh, not served the cached verdict for another place.
        other = await checker.check(VERIFIED, slot, session_id="order-3", requested=LocationTarget(country="GB"))
        assert other.expected == LocationTarget(country="GB") and other.ok and checker.checks == 2
        assert (await checker.check(VERIFIED, slot, session_id="order-3", requested=LocationTarget(country="GB"))).ok
        assert checker.checks == 2

    async def test_rotating_request_is_sampled_and_uncached(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": GB_IP, "country": "GB"})
        # No session_id: the request minted its own vendor session. The connector's sampling share rides along.
        skipped = await checker.check(
            VERIFIED, _proxy(dynamic_sessions="true", exit_sample_percent=0),
            session_id="1.2.3.4", requested=None,
        )
        assert skipped.reason == "not applicable" and checker.checks == 0
        rotating = _proxy(dynamic_sessions="true", exit_sample_percent=100)
        stored: list[str] = []

        async def store(key: str, verdict: PreflightVerdict, ttl: int) -> None:
            stored.append(key)

        checker._store = store  # type: ignore[method-assign]
        observed = await checker.check(VERIFIED, rotating, session_id="1.2.3.4", requested=None)
        assert observed.ok and observed.reason == "observed" and observed.observed == LocationTarget(country="GB") and checker.checks == 1
        assert stored == []  # nothing else will ever use this vendor session
        sighting = geo_service.observation_recorder._buffer[-1]
        assert sighting.claimed_country is None and sighting.resolved_country == "GB" and not sighting.country_conflict
        # A sampled rotating request that named a country is still judged.
        targeted = _proxy(dynamic_sessions="true", geo="US", exit_sample_percent=100)
        judged = await checker.check(VERIFIED, targeted, session_id=None, requested=LocationTarget(country="US"))
        assert not judged.ok and judged.reason == "mismatch" and checker.rejections == 1

    async def test_refusing_rules_verify_every_targeted_rotating_request(self, geo_service: GeoService) -> None:
        """Sampling thins observation; it never thins a guarantee."""
        checker = _checker(geo_service, {"ip": GB_IP, "country": "GB"})
        unsampled = _proxy(dynamic_sessions="true", exit_sample_percent=0)
        for project in (VERIFIED, CONFIRMED):
            verdict = await checker.check(project, unsampled, session_id=None, requested=LocationTarget(country="US"))
            assert not verdict.ok and verdict.reason == "mismatch"
        assert checker.checks == 2
        # A promised country (allow-list pick rendered into the request) counts as expected too.
        promised = _proxy(dynamic_sessions="true", geo="US", exit_sample_percent=0)
        assert not (await checker.check(VERIFIED, promised, session_id=None, requested=None)).ok
        # A vendor promise on a connector with no declared list cannot be refused under evidence + vendor:
        # the echo would change nothing, so the request is sampled; with a list it is verified every time.
        default = _project(LocationRules(ContradictedAction.EVIDENCE, UnknownAction.VENDOR))
        sampled = await checker.check(default, promised, session_id=None, requested=None)
        assert sampled.reason == "not applicable" and checker.checks == 3
        # With a list the exit is held to it best-effort under evidence: still sampled, so at 0% not checked.
        bounded = await checker.check(default, promised, session_id=None, requested=None, allowed=["US"])
        assert bounded.reason == "not applicable" and checker.checks == 3
        # On a sampled request the list applies and the refusal names it.
        always = _proxy(dynamic_sessions="true", geo="US", exit_sample_percent=100)
        checked = await checker.check(default, always, session_id=None, requested=None, allowed=["US"])
        assert not checked.ok and checker.checks == 4
        assert PreflightChecker.rejection_message(checked, default.location_rules, ["US"]).startswith("Exit country outside the connector's countries")
        # Rules that never refuse only record, so the percentage applies even when a country was asked.
        observed = await checker.check(OBSERVE, unsampled, session_id=None, requested=LocationTarget(country="US"))
        assert observed.reason == "not applicable" and checker.checks == 4

    async def test_state_and_city_are_verified_below_the_country(self, geo_service: GeoService) -> None:
        checker = _checker(geo_service, {"ip": GB_IP, "country": "GB"})
        target = LocationTarget(country="GB", state="ENG", city="london")
        ok = await checker.check(VERIFIED, _proxy(session_id="s1"), session_id="s", requested=target)
        assert ok.ok and ok.reason == "match" and ok.state_conflict is False and ok.city_conflict is False
        assert ok.expected == target and ok.observed == target
        # A wrong city is a mismatch even though the country is right; the country verdict stays clean.
        wrong = await checker.check(
            VERIFIED, _proxy(session_id="s2"), session_id=None,
            requested=LocationTarget(country="GB", city="manchester"),
        )
        assert not wrong.ok and wrong.reason == "mismatch" and wrong.city_conflict is True and wrong.state_conflict is None
        assert wrong.country_conflict is False and wrong.failed_levels == ("city",)
        assert wrong.observed == LocationTarget(country="GB", city="london")
        message = PreflightChecker.rejection_message(wrong, VERIFYING)
        assert message.startswith("Exit city mismatch: requested GB, city manchester, observed GB, city london")
        # What a dynamic request rendered is the claim when the request named nothing.
        rendered = _proxy(dynamic_sessions="true", session_id="abc", geo="GB", geo_state="SCT", geo_city="glasgow")
        judged = await checker.check(VERIFIED, rendered, session_id="order-1", requested=None)
        assert not judged.ok and judged.expected == LocationTarget(country="GB", state="SCT", city="glasgow")
        assert judged.state_conflict is True and judged.city_conflict is True
        # No database answer at a level is no verdict: the request goes through.
        unknown = _checker(geo_service, {"ip": "203.0.113.5"})
        verdict = await unknown.check(VERIFIED, _proxy(session_id="s3"), session_id=None, requested=LocationTarget(country="GB", state="SCT"))
        assert verdict.ok and verdict.reason == "location unknown" and verdict.state_conflict is None

    async def test_country_is_judged_under_the_project_conflict_rule(self, geo_service: GeoService) -> None:
        # The database says GB, the echo endpoint says FR: under consensus the independent sources
        # disagree, so a US claim is left unjudged and the request goes through, as attribution would.
        checker = _checker(geo_service, {"ip": GB_IP, "country": "FR"})
        verdict = await checker.check(VERIFIED, _proxy(session_id="s1"), session_id=None, requested=LocationTarget(country="US"))
        assert verdict.ok and verdict.country_conflict is None and verdict.reason == "match"
        # Under the first rule the top-ranked independent source decides.
        first = Project(id="proj", name="P", username="u", password="p", location_preflight=True, location_conflict_rule="first")
        verdict = await checker.check(first, _proxy(session_id="s2"), session_id=None, requested=LocationTarget(country="US"))
        assert not verdict.ok and verdict.country_conflict is True and verdict.observed == LocationTarget(country="GB")

    def test_verdict_roundtrip(self) -> None:
        verdict = PreflightVerdict(ok=False, expected=LocationTarget(country="US", state="NY"), observed=LocationTarget(country="GB", state="ENG"), ip=GB_IP, reason="mismatch", endpoint_country="GB", country_conflict=False, state_conflict=True)
        parsed = PreflightVerdict.from_json(verdict.to_json())
        assert parsed == verdict
        assert PreflightVerdict.from_json("garbage") is None
        # A verdict cached by the previous version, with bare country codes, is a miss.
        assert PreflightVerdict.from_json('{"ok": true, "expected": "GB", "observed": "GB", "ip": "1.1.1.1", "reason": "match"}') is None


@pytest.mark.usefixtures("db_engine")
class TestPreflightCache:
    async def test_exit_change_drops_the_cached_verdict(self, redis_client: RedisClient) -> None:
        checker = PreflightChecker(MagicMock(), redis_client)
        checker.start()
        try:
            key = GEO_PREFLIGHT_KEY.format(project_id="p", proxy_id="x")
            verdict = PreflightVerdict(ok=True, expected=LocationTarget(country="GB"), observed=LocationTarget(country="GB"), ip="81.2.69.160", reason="match")
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
        judgement = ExitJudgement(proxy_id="p1", connector_id="c1", ip="1.1.1.1", country_conflict=True).model_dump_json()
        observations, judgements = decode_batch([good.encode(), judgement.encode(), b"not json", json.dumps({"ip": "x"}).encode()])
        assert len(observations) == 1 and observations[0].ip == "1.1.1.1"
        assert len(judgements) == 1 and judgements[0].proxy_id == "p1" and judgements[0].country_conflict is True
