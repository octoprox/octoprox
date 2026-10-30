# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ProxyAttributor: sightings in, attributed rows saved to the batch's store."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.config import Settings
from api.db.repository import (
    ConnectorRepository,
    CredentialRepository,
    ProjectRepository,
    ProxyRepository,
)
from api.geo import attributor as attributor_module
from api.geo.attributor import ProxyAttributor
from api.geo.models import (
    META_LOCATION_CHECKED_AT,
    META_LOCATION_CONFLICT,
    META_LOCATION_VERDICT,
    META_VENDOR_COUNTRY,
    ConflictRule,
    ContradictedAction,
    ExitJudgement,
    IpObservation,
    ObservationSource,
    SourcePolicy,
    UnknownAction,
)
from api.geo.observations import ObservationRecorder
from api.geo.preflight import PreflightChecker
from api.geo.service import GeoService
from api.geo.settings import GeoSettingsStore
from api.geo.store import GeoDatabaseStore
from api.jobs.messages import SightingMessage
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy, ProxyProtocol
from api.providers.sdk.sources import IpDiscoverer
from api.providers.sdk.strategies import (
    META_DISCOVERED_IP,
    META_DYNAMIC_SESSIONS,
    META_GEO_COUNTRY,
    META_SESSION_ID,
)
from tests.geo.doubles import BatchStore, Retired, Sightings
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb

GB_IP = "81.2.69.160"
ECHO_IP = "198.51.100.7"  # not in the database; the echo endpoint's country decides
T0 = datetime(2026, 10, 17, 12, 0, 0)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "geo_lookup_url": "https://geo.example.test/myip.json",
        "geo_lookup_ip_path": "ip",
        "geo_lookup_country_path": "country",
        "instance_id": "worker-1",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def use_echo(geo_service: GeoService, payload: object, status: int = 200, seen: list[str] | None = None) -> None:
    """Make every echo request the service issues answer ``payload`` without a network."""

    def client_factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
        if seen is not None:
            seen.append(proxy_url)
        transport = httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
        return httpx.AsyncClient(transport=transport, timeout=timeout)

    geo_service.discoverer = lambda: IpDiscoverer(geo_service.echo_spec(), client_factory=client_factory)  # type: ignore[method-assign]


def _store() -> BatchStore:
    """Two projects, one inheriting the install's rule and one trusting the first database; three connectors."""
    return BatchStore(
        projects=[
            Project(id="p", name="P", username="u", password="pw"),
            Project(id="trusting", name="T", username="t", password="pw", location_conflict_rule=ConflictRule.FIRST),
        ],
        connectors=[
            Connector(id="static", name="s", credential_id="c", credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="p"),
            Connector(id="oxy", name="o", credential_id="c", credential_type="oxylabs", project_id="p"),
            Connector(id="trusting-c", name="tc", credential_id="c", credential_type="oxylabs", project_id="trusting"),
        ],
    )


def _proxy(**kwargs: object) -> Proxy:
    kwargs.setdefault("connector_id", "static")
    return Proxy(host="203.0.113.10", port=8080, protocol=ProxyProtocol.HTTP, username="u", password="p", **kwargs)  # type: ignore[arg-type]


def _slot(store: BatchStore, **metadata: object) -> Proxy:
    """A pooled session slot of the oxy connector, provisioned for the US."""
    return store.add(_proxy(connector_id="oxy", metadata={META_SESSION_ID: "abc", META_GEO_COUNTRY: "US", **metadata}))


def _recorded(geo_service: GeoService) -> list[IpObservation | ExitJudgement]:
    return list(geo_service.observation_recorder._buffer)


@pytest.fixture
async def geo_service(tmp_path: Path) -> GeoService:
    db = write_mmdb(tmp_path / "city.mmdb", "GeoLite2-City", {"81.2.69.0/24": MAXMIND_RECORD})
    database_store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(db)}])
    await database_store.sync_all()
    settings = _settings()
    return GeoService(settings, database_store, GeoSettingsStore(settings, None), ObservationRecorder(None))


@pytest.fixture
def attributor(geo_service: GeoService) -> ProxyAttributor:
    use_echo(geo_service, {"ip": ECHO_IP, "country": "de"})
    return ProxyAttributor(geo_service)


class TestRules:
    """The two rules a gateway and a worker both apply: what is a repeat, and which slot is rotated."""

    def test_a_repeat_is_a_judged_exit_seen_again_by_a_health_check_or_preflight(self) -> None:
        judged = _proxy(metadata={META_DISCOVERED_IP: GB_IP, META_LOCATION_CONFLICT: False})
        assert ProxyAttributor.is_repeat(judged, GB_IP, ObservationSource.HEALTH_CHECK)
        assert ProxyAttributor.is_repeat(judged, GB_IP, ObservationSource.PREFLIGHT)
        assert not ProxyAttributor.is_repeat(judged, ECHO_IP, ObservationSource.HEALTH_CHECK)  # the exit moved
        assert not ProxyAttributor.is_repeat(judged, GB_IP, ObservationSource.DISCOVERY)  # discovery is applied every time
        assert not ProxyAttributor.is_repeat(_proxy(metadata={META_DISCOVERED_IP: GB_IP}), GB_IP, ObservationSource.HEALTH_CHECK)  # never judged

    def test_only_a_contradicted_pooled_slot_of_a_refusing_project_is_rotated(self) -> None:
        refusing = Project(name="p", username="u", password="pw", location_contradicted=ContradictedAction.REFUSE)
        contradicted = {META_SESSION_ID: "abc", META_LOCATION_VERDICT: "contradicted"}
        assert ProxyAttributor.is_refused_slot(_proxy(metadata=contradicted), refusing)
        assert not ProxyAttributor.is_refused_slot(_proxy(metadata=contradicted), None)
        assert not ProxyAttributor.is_refused_slot(_proxy(metadata=contradicted), Project(name="p", username="u", password="pw"))
        assert not ProxyAttributor.is_refused_slot(_proxy(metadata={**contradicted, META_LOCATION_VERDICT: "open"}), refusing)
        assert not ProxyAttributor.is_refused_slot(_proxy(metadata={META_LOCATION_VERDICT: "contradicted"}), refusing)  # a fixed exit
        assert not ProxyAttributor.is_refused_slot(_proxy(metadata={**contradicted, META_DYNAMIC_SESSIONS: "true"}), refusing)

    def test_checked_at_reads_the_stamp_as_naive_utc(self) -> None:
        assert ProxyAttributor.checked_at(_proxy()) is None
        assert ProxyAttributor.checked_at(_proxy(metadata={META_LOCATION_CHECKED_AT: "nope"})) is None
        assert ProxyAttributor.checked_at(_proxy(metadata={META_LOCATION_CHECKED_AT: T0.isoformat()})) == T0
        aware = _proxy(metadata={META_LOCATION_CHECKED_AT: "2026-10-17T14:00:00+02:00"})
        assert ProxyAttributor.checked_at(aware) == T0


class TestLookup:
    async def test_locate_requests_through_the_proxy(self, geo_service: GeoService) -> None:
        seen: list[str] = []
        use_echo(geo_service, {"ip": ECHO_IP, "country": "de"}, seen=seen)
        assert await ProxyAttributor(geo_service).locate(_proxy()) == (ECHO_IP, LocationTarget(country="DE"))
        assert seen == ["http://u:p@203.0.113.10:8080"]

    async def test_locate_failure(self, geo_service: GeoService) -> None:
        use_echo(geo_service, {}, status=500)
        assert await ProxyAttributor(geo_service).locate(_proxy()) == (None, None)

    async def test_enrich_records_ip_and_country(self, attributor: ProxyAttributor, geo_service: GeoService) -> None:
        store = _store()
        proxy = store.add(_proxy())
        assert await attributor.enrich(store, proxy.id) is proxy
        assert proxy.effective_display_host == ECHO_IP and proxy.metadata[META_DISCOVERED_IP] == ECHO_IP
        assert proxy.country == "DE"  # nothing in the database; the endpoint decides
        ((saved_id, checked_at, rotate, drop),) = store.saves
        assert saved_id == proxy.id and checked_at is not None and not rotate and not drop
        await store.flush()
        assert store.rows[proxy.id].metadata[META_DISCOVERED_IP] == ECHO_IP
        assert _recorded(geo_service)[-1].source == ObservationSource.GEO_LOOKUP

    async def test_enrich_prefers_database_over_endpoint(self, geo_service: GeoService) -> None:
        use_echo(geo_service, {"ip": GB_IP, "country": "de"})
        store = _store()
        proxy = store.add(_proxy())
        await ProxyAttributor(geo_service).enrich(store, proxy.id)
        assert proxy.country == "GB" and proxy.metadata["location"]["city"] == "London"

    async def test_enrich_keeps_manual_country_when_endpoint_has_none(self, geo_service: GeoService) -> None:
        use_echo(geo_service, {"ip": ECHO_IP})
        store = _store()
        proxy = store.add(_proxy(metadata={"country": "FR", "country_source": "manual"}))
        await ProxyAttributor(geo_service).enrich(store, proxy.id)
        assert proxy.country == "FR" and proxy.effective_display_host == ECHO_IP

    async def test_enrich_saves_even_when_nothing_changed(self, attributor: ProxyAttributor) -> None:
        """Someone asked: the stamp says when it was looked at."""
        store = _store()
        proxy = store.add(_proxy())
        await attributor.enrich(store, proxy.id)
        await store.flush()
        store.load([proxy.id])
        await attributor.enrich(store, proxy.id)
        assert len(store.saves) == 2

    async def test_enrich_failure_changes_nothing(self, geo_service: GeoService) -> None:
        use_echo(geo_service, {}, status=502)
        store = _store()
        proxy = store.add(_proxy())
        assert await ProxyAttributor(geo_service).enrich(store, proxy.id) is None
        assert proxy.effective_display_host == proxy.host and store.saves == []

    async def test_enrich_missing_proxy(self, attributor: ProxyAttributor) -> None:
        assert await attributor.enrich(_store(), "gone") is None

    async def test_a_lookup_is_wanted_for_static_proxies_without_a_country(self, attributor: ProxyAttributor) -> None:
        store = _store()
        plain = store.add(_proxy(id="plain", connector_id="static"))
        manual = store.add(_proxy(id="manual", connector_id="static", metadata={"country": "GB"}))
        vendor = store.add(_proxy(id="vendor", connector_id="oxy"))
        assert attributor.wants_lookup(store, plain)
        assert not attributor.wants_lookup(store, manual) and not attributor.wants_lookup(store, vendor)
        assert not attributor.wants_lookup(store, _proxy(connector_id="unknown"))
        await attributor.enrich(store, plain.id)
        assert plain.country == "DE"

    def test_disabled_lookups_want_nothing(self, geo_service: GeoService) -> None:
        store = _store()
        proxy = store.add(_proxy())
        assert not ProxyAttributor(geo_service, lookup_enabled=False).wants_lookup(store, proxy)


class TestObservations:
    def test_a_sighting_attributes_and_saves_the_row(self, attributor: ProxyAttributor, geo_service: GeoService) -> None:
        store = _store()
        proxy = store.add(_proxy(connector_id="oxy", metadata={META_GEO_COUNTRY: "US"}))
        attributor.observe(store, proxy.id, GB_IP, source=ObservationSource.DISCOVERY, endpoint=LocationTarget(country="GB"))
        assert proxy.country == "GB" and proxy.metadata[META_LOCATION_CONFLICT] is True
        assert proxy.metadata[META_VENDOR_COUNTRY] == "US"
        assert [(saved, rotate, drop) for saved, _, rotate, drop in store.saves] == [(proxy.id, False, False)]
        # The observation carries the owning project, so project-scoped views can filter on it.
        observation = _recorded(geo_service)[-1]
        assert isinstance(observation, IpObservation)
        assert observation.project_id == "p" and observation.connector_id == "oxy"

    def test_when_and_where_the_exit_was_seen_stamp_the_row_and_the_observation(
        self, attributor: ProxyAttributor, geo_service: GeoService
    ) -> None:
        """A gateway's sighting is applied by a worker later: the gateway's time is what stays."""
        store = _store()
        proxy = store.add(_proxy(connector_id="oxy"))
        attributor.observe(store, proxy.id, GB_IP, source=ObservationSource.PREFLIGHT, observed_at=T0, session_id="s")
        assert proxy.metadata[META_LOCATION_CHECKED_AT] == T0.isoformat()
        assert store.saves == [(proxy.id, T0, False, False)]
        observation = _recorded(geo_service)[-1]
        assert isinstance(observation, IpObservation)
        assert observation.observed_at == T0 and observation.session_id == "s"
        # Without a time: now.
        other = store.add(_proxy(connector_id="oxy"))
        attributor.observe(store, other.id, GB_IP, source=ObservationSource.HEALTH_CHECK)
        assert _recorded(geo_service)[-1].observed_at > T0
        assert ProxyAttributor.checked_at(other) is not None and ProxyAttributor.checked_at(other) > T0  # type: ignore[operator]

    async def test_an_exit_move_drops_the_cached_verdict_unless_preflight_saw_it(self, attributor: ProxyAttributor) -> None:
        """A cached verdict judged the old exit; preflight caches the new one itself, so only other sources drop it."""
        store = _store()
        proxy = store.add(_proxy(connector_id="oxy"))
        attributor.observe(store, proxy.id, GB_IP, source=ObservationSource.DISCOVERY)  # first sighting: nothing cached yet
        attributor.observe(store, proxy.id, GB_IP, source=ObservationSource.HEALTH_CHECK)  # the same exit: a repeat
        attributor.observe(store, proxy.id, ECHO_IP, source=ObservationSource.HEALTH_CHECK)
        attributor.observe(store, proxy.id, GB_IP, source=ObservationSource.PREFLIGHT)
        assert [drop for *_, drop in store.saves] == [False, True, False]
        await store.flush()
        assert store.dropped_verdicts == []  # one write per row: the last save (preflight's) decides
        store.load([proxy.id])
        attributor.observe(store, proxy.id, ECHO_IP, source=ObservationSource.HEALTH_CHECK)
        await store.flush()
        assert store.dropped_verdicts == [("p", proxy.id)]

    def test_health_check_sightings_only_save_on_change(self, attributor: ProxyAttributor) -> None:
        store = _store()
        proxy = store.add(_proxy(connector_id="oxy"))
        attributor.observe(store, proxy.id, GB_IP, source=ObservationSource.HEALTH_CHECK)
        attributor.observe(store, proxy.id, GB_IP, source=ObservationSource.HEALTH_CHECK)
        assert len(store.saves) == 1
        attributor.observe(store, proxy.id, ECHO_IP, source=ObservationSource.HEALTH_CHECK, endpoint=LocationTarget(country="DE"))
        assert len(store.saves) == 2 and proxy.country == "DE"

    def test_dynamic_gateway_is_recorded_but_never_written(self, attributor: ProxyAttributor, geo_service: GeoService) -> None:
        store = _store()
        gateway = store.add(_proxy(connector_id="oxy", metadata={META_DYNAMIC_SESSIONS: "true", META_SESSION_ID: "probe"}))
        attributor.observe(store, gateway.id, GB_IP, source=ObservationSource.MANUAL)
        attributor.observe(store, gateway.id, ECHO_IP, source=ObservationSource.MANUAL, endpoint=LocationTarget(country="DE"))
        recorded = _recorded(geo_service)
        assert len(recorded) == 2
        sighting = recorded[-1]
        assert isinstance(sighting, IpObservation)
        assert sighting.connector_id == "oxy" and sighting.project_id == "p" and sighting.session_id == "probe"
        assert sighting.resolved_country == "DE"
        assert gateway.country is None and META_DISCOVERED_IP not in gateway.metadata
        assert META_LOCATION_CONFLICT not in gateway.metadata and store.saves == []

    def test_garbage_ip_and_unknown_proxy_are_ignored(self, attributor: ProxyAttributor, geo_service: GeoService) -> None:
        store = _store()
        store.add(_proxy(id="x"))
        assert attributor.observe(store, "missing", GB_IP, source=ObservationSource.DISCOVERY) is None
        assert attributor.observe(store, "x", "not-an-ip", source=ObservationSource.DISCOVERY) is None
        assert store.saves == [] and _recorded(geo_service) == []

    def test_project_policy_decides(self, attributor: ProxyAttributor, geo_service: GeoService) -> None:
        store = _store()
        trusting = store.add(_proxy(id="t", connector_id="trusting-c", metadata={META_GEO_COUNTRY: "US"}))
        default = store.add(_proxy(id="d", connector_id="oxy", metadata={META_GEO_COUNTRY: "US"}))
        assert attributor.policy_for(store.projects["trusting"]) == SourcePolicy(conflict_rule=ConflictRule.FIRST)
        assert attributor.policy_for(store.projects["p"]) == geo_service.default_policy
        assert attributor.policy_for(None) == geo_service.default_policy
        assert attributor.project_of(store, "trusting-c") is store.projects["trusting"]
        assert attributor.project_of(store, "nope") is None
        attributor.observe(store, trusting.id, GB_IP, source=ObservationSource.DISCOVERY)
        attributor.observe(store, default.id, GB_IP, source=ObservationSource.DISCOVERY)
        # The value is the database's under either rule; the rule only judges the claim.
        assert trusting.country == "GB" and trusting.metadata[META_LOCATION_CONFLICT] is True
        assert default.country == "GB" and default.metadata[META_LOCATION_CONFLICT] is True


class TestForwardOnly:
    """Batches applied out of order never roll a row back; a losing sighting is still an observation."""

    async def test_an_older_sighting_never_rolls_the_row_back(self, attributor: ProxyAttributor, geo_service: GeoService) -> None:
        store = _store()
        proxy = store.add(_proxy(connector_id="oxy"))
        attributor.observe(store, proxy.id, ECHO_IP, source=ObservationSource.HEALTH_CHECK, observed_at=T0 + timedelta(minutes=5))
        await store.flush()
        # A batch popped later carries a sighting made earlier, of the exit before.
        store.load([proxy.id])
        attributor.observe(store, proxy.id, GB_IP, source=ObservationSource.HEALTH_CHECK, observed_at=T0)
        await store.flush()
        assert store.refused == [proxy.id]
        assert store.rows[proxy.id].metadata[META_DISCOVERED_IP] == ECHO_IP
        assert store.rows[proxy.id].metadata[META_LOCATION_CHECKED_AT] == (T0 + timedelta(minutes=5)).isoformat()
        assert len(_recorded(geo_service)) == 2  # both sightings are observations

    async def test_a_losing_sighting_neither_rotates_nor_drops(self, attributor: ProxyAttributor) -> None:
        store = _store()
        store.projects["p"].location_contradicted = ContradictedAction.REFUSE
        slot = _slot(store, **{META_DISCOVERED_IP: ECHO_IP, META_LOCATION_CHECKED_AT: (T0 + timedelta(minutes=5)).isoformat()})
        # An older sighting puts the slot in GB, contradicting its US claim: rotate, and the exit moved.
        attributor.observe(store, slot.id, GB_IP, source=ObservationSource.HEALTH_CHECK, observed_at=T0)
        assert store.saves == [(slot.id, T0, True, True)]
        await store.flush()
        assert store.refused == [slot.id] and store.deleted == [] and store.dropped_verdicts == []
        assert slot.id in store.rows

    async def test_a_rejudgement_keeps_the_stamp_and_yields_to_a_newer_sighting(
        self, attributor: ProxyAttributor, geo_service: GeoService
    ) -> None:
        stamp = T0.isoformat()
        store = _store()
        proxy = store.add(_proxy(connector_id="oxy", metadata={META_DISCOVERED_IP: GB_IP, META_GEO_COUNTRY: "US", META_LOCATION_CHECKED_AT: stamp}))
        await attributor.reattribute(store)
        assert store.saves == [(proxy.id, T0, False, False)]
        assert store.rows[proxy.id].metadata[META_LOCATION_CHECKED_AT] == stamp  # a re-judgement is not a sighting
        assert store.rows[proxy.id].metadata[META_LOCATION_VERDICT] == "contradicted"

        class SightedMeanwhile(BatchStore):
            async def flush(self) -> None:
                # A sighting applied between this page's read and its write.
                self.rows[proxy.id].metadata[META_LOCATION_CHECKED_AT] = (T0 + timedelta(minutes=1)).isoformat()
                await super().flush()

        racing = SightedMeanwhile(store.projects.values(), store.connectors.values())
        racing.add(_proxy(id=proxy.id, connector_id="oxy", metadata={META_DISCOVERED_IP: GB_IP, META_GEO_COUNTRY: "GB", META_LOCATION_CHECKED_AT: stamp}))
        geo_service.observation_recorder._buffer.clear()
        await attributor.reattribute(racing)
        assert racing.refused == [proxy.id]


class TestPreflightSighting:
    """Preflight's echo through a pooled slot crosses from the gateway to a worker as one queued sighting."""

    @staticmethod
    def _checker(geo_service: GeoService) -> tuple[PreflightChecker, Sightings, Retired]:
        sink, retired = Sightings(), Retired()
        return PreflightChecker(geo_service, None, sightings=sink, exclusions=retired), sink, retired

    @staticmethod
    def _ingest(attributor: ProxyAttributor, store: BatchStore, messages: list[SightingMessage]) -> None:
        """What the worker's sighting ingest does with a popped batch: in sighting order, one observe each."""
        store.load({m.proxy_id for m in messages})
        for message in sorted(messages, key=lambda m: m.observed_at):
            attributor.observe(
                store,
                message.proxy_id,
                message.ip,
                source=message.source,
                endpoint=LocationTarget.reported(message.endpoint_country, message.endpoint_state, message.endpoint_city),
                session_id=message.session_id,
                candidates=message.candidates,
                observed_at=message.observed_at,
            )

    async def test_two_sessions_on_one_exit_give_two_observations_and_one_row_write(
        self, geo_service: GeoService, attributor: ProxyAttributor
    ) -> None:
        use_echo(geo_service, {"ip": GB_IP})
        checker, sink, retired = self._checker(geo_service)
        store = _store()
        slot = _slot(store)
        before = slot.model_copy(deep=True)
        project = Project(id="p", name="P", username="u", password="pw", location_preflight=True)
        for session in ("s1", "s2"):
            await checker.check(project, slot, session_id=session, requested=LocationTarget(country="GB"))
        # The gateway records nothing and never changes its snapshot row: the worker owns the row.
        assert _recorded(geo_service) == [] and slot == before and retired.ids == []
        assert [m.session_id for m in sink.messages] == ["s1", "s2"]
        assert all([c.country for c in m.candidates] == ["GB"] for m in sink.messages)

        self._ingest(attributor, store, sink.messages)
        await store.flush()
        # One observation per sighting, and the row is written once: the second session's sighting is a repeat.
        recorded = _recorded(geo_service)
        assert [o.session_id for o in recorded if isinstance(o, IpObservation)] == ["s1", "s2"]
        assert [o.observed_at for o in recorded] == [m.observed_at for m in sink.messages]
        assert store.written == [slot.id]
        assert store.rows[slot.id].metadata[META_LOCATION_VERDICT] == "contradicted"  # judged against its own US claim

    async def test_a_refused_slot_leaves_the_gateway_at_once_and_is_rotated_by_the_worker(
        self, geo_service: GeoService, attributor: ProxyAttributor
    ) -> None:
        use_echo(geo_service, {"ip": GB_IP})
        checker, sink, retired = self._checker(geo_service)
        store = _store()
        store.projects["p"].location_contradicted = ContradictedAction.REFUSE
        slot = _slot(store)
        project = store.projects["p"].model_copy(update={"location_preflight": True})
        await checker.check(project, slot, session_id="s1", requested=LocationTarget(country="GB"))
        assert retired.ids == [slot.id]
        self._ingest(attributor, store, sink.messages)
        await store.flush()
        assert store.deleted == [slot.id] and store.dropped_verdicts == []

    async def test_a_repeat_is_not_retired_at_the_gateway(self, geo_service: GeoService) -> None:
        """The worker does not rotate on a repeat, so neither does the gateway; a re-attribution replaces it."""
        use_echo(geo_service, {"ip": GB_IP})
        checker, sink, retired = self._checker(geo_service)
        store = _store()
        store.projects["p"].location_contradicted = ContradictedAction.REFUSE
        slot = _slot(store, **{META_DISCOVERED_IP: GB_IP, META_LOCATION_CONFLICT: True, META_LOCATION_VERDICT: "contradicted"})
        project = store.projects["p"].model_copy(update={"location_preflight": True})
        await checker.check(project, slot, session_id="s1", requested=LocationTarget(country="GB"))
        assert retired.ids == [] and len(sink.messages) == 1


class TestReattribute:
    async def test_reattributes_known_ips_only_and_writes_the_changed(
        self, attributor: ProxyAttributor, geo_service: GeoService
    ) -> None:
        store = _store()
        store.add(_proxy(id="known", connector_id="oxy", metadata={META_DISCOVERED_IP: GB_IP}))
        store.add(Proxy(id="disp", host="gw", port=1, connector_id="oxy", display_host=GB_IP))
        store.add(_proxy(id="nothing", connector_id="oxy"))
        store.add(Proxy(id="hostname", host="gw", port=1, connector_id="oxy", display_host="proxy.example.com"))
        store.add(_proxy(id="other", connector_id="trusting-c", metadata={META_DISCOVERED_IP: GB_IP}))
        # No database covers this exit; the discovery endpoint's country, kept on the proxy, still decides.
        store.add(_proxy(id="endpoint", connector_id="oxy", metadata={META_DISCOVERED_IP: "203.0.113.77", "endpoint_country": "NL"}))

        scanned, changed = await attributor.reattribute(store)
        assert (scanned, changed) == (4, 4) and store.flushes == 1
        # Re-attribution records a judgement per proxy scanned and no sighting.
        recorded = _recorded(geo_service)
        assert len(recorded) == scanned and all(isinstance(r, ExitJudgement) for r in recorded)
        assert sorted(store.written) == ["disp", "endpoint", "known", "other"]
        assert {store.rows[i].country for i in ("known", "disp", "other")} == {"GB"}
        assert store.rows["endpoint"].country == "NL"
        # A second run re-judges the same proxies, recording a judgement for each, but changes nothing.
        store.written.clear()
        assert (await attributor.reattribute(store))[1] == 0 and store.written == []
        assert (await attributor.reattribute(store, connector_id="trusting-c")) == (1, 0)

    async def test_pages_through_one_scope_in_id_order(
        self, attributor: ProxyAttributor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(attributor_module, "REATTRIBUTE_PAGE_SIZE", 2)
        store = _store()
        for n in range(5):
            store.add(_proxy(id=f"p{n}", connector_id="oxy", metadata={META_DISCOVERED_IP: GB_IP}))
        store.add(_proxy(id="t0", connector_id="trusting-c", metadata={META_DISCOVERED_IP: GB_IP}))
        assert await attributor.reattribute(store, project_id="p") == (5, 5)
        assert store.flushes == 3  # pages of 2, 2 and 1, each its own write
        assert sorted(store.written) == [f"p{n}" for n in range(5)]
        assert await attributor.reattribute(store, project_id="trusting") == (1, 1)
        assert await attributor.reattribute(store, connector_id="oxy") == (5, 0)


class TestRefusedRotation:
    """A project that refuses contradictions replaces a pooled session slot whose claim was contradicted."""

    @pytest.mark.parametrize("unknown", list(UnknownAction))
    async def test_contradicted_slot_is_replaced(self, attributor: ProxyAttributor, unknown: UnknownAction) -> None:
        store = _store()
        store.projects["p"].location_contradicted = ContradictedAction.REFUSE
        store.projects["p"].location_unknown = unknown
        slot = _slot(store)
        attributor.observe(store, slot.id, GB_IP, source=ObservationSource.HEALTH_CHECK)
        assert [(saved, rotate) for saved, _, rotate, _ in store.saves] == [(slot.id, True)]
        # Gone for the rest of the batch: a later sighting of it finds nothing.
        assert store.get_proxy(slot.id) is None
        assert attributor.observe(store, slot.id, GB_IP, source=ObservationSource.HEALTH_CHECK) is None
        await store.flush()
        assert store.deleted == [slot.id]

    async def test_slot_judged_before_the_project_refused_is_replaced_by_the_re_judgement(self, attributor: ProxyAttributor) -> None:
        # Judged contradicted while the project routed on the evidence, so it
        # stayed. The project now refuses contradictions: a repeat sighting of
        # the same exit changes nothing, the re-judgement the settings change
        # asks for is what replaces the slot.
        store = _store()
        slot = _slot(store)
        attributor.observe(store, slot.id, GB_IP, source=ObservationSource.HEALTH_CHECK)
        await store.flush()
        assert store.deleted == []
        store.projects["p"].location_contradicted = ContradictedAction.REFUSE
        store.load([slot.id])
        attributor.observe(store, slot.id, GB_IP, source=ObservationSource.HEALTH_CHECK)
        await store.flush()
        assert store.deleted == []
        await attributor.reattribute(store, project_id="p")
        assert store.deleted == [slot.id]

    def test_open_verdict_waits_for_evidence(self, attributor: ProxyAttributor) -> None:
        store = _store()
        store.projects["p"].location_contradicted = ContradictedAction.REFUSE
        store.projects["p"].location_unknown = UnknownAction.REFUSE
        slot = _slot(store)
        # Unknown to the database, no endpoint country: the claim cannot be judged.
        attributor.observe(store, slot.id, ECHO_IP, source=ObservationSource.HEALTH_CHECK)
        assert [rotate for _, _, rotate, _ in store.saves] == [False]
        assert slot.metadata[META_LOCATION_VERDICT] == "open" and slot.metadata[META_LOCATION_CONFLICT] is False

    @pytest.mark.parametrize("contradicted", [ContradictedAction.EVIDENCE, ContradictedAction.VENDOR])
    def test_routing_project_keeps_the_slot(self, attributor: ProxyAttributor, contradicted: ContradictedAction) -> None:
        store = _store()
        store.projects["p"].location_contradicted = contradicted
        slot = _slot(store)
        attributor.observe(store, slot.id, GB_IP, source=ObservationSource.HEALTH_CHECK)
        assert [rotate for _, _, rotate, _ in store.saves] == [False]
        # Routed on the evidence, or on the claim when the vendor is trusted; flagged either way.
        expected_country = "GB" if contradicted == ContradictedAction.EVIDENCE else "US"
        assert slot.metadata[META_LOCATION_VERDICT] == "contradicted" and slot.country == expected_country

    def test_fixed_exit_is_flagged_not_replaced(self, attributor: ProxyAttributor) -> None:
        store = _store()
        store.projects["p"].location_contradicted = ContradictedAction.REFUSE
        store.projects["p"].location_unknown = UnknownAction.REFUSE
        fixed = store.add(_proxy(connector_id="oxy", metadata={META_VENDOR_COUNTRY: "US"}))
        attributor.observe(store, fixed.id, GB_IP, source=ObservationSource.DISCOVERY)
        assert [rotate for _, _, rotate, _ in store.saves] == [False]
        assert fixed.metadata[META_LOCATION_CONFLICT] is True

    async def test_reattribution_replaces_contradicted_slots(self, attributor: ProxyAttributor) -> None:
        store = _store()
        store.projects["p"].location_contradicted = ContradictedAction.REFUSE
        store.projects["p"].location_unknown = UnknownAction.REFUSE
        slot = _slot(store, **{META_DISCOVERED_IP: GB_IP})
        kept = store.add(_proxy(connector_id="oxy", metadata={META_SESSION_ID: "ok", META_GEO_COUNTRY: "GB", META_DISCOVERED_IP: GB_IP}))
        assert await attributor.reattribute(store) == (2, 1)
        assert store.deleted == [slot.id]
        assert store.rows[kept.id].metadata[META_LOCATION_VERDICT] == "confirmed"

    async def test_manual_locate_replaces_a_contradicted_slot(self, geo_service: GeoService) -> None:
        store = _store()
        store.projects["p"].location_contradicted = ContradictedAction.REFUSE
        store.projects["p"].location_unknown = UnknownAction.REFUSE
        slot = _slot(store)
        # The echo reports a GB exit for a slot provisioned for US: contradicted.
        use_echo(geo_service, {"ip": GB_IP})
        assert await ProxyAttributor(geo_service).enrich(store, slot.id, source=ObservationSource.MANUAL) is slot
        assert [rotate for _, _, rotate, _ in store.saves] == [True]


class TestOutsideTheDeclaredCountries:
    """A pooled slot outside the connector's declared countries is skipped by routing, never replaced here."""

    @pytest.mark.parametrize("declared", [["US"], ["US", "GB"]])
    def test_the_slot_is_kept(self, attributor: ProxyAttributor, declared: list[str]) -> None:
        store = _store()
        store.connectors["oxy"] = store.connectors["oxy"].model_copy(update={"config": {"countries": declared}})
        slot = _slot(store)
        # Routed on the evidence (GB) under the default decision: outside a US-only list, so routing skips
        # it; replacing it would spend a session on every sighting a wrong database makes.
        attributor.observe(store, slot.id, GB_IP, source=ObservationSource.HEALTH_CHECK)
        assert [rotate for _, _, rotate, _ in store.saves] == [False]
        assert slot.country == "GB"


class _PostgresBatch(BatchStore):
    """The forward-only write through the real repository, to pin the stamp format both sides agree on."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session_factory = session_factory

    async def flush(self) -> None:
        async with self._session_factory() as session:
            repo = ProxyRepository(session)
            for proxy_id in sorted(self._pending):
                loaded, current = self._loaded[proxy_id], self._batch[proxy_id].metadata
                changed = {key: value for key, value in current.items() if loaded.get(key) != value}
                removed = {key for key in loaded if key not in current}
                written = await repo.patch_metadata(proxy_id, changed, removed, self._pending[proxy_id].checked_at)
                (self.written if written else self.refused).append(proxy_id)
            await session.commit()
        self._pending = {}


class TestForwardOnlyInPostgres:
    async def test_the_stamp_a_sighting_writes_is_the_one_the_patch_compares(
        self, attributor: ProxyAttributor, db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        project = await ProjectRepository(db_session).create(Project(name="fwd", username="fwd", password="pw"))
        credential = await CredentialRepository(db_session).create(
            Credential(name="c", type="oxylabs", project_id=project.id, config={})
        )
        connector = await ConnectorRepository(db_session).create(
            Connector(name="o", credential_id=credential.id, credential_type="oxylabs", project_id=project.id)
        )
        row = await ProxyRepository(db_session).create(_proxy(connector_id=connector.id, metadata={META_SESSION_ID: "abc"}))
        await db_session.commit()

        async def ingest(ip: str, observed_at: datetime) -> _PostgresBatch:
            store = _PostgresBatch(db_session_factory, projects=[project], connectors=[connector])
            async with db_session_factory() as session:
                loaded = await ProxyRepository(session).get_by_id(row.id)
            assert loaded is not None
            store.add(loaded)
            attributor.observe(store, row.id, ip, source=ObservationSource.HEALTH_CHECK, observed_at=observed_at)
            await store.flush()
            return store

        newer = utc_now()
        assert (await ingest(ECHO_IP, newer)).written == [row.id]
        assert (await ingest(GB_IP, newer - timedelta(seconds=30))).refused == [row.id]
        async with db_session_factory() as session:
            stored = await ProxyRepository(session).get_by_id(row.id)
        assert stored is not None and stored.metadata[META_DISCOVERED_IP] == ECHO_IP
        assert ProxyAttributor.checked_at(stored) == newer
        assert (await ingest(GB_IP, newer + timedelta(seconds=30))).written == [row.id]
