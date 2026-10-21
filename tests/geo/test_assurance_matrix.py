# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The location matrix: every verdict shape against every setting, on the row and at preflight.

This file is the specification of a project's two location decisions, so a
cell that changes here is a change of behaviour, and the docs change with it.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from api.core.config import Settings
from api.geo.models import (
    MANUAL_SOURCE,
    META_COUNTRY_SOURCE,
    META_LOCATION_CHECKED_AT,
    META_LOCATION_CONFLICT,
    META_LOCATION_VERDICT,
    ConflictRule,
    ContradictedAction,
    LocationRules,
    ObservationSource,
    SourcePolicy,
    UnknownAction,
)
from api.geo.observations import ObservationRecorder
from api.geo.preflight import PreflightChecker
from api.geo.resolver import endpoint_candidate, resolve, vendor_candidate
from api.geo.service import GeoService
from api.geo.settings import GeoSettingsStore
from api.geo.store import GeoDatabaseStore
from api.geo.verifier import ExitVerifier
from api.models.connector import Connector
from api.models.credential import Credential, CredentialType
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy, ProxyStatus
from api.providers.sdk.sources import IpDiscoverer
from api.routing.router import Router
from api.strategies import get_strategy
from tests.geo.doubles import Refusals, Retired, Sightings, logged_refusals
from tests.geo.test_readers import MAXMIND_RECORD, write_mmdb
from tests.geo.test_resolver import db
from tests.routing.builder import FeedBuilder

EVIDENCE, VENDOR_CLAIM, REFUSE = ContradictedAction.EVIDENCE, ContradictedAction.VENDOR, ContradictedAction.REFUSE
TRUST, REFUSE_UNKNOWN = UnknownAction.VENDOR, UnknownAction.REFUSE
# Every pair of the two decisions, the matrix's columns.
SETTINGS = tuple(LocationRules(c, u) for c in ContradictedAction for u in UnknownAction)

# Exits the test database knows: one with a city, one with the country alone, one not at all.
CITY_IP = "81.2.69.160"  # GB, ENG, London
COUNTRY_ONLY_IP = "81.2.70.5"  # GB, nothing below
UNKNOWN_IP = "203.0.113.5"


def _serves(predicate: Callable[[LocationRules], bool]) -> dict[LocationRules, bool]:
    return {rules: predicate(rules) for rules in SETTINGS}


ALL = _serves(lambda r: True)
NONE = _serves(lambda r: False)
# A contradicted claim routed on the evidence: every setting that does not refuse contradictions.
CONTRADICTED = _serves(lambda r: r.contradicted != REFUSE)
# A contradicted claim whose routing value the vendor supplied: only routing on the claim keeps it.
CLAIM_ONLY = _serves(lambda r: r.contradicted == VENDOR_CLAIM)
# An open claim: every setting that trusts the vendor where nothing independent answers.
OPEN = _serves(lambda r: r.unknown == TRUST)

JUDGED = {META_LOCATION_CHECKED_AT: "2026-10-14T00:00:00+00:00"}


# --- the row -----------------------------------------------------------------------

# Rows a project may route to, by the verdict attribution wrote on them.
# ``metadata`` is the row beyond its status; ``declared`` the connector's
# country list; ``asked`` what the client asked for, so a cell says whether
# the row is eligible for that request.
ROW_CASES: list[tuple[str, dict[str, object], list[str], LocationTarget | None, dict[LocationRules, bool]]] = [
    # Country level: the verdict on the vendor's claim, and where the value came from.
    ("country-unjudged-slot", {"geo": "GB"}, [], LocationTarget(country="GB"), ALL),
    ("country-unjudged-static", {"country": "GB"}, [], LocationTarget(country="GB"), ALL),
    ("country-no-claim-placed", {"country": "GB", META_COUNTRY_SOURCE: "database", META_LOCATION_CONFLICT: False, **JUDGED}, [], LocationTarget(country="GB"), ALL),
    ("country-no-claim-echo-placed", {"country": "GB", META_COUNTRY_SOURCE: "endpoint", META_LOCATION_CONFLICT: False, **JUDGED}, [], LocationTarget(country="GB"), ALL),
    ("country-confirmed", {"country": "GB", META_COUNTRY_SOURCE: "database", META_LOCATION_CONFLICT: False, META_LOCATION_VERDICT: "confirmed", **JUDGED}, [], LocationTarget(country="GB"), ALL),
    ("country-open", {"country": "GB", META_COUNTRY_SOURCE: "vendor", META_LOCATION_CONFLICT: False, META_LOCATION_VERDICT: "open", **JUDGED}, [], LocationTarget(country="GB"), OPEN),
    ("country-open-disagreeing-databases", {"country": "GB", META_COUNTRY_SOURCE: "database", META_LOCATION_CONFLICT: False, META_LOCATION_VERDICT: "open", **JUDGED}, [], LocationTarget(country="GB"), OPEN),
    ("country-contradicted", {"country": "GB", META_COUNTRY_SOURCE: "database", META_LOCATION_CONFLICT: True, META_LOCATION_VERDICT: "contradicted", **JUDGED}, [], LocationTarget(country="GB"), CONTRADICTED),
    ("country-legacy-flag", {"country": "GB", META_COUNTRY_SOURCE: "database", META_LOCATION_CONFLICT: True, **JUDGED}, [], LocationTarget(country="GB"), CONTRADICTED),
    # A country pinned by hand is the operator's word and stays the routing value: trusted while open, shown as
    # contradicted and kept unless the project refuses contradictions outright.
    ("country-pinned-open", {"country": "GB", META_COUNTRY_SOURCE: MANUAL_SOURCE, META_LOCATION_CONFLICT: False, META_LOCATION_VERDICT: "open", **JUDGED}, [], LocationTarget(country="GB"), ALL),
    ("country-pinned-contradicted", {"country": "GB", META_COUNTRY_SOURCE: MANUAL_SOURCE, META_LOCATION_CONFLICT: True, META_LOCATION_VERDICT: "contradicted", **JUDGED}, [], LocationTarget(country="GB"), CONTRADICTED),
    # The row verdict is read for every request, not only for the claimed country.
    ("country-contradicted-untargeted", {"country": "GB", META_COUNTRY_SOURCE: "database", META_LOCATION_CONFLICT: True, META_LOCATION_VERDICT: "contradicted", **JUDGED}, [], None, CONTRADICTED),
    ("country-open-untargeted", {"country": "GB", META_COUNTRY_SOURCE: "vendor", META_LOCATION_CONFLICT: False, META_LOCATION_VERDICT: "open", **JUDGED}, [], None, OPEN),
    # A judged row that nothing could place has no country: never for a country, and only where the vendor's word is trusted.
    ("country-unplaced-judged", {**JUDGED}, [], None, OPEN),
    ("country-unplaced-judged-asked", {**JUDGED}, [], LocationTarget(country="GB"), NONE),
    # The connector's declared countries bound every setting: a row the vendor placed in a declared country but which
    # the evidence puts outside them is never routed to. A row that made no claim is bound only for a named country.
    ("list-inside", {"country": "GB", META_COUNTRY_SOURCE: "database", META_LOCATION_CONFLICT: False, **JUDGED}, ["GB", "DE"], None, ALL),
    ("list-outside-no-claim", {"country": "FR", META_COUNTRY_SOURCE: "database", META_LOCATION_CONFLICT: False, **JUDGED}, ["GB", "DE"], None, ALL),
    ("list-outside-no-claim-asked", {"country": "FR", META_COUNTRY_SOURCE: "database", META_LOCATION_CONFLICT: False, **JUDGED}, ["GB", "DE"], LocationTarget(country="GB"), NONE),
    ("list-outside-claimed-open", {"geo": "GB", "country": "FR", META_COUNTRY_SOURCE: "database", META_LOCATION_CONFLICT: False, META_LOCATION_VERDICT: "open", **JUDGED}, ["GB"], None, NONE),
    ("list-outside-contradicted", {"geo": "GB", "country": "FR", META_COUNTRY_SOURCE: "database", META_LOCATION_CONFLICT: True, META_LOCATION_VERDICT: "contradicted", **JUDGED}, ["GB"], None, NONE),
    ("list-no-country-yet", {"geo": "GB"}, ["GB"], None, ALL),
    # A pinned country is the operator's word and is not bound by the connector's list.
    ("list-outside-pinned", {"geo": "GB", "country": "CH", META_COUNTRY_SOURCE: MANUAL_SOURCE, META_LOCATION_CONFLICT: False, **JUDGED}, ["GB"], None, ALL),
    # City level, on a fixed exit whose country is confirmed. Where the city came
    # from is its evidence: a database or the echo placed it, the vendor only
    # claimed it, an operator pinned it. ``city_verdict`` is how a claim fared.
    ("city-from-database", {"country": "GB", "city": "London", "city_source": "database"}, [], LocationTarget(country="GB", city="london"), ALL),
    ("city-from-endpoint", {"country": "GB", "city": "london", "city_source": "endpoint"}, [], LocationTarget(country="GB", city="london"), ALL),
    # A city known only from the databases' record on the row (no resolved value written): placed by a database.
    ("city-from-database-record", {"country": "GB", "location": {"country": "GB", "city": "London"}}, [], LocationTarget(country="GB", city="london"), ALL),
    ("city-from-vendor-unjudged", {"country": "GB", "city": "london", "city_source": "vendor"}, [], LocationTarget(country="GB", city="london"), OPEN),
    ("city-from-vendor-confirmed", {"country": "GB", "city": "london", "city_source": "vendor", "city_verdict": "confirmed"}, [], LocationTarget(country="GB", city="london"), ALL),
    ("city-from-vendor-open", {"country": "GB", "city": "london", "city_source": "vendor", "city_verdict": "open"}, [], LocationTarget(country="GB", city="london"), OPEN),
    ("city-from-vendor-contradicted", {"country": "GB", "city": "london", "city_source": "vendor", "city_verdict": "contradicted"}, [], LocationTarget(country="GB", city="london"), CLAIM_ONLY),
    ("city-pinned", {"country": "GB", "manual_location": {"city": "london"}}, [], LocationTarget(country="GB", city="london"), ALL),
    ("city-pinned-contradicted", {"country": "GB", "manual_location": {"city": "london"}, "city_verdict": "contradicted"}, [], LocationTarget(country="GB", city="london"), CONTRADICTED),
    ("city-unknown", {"country": "GB"}, [], LocationTarget(country="GB", city="london"), NONE),
    # A city verdict only matters when a city was asked for.
    ("city-contradicted-country-request", {"country": "GB", "city": "london", "city_source": "vendor", "city_verdict": "contradicted"}, [], LocationTarget(country="GB"), ALL),
    # The state reads the same way.
    ("state-from-database", {"country": "GB", "state_code": "ENG", "state_source": "database"}, [], LocationTarget(country="GB", state="ENG"), ALL),
    ("state-from-vendor-unjudged", {"country": "GB", "state_code": "ENG", "state_source": "vendor"}, [], LocationTarget(country="GB", state="ENG"), OPEN),
    ("state-from-vendor-contradicted", {"country": "GB", "state_code": "ENG", "state_source": "vendor", "state_verdict": "contradicted"}, [], LocationTarget(country="GB", state="ENG"), CLAIM_ONLY),
]


def _feed(rules: LocationRules, declared: list[str]) -> FeedBuilder:
    """A project deciding by ``rules``, with one static connector declaring ``declared``."""
    return FeedBuilder().add(
        Project(
            id="p", name="P", username="p", password="pw", location_contradicted=rules.contradicted, location_unknown=rules.unknown,
        ),
        Credential(id="c", name="Static", type=CredentialType.STATIC_PROXY_PROVIDER, project_id="p", config={}),
        Connector(
            id="conn", name="C", credential_id="c", credential_type=CredentialType.STATIC_PROXY_PROVIDER, project_id="p",
            config={"countries": declared} if declared else {},
        ),
    )


class TestRowMatrix:
    @pytest.mark.parametrize("rules", SETTINGS, ids=[f"{r.contradicted}-{r.unknown}" for r in SETTINGS])
    @pytest.mark.parametrize(("name", "metadata", "declared", "asked", "serves"), ROW_CASES, ids=[c[0] for c in ROW_CASES])
    def test_row(
        self, rules: LocationRules, name: str, metadata: dict[str, object], declared: list[str],
        asked: LocationTarget | None, serves: dict[LocationRules, bool],
    ) -> None:
        feed = _feed(rules, declared)
        feed.add(Proxy(id=name, host=name, port=1, connector_id="conn", metadata=dict(metadata), status=ProxyStatus.HEALTHY))
        eligible = {p.id for p in feed.router().get_routable_proxies_for_project("p", location=asked)}
        assert (name in eligible) is serves[rules], f"{name} under {rules}: expected serves={serves[rules]}"


# --- the value ---------------------------------------------------------------------


class TestValue:
    """The routing value is the independent answer; the vendor fills a gap, or carries a contradiction when chosen."""

    def test_databases_then_echo_then_vendor(self) -> None:
        policy = SourcePolicy()
        both = resolve([db("mm", "GB"), endpoint_candidate(LocationTarget.reported("FR")), vendor_candidate(LocationTarget.reported("US"))], policy)  # type: ignore[list-item]
        assert both.resolved_country == "GB" and both.resolved_source == "database"
        echo = resolve([endpoint_candidate(LocationTarget.reported("FR")), vendor_candidate(LocationTarget.reported("US"))], policy)  # type: ignore[list-item]
        assert echo.resolved_country == "FR" and echo.resolved_source == "endpoint" and echo.country_conflict is True
        gap = resolve([db("mm", None), vendor_candidate(LocationTarget.reported("US"))], policy)  # type: ignore[list-item]
        assert gap.resolved_country == "US" and gap.resolved_source == "vendor" and gap.country_conflict is None

    def test_a_resolution_always_carries_the_independent_answer(self) -> None:
        agreeing = [db("mm", "GB"), db("dbip", "GB"), vendor_candidate(LocationTarget.reported("US"))]
        for rule in ConflictRule:
            for contradicted in ContradictedAction:
                result = resolve(agreeing, SourcePolicy(conflict_rule=rule, contradicted=contradicted))  # type: ignore[arg-type]
                assert result.resolved_country == "GB" and result.observed_country == "GB" and result.country_conflict is True

    async def test_an_open_claim_carries_the_vendor_word_when_unknown_exits_are_trusted(self, geo_service: GeoService) -> None:
        """Databases that disagree leave the claim open; the row routes on the vendor's word under ``vendor``, on the top database under ``refuse``."""
        split = [db("mm", "FR"), db("dbip", "DE")]
        for unknown, expected in ((UnknownAction.VENDOR, ("DE", "vendor")), (UnknownAction.REFUSE, ("FR", "database"))):
            slot = Proxy(host="s", port=1, connector_id="conn", metadata={"session_id": "abc", "geo": "DE"})
            resolution, _ = geo_service.apply_observation(
                slot, CITY_IP, source=ObservationSource.HEALTH_CHECK, policy=SourcePolicy(unknown=unknown), candidates=split,
            )
            # The resolution itself is always the evidence; the row's value is the project's choice.
            assert resolution.country_conflict is None and resolution.resolved_country == "FR"
            assert (slot.country, slot.metadata["country_source"]) == expected
            assert slot.metadata[META_LOCATION_VERDICT] == "open"

    async def test_the_row_carries_the_claim_only_when_the_vendor_is_chosen(self, geo_service: GeoService) -> None:
        for contradicted in ContradictedAction:
            slot = Proxy(host="s", port=1, connector_id="conn", metadata={"session_id": "abc", "geo": "US"})
            resolution, _ = geo_service.apply_observation(
                slot, CITY_IP, source=ObservationSource.HEALTH_CHECK, policy=SourcePolicy(contradicted=contradicted)
            )
            # What was resolved is the evidence; what the row routes on is the project's choice.
            assert resolution.resolved_country == "GB" and resolution.country_conflict is True
            expected = ("US", "vendor") if contradicted == VENDOR_CLAIM else ("GB", "database")
            assert (slot.country, slot.metadata["country_source"]) == expected
            assert slot.metadata[META_LOCATION_VERDICT] == "contradicted"

    def test_disagreeing_databases_under_each_rule(self) -> None:
        # MaxMind (top priority) says FR, DB-IP agrees with the vendor's DE.
        split = [db("mm", "FR"), db("dbip", "DE"), vendor_candidate(LocationTarget.reported("DE"))]
        consensus = resolve(split, SourcePolicy(conflict_rule=ConflictRule.CONSENSUS))  # type: ignore[arg-type]
        assert consensus.country_conflict is None and consensus.resolved_country == "FR"
        first = resolve(split, SourcePolicy(conflict_rule=ConflictRule.FIRST))  # type: ignore[arg-type]
        assert first.country_conflict is True and first.resolved_country == "FR"
        # ``any`` confirms on the agreeing database, and a confirmed claim is routed on the value that confirmed it.
        any_ = resolve(split, SourcePolicy(conflict_rule=ConflictRule.ANY))  # type: ignore[arg-type]
        assert any_.country_conflict is False and any_.resolved_country == "DE" and any_.resolved_origin == "dbip"
        # What was observed is the agreeing source too: a confirmed claim never reports a contradicting observation.
        assert any_.observed_country == "DE"
        # With no database agreeing, ``any`` is a plain contradiction routed on the top-priority answer.
        none_agree = resolve([db("mm", "FR"), db("dbip", "ES"), vendor_candidate(LocationTarget.reported("DE"))], SourcePolicy(conflict_rule=ConflictRule.ANY))  # type: ignore[arg-type]
        assert none_agree.country_conflict is True and none_agree.resolved_country == "FR"
        # ``any`` never leaves a claim open while there is evidence; without evidence it is open like the others.
        assert resolve([vendor_candidate(LocationTarget.reported("DE"))], SourcePolicy(conflict_rule=ConflictRule.ANY)).country_conflict is None  # type: ignore[list-item]


# --- preflight -----------------------------------------------------------------------

# What preflight found for one request through a session row, and
# whether each setting forwards it. ``requested`` is the client's own place,
# ``metadata`` the row (``dynamic_sessions``: a gateway; ``geo``: the country
# a slot was provisioned for or a session was rendered with; ``geo_city``: a
# city rendered into a dynamic request), ``allowed`` the connector's declared
# countries, ``exit`` where the echo lands. ``contradicted`` says the verdict
# is a contradiction. Every refusal is logged by the exit verifier;
# what it does to a pooled slot is decided by the slot's own sighting (see
# ``TestPreflightSighting``), not here. ``message`` is the start of the 502 text; ``{outside}`` is the
# reason when the exit left the connector's countries, a plain mismatch otherwise.
PREFLIGHT_CASES: list[
    tuple[str, LocationTarget | None, dict[str, object], list[str], str, dict[LocationRules, bool], bool, str | None]
] = [
    # The client asked for the place: the exit is held to it, unless the vendor's claim is trusted over the evidence.
    ("request-country-match", LocationTarget(country="GB"), {}, [], CITY_IP, ALL, False, None),
    ("request-country-contradicted", LocationTarget(country="US"), {}, [], CITY_IP, CLAIM_ONLY, True, "Exit country mismatch: requested US, observed GB"),
    ("request-country-open", LocationTarget(country="US"), {}, [], UNKNOWN_IP, OPEN, False, "Exit country not confirmed: requested US, observed unknown"),
    ("request-city-match", LocationTarget(country="GB", city="london"), {}, [], CITY_IP, ALL, False, None),
    ("request-city-contradicted", LocationTarget(country="GB", city="manchester"), {}, [], CITY_IP, CLAIM_ONLY, True, "Exit city mismatch: requested GB, city manchester, observed GB, city london"),
    ("request-city-open", LocationTarget(country="GB", city="london"), {}, [], COUNTRY_ONLY_IP, OPEN, False, "Exit city not confirmed: requested GB, city london, observed GB"),
    ("request-state-open", LocationTarget(country="GB", state="ENG"), {}, [], COUNTRY_ONLY_IP, OPEN, False, "Exit state not confirmed: requested GB, state ENG, observed GB"),
    ("request-city-country-open", LocationTarget(country="GB", city="london"), {}, [], UNKNOWN_IP, OPEN, False, "Exit country not confirmed: requested GB, city london, observed unknown"),
    # The client asked for nothing. A pooled slot is then not checked at all: its own claim is judged on the row.
    ("pooled-promise-not-checked", None, {"geo": "US"}, ["US"], CITY_IP, ALL, False, None),
    # On a dynamic gateway the place rendered into the vendor session is the vendor's promise. Refusing
    # contradictions holds the vendor to that exact place; routing on the evidence holds the exit to the
    # connector's declared countries instead; trusting the vendor's claim verifies nothing but placement.
    ("promise-country-match", None, {"dynamic_sessions": "true", "geo": "GB"}, [], CITY_IP, ALL, False, None),
    ("promise-country-contradicted", None, {"dynamic_sessions": "true", "geo": "US"}, [], CITY_IP, CONTRADICTED, True, "Exit country mismatch: requested US, observed GB"),
    ("promise-country-contradicted-inside-list", None, {"dynamic_sessions": "true", "geo": "US"}, ["US", "GB"], CITY_IP, CONTRADICTED, True, "Exit country mismatch: requested US, observed GB"),
    ("promise-country-contradicted-outside-list", None, {"dynamic_sessions": "true", "geo": "US"}, ["US"], CITY_IP, CLAIM_ONLY, True, "Exit country {outside}: requested US, observed GB"),
    ("promise-country-open", None, {"dynamic_sessions": "true", "geo": "GB"}, [], UNKNOWN_IP, OPEN, False, "Exit country not confirmed: requested GB, observed unknown"),
    ("promise-city-contradicted", None, {"dynamic_sessions": "true", "geo": "GB", "geo_city": "glasgow"}, [], CITY_IP, CONTRADICTED, True, "Exit city mismatch: requested GB, city glasgow, observed GB, city london"),
    ("promise-city-open", None, {"dynamic_sessions": "true", "geo": "GB", "geo_city": "london"}, [], COUNTRY_ONLY_IP, _serves(lambda r: r != LocationRules(REFUSE, REFUSE_UNKNOWN)), False, "Exit city not confirmed: requested GB, city london, observed GB"),
    # A place merely observed on the slot is not a requirement: nothing to verify. A pin is judged on the row only.
    ("observed-country-only", None, {"country": "US", META_COUNTRY_SOURCE: "database"}, [], CITY_IP, ALL, False, None),
]

OUTSIDE = "outside the connector's countries"


@pytest.fixture
async def geo_service(tmp_path: Path) -> GeoService:
    db_path = write_mmdb(
        tmp_path / "city.mmdb",
        "GeoLite2-City",
        {"81.2.69.0/24": MAXMIND_RECORD, "81.2.70.0/24": {"country": {"iso_code": "GB", "names": {"en": "United Kingdom"}}}},
    )
    store = GeoDatabaseStore(None, tmp_path / "cache", [{"path": str(db_path)}])
    await store.sync_all()
    settings = Settings(instance_id="t", geo_lookup_ip_path="ip", geo_lookup_country_path="country")  # type: ignore[call-arg]
    return GeoService(settings, store, GeoSettingsStore(settings, None), ObservationRecorder(None))


def _checker(geo_service: GeoService, exit_ip: str) -> PreflightChecker:
    """A checker whose echo lands on ``exit_ip`` and reports no country of its own."""

    def factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ip": exit_ip})), timeout=timeout
        )

    geo_service.discoverer = lambda: IpDiscoverer(geo_service.echo_spec(), client_factory=factory)  # type: ignore[method-assign]
    return PreflightChecker(geo_service, None, sightings=Sightings(), exclusions=Retired())


def _slot(**metadata: object) -> Proxy:
    return Proxy(
        id="slot", host="slot", port=8000, connector_id="conn", username="u", password="p",
        status=ProxyStatus.HEALTHY, metadata={"session_id": "abc", **metadata},
    )


def _project(rules: LocationRules) -> Project:
    return Project(
        id="proj", name="P", username="u", password="p", location_preflight=True,
        location_contradicted=rules.contradicted, location_unknown=rules.unknown,
    )


def _sticky_router(declared: list[str]) -> MagicMock:
    """A bound sticky session: a refusal fails at once, so the decision is the verdict's alone."""
    router = MagicMock(spec=Router)
    router.strategy_for_project = MagicMock(return_value=get_strategy("sticky"))
    router.select_proxy_for_project = AsyncMock(return_value=None)
    router.declared_countries = MagicMock(return_value=list(declared))
    return router


@pytest.fixture
def refusals() -> Iterator[Refusals]:
    with logged_refusals() as logged:
        yield logged


class TestPreflightMatrix:
    @pytest.mark.parametrize("rules", SETTINGS, ids=[f"{r.contradicted}-{r.unknown}" for r in SETTINGS])
    @pytest.mark.parametrize(
        ("name", "requested", "metadata", "allowed", "exit_ip", "serves", "contradicted", "message"),
        PREFLIGHT_CASES,
        ids=[c[0] for c in PREFLIGHT_CASES],
    )
    async def test_preflight(
        self, geo_service: GeoService, refusals: Refusals, rules: LocationRules, name: str,
        requested: LocationTarget | None, metadata: dict[str, object], allowed: list[str], exit_ip: str,
        serves: dict[LocationRules, bool], contradicted: bool, message: str | None,
    ) -> None:
        checker = _checker(geo_service, exit_ip)
        decision = await ExitVerifier(checker, _sticky_router(allowed)).verify(
            _project(rules), _slot(**metadata), session_id="s", location=requested, target_host="x"
        )
        assert decision.rejected is (not serves[rules]), f"{name} under {rules}: {decision.rejection}"
        assert bool(refusals.events) is decision.rejected, f"{name} under {rules}: a refusal is logged, a forward is not"
        if decision.rejected:
            assert message is not None
            # Outside the list is the reason when the evidence routes; refusing contradictions refuses the same exit as a plain mismatch.
            outside = OUTSIDE if rules.contradicted == EVIDENCE else "mismatch"
            expected = message.replace("{outside}", outside)
            assert (decision.rejection or "").startswith(expected), decision.rejection

    async def test_echo_outage_never_refuses(self, geo_service: GeoService, refusals: Refusals) -> None:
        def factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)), timeout=timeout)

        geo_service.discoverer = lambda: IpDiscoverer(geo_service.echo_spec(), client_factory=factory)  # type: ignore[method-assign]
        checker = PreflightChecker(geo_service, None, sightings=Sightings(), exclusions=Retired())
        for rules in SETTINGS:
            decision = await ExitVerifier(checker, _sticky_router([])).verify(
                _project(rules), _slot(), session_id="s", location=LocationTarget(country="US", city="austin"), target_host="x"
            )
            assert not decision.rejected
        assert refusals.events == []
