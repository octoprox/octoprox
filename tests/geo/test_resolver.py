# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the source policy: who wins, and when the vendor is contradicted."""

import pytest

from api.geo.models import (
    ConflictRule,
    GeoSourceKind,
    IpLocation,
    LocationCandidate,
    SourcePolicy,
)
from api.geo.resolver import (
    CityProximity,
    endpoint_candidate,
    judge_level,
    resolve,
    vendor_candidate,
)
from api.models.location import LocationTarget


def db(origin: str, country: str | None, **location: object) -> LocationCandidate:
    loc = IpLocation(country=country, **location) if country or location else None  # type: ignore[arg-type]
    return LocationCandidate(source=GeoSourceKind.DATABASE, origin=origin, country=country, location=loc)


class TestWinner:
    def test_database_wins_by_default(self) -> None:
        result = resolve([db("mm", "GB"), vendor_candidate(LocationTarget.reported("US")), endpoint_candidate(LocationTarget.reported("GB"))], SourcePolicy())  # type: ignore[list-item]
        assert result.resolved_country == "GB" and result.resolved_source == GeoSourceKind.DATABASE and result.resolved_origin == "mm"

    def test_database_wins_while_the_independent_sources_disagree(self) -> None:
        # Database GB, echo US: under consensus the claim is open, and the top-ranked source still decides the value.
        result = resolve([db("mm", "GB"), vendor_candidate(LocationTarget.reported("US")), endpoint_candidate(LocationTarget.reported("US"))], SourcePolicy())  # type: ignore[list-item]
        assert result.country_conflict is None
        assert result.resolved_country == "GB" and result.resolved_source == GeoSourceKind.DATABASE

    def test_falls_through_when_first_source_has_no_answer(self) -> None:
        result = resolve([db("mm", None), vendor_candidate(LocationTarget.reported("US"))], SourcePolicy())  # type: ignore[list-item]
        assert result.resolved_country == "US" and result.resolved_source == GeoSourceKind.VENDOR

    def test_vendor_never_outranks_independent_evidence(self) -> None:
        result = resolve([endpoint_candidate(LocationTarget.reported("GB")), vendor_candidate(LocationTarget.reported("US"))], SourcePolicy())  # type: ignore[list-item]
        assert result.resolved_country == "GB" and result.resolved_source == GeoSourceKind.ENDPOINT

    def test_database_priority_order_is_input_order(self) -> None:
        result = resolve([db("first", "GB"), db("second", "FR")], SourcePolicy())
        assert result.resolved_origin == "first"

    def test_no_candidates(self) -> None:
        result = resolve([], SourcePolicy())
        assert result.resolved_country is None and result.country_conflict is None


class TestConflict:
    def test_no_claim_no_conflict(self) -> None:
        result = resolve([db("mm", "GB"), endpoint_candidate(LocationTarget.reported("FR"))], SourcePolicy())  # type: ignore[list-item]
        assert result.country_conflict is None  # the databases disagree: no verdict

    def test_consensus_flags_vendor_when_all_independent_agree(self) -> None:
        result = resolve([db("mm", "GB"), db("dbip", "GB"), vendor_candidate(LocationTarget.reported("US"))], SourcePolicy())  # type: ignore[list-item]
        assert result.country_conflict is True and result.claimed_country == "US"

    def test_consensus_does_not_flag_when_databases_disagree(self) -> None:
        result = resolve([db("mm", "GB"), db("dbip", "FR"), vendor_candidate(LocationTarget.reported("US"))], SourcePolicy())  # type: ignore[list-item]
        assert result.country_conflict is None  # the databases disagree: no verdict

    def test_consensus_no_conflict_when_one_independent_agrees_with_vendor(self) -> None:
        result = resolve([db("mm", "US"), endpoint_candidate(LocationTarget.reported("GB")), vendor_candidate(LocationTarget.reported("US"))], SourcePolicy())  # type: ignore[list-item]
        assert not result.country_conflict

    def test_first_rule_flags_on_top_ranked_disagreement(self) -> None:
        policy = SourcePolicy(conflict_rule=ConflictRule.FIRST)
        result = resolve([db("mm", "GB"), db("dbip", "US"), vendor_candidate(LocationTarget.reported("US"))], policy)  # type: ignore[list-item]
        assert result.country_conflict

    def test_first_rule_judges_by_the_top_independent_source(self) -> None:
        policy = SourcePolicy(conflict_rule=ConflictRule.FIRST)
        result = resolve([db("mm", "GB"), vendor_candidate(LocationTarget.reported("US"))], policy)  # type: ignore[list-item]
        assert result.resolved_country == "GB" and result.country_conflict

    def test_agreeing_vendor_is_not_flagged(self) -> None:
        result = resolve([db("mm", "US"), vendor_candidate(LocationTarget.reported("us"))], SourcePolicy())  # type: ignore[list-item]
        assert result.country_conflict is False and result.claimed_country == "US"


class TestLocation:
    def test_merges_database_records_in_priority_order(self) -> None:
        result = resolve(
            [db("city", "GB", city="London"), db("asn", None, asn=12345, organization="Example")],
            SourcePolicy(),
        )
        assert result.location is not None
        assert result.location.city == "London" and result.location.asn == 12345

    def test_compact_candidates(self) -> None:
        result = resolve([db("mm", "GB"), vendor_candidate(LocationTarget.reported("US"))], SourcePolicy())  # type: ignore[list-item]
        assert result.compact_candidates() == [
            {"source": "database", "origin": "mm", "country": "GB"},
            {"source": "vendor", "origin": "vendor", "country": "US"},
        ]


class TestCandidates:
    def test_vendor_candidate_carries_the_whole_claim(self) -> None:
        candidate = vendor_candidate(LocationTarget(country="US", state="NY", city="new_york"))
        assert candidate is not None and candidate.source == GeoSourceKind.VENDOR
        assert (candidate.country, candidate.state, candidate.city) == ("US", "NY", "new_york")
        assert vendor_candidate(None) is None and vendor_candidate(LocationTarget()) is None

    def test_endpoint_candidate(self) -> None:
        candidate = endpoint_candidate(LocationTarget.reported("de", None, "Berlin"))
        assert candidate is not None and candidate.source == GeoSourceKind.ENDPOINT and candidate.origin == "endpoint"
        assert candidate.country == "DE" and candidate.city == "berlin"
        assert endpoint_candidate(None) is None


class TestPlace:
    """State and city are judged by the same rule as the country, from the same candidates."""

    def test_state_and_city_verdicts(self) -> None:
        databases = [db("a", "US", state_code="NY", city="New York"), db("b", "US", state_code="NY", city="New York")]
        confirmed = resolve([*databases, vendor_candidate(LocationTarget(country="US", state="NY", city="new_york"))], SourcePolicy())  # type: ignore[list-item]
        assert confirmed.state_conflict is False and confirmed.city_conflict is False and not confirmed.country_conflict
        assert (confirmed.claimed_state, confirmed.observed_state, confirmed.resolved_state) == ("NY", "NY", "NY")
        assert (confirmed.claimed_city, confirmed.observed_city, confirmed.resolved_city) == ("new_york", "new_york", "new_york")
        wrong = resolve([*databases, vendor_candidate(LocationTarget(country="US", state="CA", city="boston"))], SourcePolicy())  # type: ignore[list-item]
        assert wrong.state_conflict is True and wrong.city_conflict is True and not wrong.country_conflict
        # Nothing claimed at a level, or nothing independent known there: no verdict.
        country_only = resolve([*databases, vendor_candidate(LocationTarget(country="US"))], SourcePolicy())  # type: ignore[list-item]
        assert country_only.state_conflict is None and country_only.claimed_state is None and country_only.observed_state == "NY"
        unknown = resolve([db("a", "US"), vendor_candidate(LocationTarget(country="US", city="boston"))], SourcePolicy())  # type: ignore[list-item]
        assert unknown.city_conflict is None and unknown.resolved_city == "boston" and unknown.observed_city is None

    def test_sub_localities_are_their_city(self) -> None:
        # DB-IP labels districts "Sofia (g.k. Banishora)"; MaxMind says "Sofia". Same city: they agree, and confirm the claim.
        databases = [db("dbip", "BG", city="Sofia (g.k. Banishora)"), db("maxmind", "BG", city="Sofia")]
        result = resolve([*databases, vendor_candidate(LocationTarget(country="BG", city="sofia"))], SourcePolicy())  # type: ignore[list-item]
        assert result.city_conflict is False and result.observed_city == "sofia" and result.resolved_city == "sofia"

    def test_conflict_rule_applies_below_the_country(self) -> None:
        disagreeing = [db("a", "US", city="New York"), db("b", "US", city="Jersey City")]
        claim = vendor_candidate(LocationTarget(country="US", city="new_york"))
        consensus = resolve([*disagreeing, claim], SourcePolicy())  # type: ignore[list-item]
        assert consensus.city_conflict is None  # the databases disagree: the claim is left unjudged
        first = resolve([*disagreeing, claim], SourcePolicy(conflict_rule=ConflictRule.FIRST))  # type: ignore[list-item]
        assert first.city_conflict is False and first.observed_city == "new_york"
        reversed_first = resolve([disagreeing[1], disagreeing[0], claim], SourcePolicy(conflict_rule=ConflictRule.FIRST))  # type: ignore[list-item]
        assert reversed_first.city_conflict is True

    def test_vendor_fills_a_level_nothing_independent_knows(self) -> None:
        # No database knows the city: the vendor's city is what routing gets, with its source recorded.
        result = resolve([db("a", "US", state_code="NY"), vendor_candidate(LocationTarget(country="US", city="buffalo"))], SourcePolicy())  # type: ignore[list-item]
        assert result.resolved_city == "buffalo" and result.resolved_city_source == GeoSourceKind.VENDOR
        assert result.resolved_state == "NY" and result.resolved_state_source == GeoSourceKind.DATABASE
        assert result.observed_city is None and result.city_conflict is None

    def test_judge_level_directly(self) -> None:
        verdict = judge_level([db("a", "GB", city="London"), endpoint_candidate(LocationTarget(country="GB", city="london"))], SourcePolicy(), lambda c: c.city)  # type: ignore[list-item]
        assert verdict.observed == "london" and verdict.resolved == "london" and verdict.claimed is None and verdict.conflict is None


class TestObservedCountry:
    def test_observed_is_the_independent_answer(self) -> None:
        result = resolve([db("mm", "GB"), vendor_candidate(LocationTarget.reported("US"))], SourcePolicy())  # type: ignore[list-item]
        assert result.resolved_country == "GB" and result.observed_country == "GB" and result.country_conflict

    def test_observed_is_none_without_independent_evidence(self) -> None:
        result = resolve([vendor_candidate(LocationTarget.reported("US"))], SourcePolicy())  # type: ignore[list-item]
        assert result.resolved_country == "US" and result.observed_country is None and result.country_conflict is None
        assert resolve([], SourcePolicy()).observed_country is None


class TestOpenClaimRouting:
    """Disagreeing databases leave the claim open under consensus; the value never depends on the rule."""

    def _disagreeing(self) -> list[LocationCandidate]:
        return [db("mm", "GB"), db("dbip", "FR"), vendor_candidate(LocationTarget.reported("US"))]  # type: ignore[list-item]

    def test_consensus_leaves_the_claim_open_and_routes_on_the_top_database(self) -> None:
        result = resolve(self._disagreeing(), SourcePolicy())
        assert result.country_conflict is None
        assert result.resolved_country == "GB" and result.resolved_source == GeoSourceKind.DATABASE
        assert result.observed_country == "GB"

    def test_first_flags_on_the_same_evidence_it_routes_on(self) -> None:
        result = resolve(self._disagreeing(), SourcePolicy(conflict_rule=ConflictRule.FIRST))
        assert result.country_conflict is True and result.resolved_country == "GB"

    def test_disagreement_without_a_claim_keeps_the_top_answer(self) -> None:
        result = resolve([db("mm", "GB"), db("dbip", "FR")], SourcePolicy())
        assert result.country_conflict is None and result.resolved_country == "GB"

    def test_applies_below_the_country(self) -> None:
        candidates = [
            db("mm", "US", region="New York", state_code="NY", city="New York"),
            db("dbip", "US", region="New Jersey", state_code="NJ", city="Newark"),
            vendor_candidate(LocationTarget(country="US", state="NJ", city="newark")),
        ]
        result = resolve(candidates, SourcePolicy())  # type: ignore[arg-type]
        assert result.country_conflict is False and result.resolved_country == "US"
        assert result.state_conflict is None and result.resolved_state == "NY"
        assert result.city_conflict is None and result.resolved_city == "new_york"


# Points a GeoNames file would give these cities, for the claimed side.
PLACES = {
    ("BR", "sao_paulo"): (-23.5475, -46.63611),
    ("BR", "diadema"): (-23.68611, -46.62278),
    ("BR", "guarulhos"): (-23.46278, -46.53333),
    ("BR", "campinas"): (-22.90556, -47.06083),
    # GeoNames has London but no "camden": the populated place is "Camden Town".
    ("GB", "london"): (51.50853, -0.12574),
}


def locate(country: str | None, state: str | None, city: str | None) -> tuple[float, float] | None:
    return PLACES.get((country or "", city or ""))


def sp_db(origin: str, city: str, *, coordinates: bool = True) -> LocationCandidate:
    """A database answer in the state of São Paulo, at the city's own point unless ``coordinates`` is off."""
    point = PLACES[("BR", city.lower().replace(" ", "_"))] if coordinates else (None, None)
    return db(origin, "BR", city=city, latitude=point[0], longitude=point[1])


SAO_PAULO_CLAIM = LocationTarget(country="BR", city="sao_paulo")
LONDON_CLAIM = LocationTarget(country="GB", city="london")
CAMDEN = (51.5406, -0.14334)


def camden_db(origin: str) -> LocationCandidate:
    """DB-IP's answer for a Camden exit: the borough's name and its own coordinates."""
    return db(origin, "GB", city="Camden", latitude=CAMDEN[0], longitude=CAMDEN[1])


class TestCityRadius:
    """A city claim is confirmed by an answer within the radius of the claimed city, once a file places it."""

    def test_neighbouring_city_within_the_radius_confirms_the_claim(self) -> None:
        for rule in ConflictRule:
            result = resolve([sp_db("dbip", "Diadema"), vendor_candidate(SAO_PAULO_CLAIM)], SourcePolicy(conflict_rule=rule), locate)  # type: ignore[list-item]
            assert result.city_conflict is False, rule
            # What was observed and resolved stays the database's word; the distance says how far it was.
            assert result.observed_city == "diadema" and result.resolved_city == "diadema"
            assert result.city_distance_km == pytest.approx(15.5, abs=0.2)

    def test_beyond_the_radius_contradicts_it(self) -> None:
        result = resolve([sp_db("dbip", "Campinas"), vendor_candidate(SAO_PAULO_CLAIM)], SourcePolicy(), locate)  # type: ignore[list-item]
        assert result.city_conflict is True and result.city_distance_km == pytest.approx(83.5, abs=1.0)
        strict = resolve([sp_db("dbip", "Diadema"), vendor_candidate(SAO_PAULO_CLAIM)], SourcePolicy(city_radius_km=10), locate)  # type: ignore[list-item]
        assert strict.city_conflict is True

    def test_zero_radius_compares_names_only(self) -> None:
        result = resolve([sp_db("dbip", "Diadema"), vendor_candidate(SAO_PAULO_CLAIM)], SourcePolicy(city_radius_km=0), locate)  # type: ignore[list-item]
        assert result.city_conflict is True
        assert result.city_distance_km == pytest.approx(15.5, abs=0.2)  # still reported

    def test_without_a_places_file_names_decide(self) -> None:
        result = resolve([sp_db("dbip", "Diadema"), vendor_candidate(SAO_PAULO_CLAIM)], SourcePolicy())  # type: ignore[list-item]
        assert result.city_conflict is True and result.city_distance_km is None

    def test_claim_the_file_does_not_know_falls_back_to_names(self) -> None:
        claim = vendor_candidate(LocationTarget(country="BR", city="vila_olimpia"))
        result = resolve([sp_db("dbip", "Diadema"), claim], SourcePolicy(), locate)  # type: ignore[list-item]
        assert result.city_conflict is True and result.city_distance_km is None

    def test_only_the_city_level_is_lenient(self) -> None:
        claim = vendor_candidate(LocationTarget(country="BR", state="RJ", city="sao_paulo"))
        database = db("dbip", "BR", state_code="SP", city="Diadema", latitude=-23.68611, longitude=-46.62278)
        result = resolve([database, claim], SourcePolicy(), locate)  # type: ignore[list-item]
        assert result.city_conflict is False and result.state_conflict is True

    def test_consensus_confirms_when_every_answer_is_near_the_claim(self) -> None:
        # By name the two databases disagree and the claim would be open; both are within 25 km of the claimed city.
        candidates = [sp_db("dbip", "Diadema"), sp_db("maxmind", "Guarulhos"), vendor_candidate(SAO_PAULO_CLAIM)]
        assert resolve(candidates, SourcePolicy(), locate).city_conflict is False  # type: ignore[arg-type]
        assert resolve(candidates, SourcePolicy(city_radius_km=0), locate).city_conflict is None  # type: ignore[arg-type]

    def test_consensus_stays_open_when_only_some_answers_are_near(self) -> None:
        candidates = [sp_db("dbip", "Diadema"), sp_db("maxmind", "Campinas"), vendor_candidate(SAO_PAULO_CLAIM)]
        assert resolve(candidates, SourcePolicy(), locate).city_conflict is None  # type: ignore[arg-type]
        first = resolve(candidates, SourcePolicy(conflict_rule=ConflictRule.FIRST), locate)  # type: ignore[arg-type]
        assert first.city_conflict is False
        any_rule = resolve(candidates, SourcePolicy(conflict_rule=ConflictRule.ANY), locate)  # type: ignore[arg-type]
        assert any_rule.city_conflict is False and any_rule.resolved_city == "diadema"

    def test_any_routes_on_the_answer_that_confirmed_the_claim(self) -> None:
        candidates = [sp_db("dbip", "Campinas"), sp_db("maxmind", "Diadema"), vendor_candidate(SAO_PAULO_CLAIM)]
        result = resolve(candidates, SourcePolicy(conflict_rule=ConflictRule.ANY), locate)  # type: ignore[arg-type]
        assert result.city_conflict is False
        assert result.observed_city == "diadema" and result.resolved_city == "diadema"
        assert result.city_distance_km == pytest.approx(15.5, abs=0.2)

    def test_a_database_without_coordinates_is_placed_by_its_city_name(self) -> None:
        result = resolve([sp_db("ipinfo", "Diadema", coordinates=False), vendor_candidate(SAO_PAULO_CLAIM)], SourcePolicy(), locate)  # type: ignore[list-item]
        assert result.city_conflict is False

    def test_the_echo_endpoint_is_placed_by_its_city_name(self) -> None:
        echo = endpoint_candidate(LocationTarget(country="BR", city="diadema"))
        result = resolve([echo, vendor_candidate(SAO_PAULO_CLAIM)], SourcePolicy(), locate)  # type: ignore[list-item]
        assert result.city_conflict is False and result.city_distance_km == pytest.approx(15.5, abs=0.2)

    def test_an_echo_naming_the_database_city_shares_its_point(self) -> None:
        # DB-IP says Camden for a london claim and the echo, answering from the same databases, says camden.
        # The file cannot place "camden"; the echo is placed where DB-IP put Camden, 3.8 km from London.
        echo = endpoint_candidate(LocationTarget(country="GB", city="camden"))
        candidates = [camden_db("dbip"), echo, vendor_candidate(LONDON_CLAIM)]
        for rule in ConflictRule:
            result = resolve(candidates, SourcePolicy(conflict_rule=rule), locate)  # type: ignore[arg-type]
            assert result.city_conflict is False, rule
            assert result.city_distance_km == pytest.approx(3.8, abs=0.2)

    def test_an_echo_city_no_database_named_still_needs_the_file(self) -> None:
        # The databases said London; the echo's camden has no database point to share and the file does not know it.
        candidates = [db("dbip", "GB", city="London", latitude=51.5085, longitude=-0.1257),
                      endpoint_candidate(LocationTarget(country="GB", city="camden")), vendor_candidate(LONDON_CLAIM)]
        assert resolve(candidates, SourcePolicy(), locate).city_conflict is None  # type: ignore[arg-type]
        assert resolve(candidates, SourcePolicy(conflict_rule=ConflictRule.FIRST), locate).city_conflict is False  # type: ignore[arg-type]

    def test_a_claimed_city_one_database_named_is_placed_at_its_point(self) -> None:
        # A -city-camden claim: DB-IP says Camden, MaxMind says Islington, 2.8 km away. Neither name is in the file.
        islington = db("maxmind", "GB", city="Islington", latitude=51.5362, longitude=-0.1033)
        candidates = [camden_db("dbip"), islington, vendor_candidate(LocationTarget(country="GB", city="camden"))]
        result = resolve(candidates, SourcePolicy(), locate)  # type: ignore[arg-type]
        assert result.city_conflict is False and result.city_distance_km == 0
        # By name alone the two databases split and the claim stays open.
        assert resolve(candidates, SourcePolicy(city_radius_km=0), locate).city_conflict is None  # type: ignore[arg-type]

    def test_the_highest_priority_database_places_a_shared_name(self) -> None:
        elsewhere = db("other", "GB", city="Camden", latitude=52.48, longitude=-1.89)
        echo = endpoint_candidate(LocationTarget(country="GB", city="camden"))
        proximity = CityProximity(locate, 25, [camden_db("dbip"), elsewhere, echo])
        assert proximity.point(echo) == pytest.approx(CAMDEN)
        # Only the same country shares a point.
        abroad = endpoint_candidate(LocationTarget(country="US", city="camden"))
        assert CityProximity(locate, 25, [camden_db("dbip"), abroad]).point(abroad) is None

    def test_judge_level_takes_any_agreement(self) -> None:
        candidates = [db("a", "GB", city="Croydon"), vendor_candidate(LocationTarget(country="GB", city="london"))]
        verdict = judge_level(candidates, SourcePolicy(), lambda c: c.city, lambda a, b: True)  # type: ignore[arg-type]
        assert verdict.conflict is False and verdict.observed == "croydon" and verdict.claimed == "london"
