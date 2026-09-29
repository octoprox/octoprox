# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the source policy: who wins, and when the vendor is contradicted."""

from api.geo.models import (
    ConflictRule,
    GeoSourceKind,
    IpLocation,
    LocationCandidate,
    SourcePolicy,
)
from api.geo.resolver import endpoint_candidate, judge_level, resolve, vendor_candidate
from api.models.location import LocationTarget


def db(origin: str, country: str | None, **location: object) -> LocationCandidate:
    loc = IpLocation(country=country, **location) if country or location else None  # type: ignore[arg-type]
    return LocationCandidate(source=GeoSourceKind.DATABASE, origin=origin, country=country, location=loc)


class TestWinner:
    def test_database_wins_by_default(self) -> None:
        result = resolve([db("mm", "GB"), vendor_candidate(LocationTarget.reported("US")), endpoint_candidate(LocationTarget.reported("US"))], SourcePolicy())  # type: ignore[list-item]
        assert result.resolved_country == "GB" and result.resolved_source == GeoSourceKind.DATABASE and result.resolved_origin == "mm"

    def test_source_order_is_policy(self) -> None:
        policy = SourcePolicy(sources=[GeoSourceKind.VENDOR, GeoSourceKind.DATABASE])
        result = resolve([db("mm", "GB"), vendor_candidate(LocationTarget.reported("US"))], policy)  # type: ignore[list-item]
        assert result.resolved_country == "US" and result.resolved_source == GeoSourceKind.VENDOR

    def test_falls_through_when_first_source_has_no_answer(self) -> None:
        result = resolve([db("mm", None), vendor_candidate(LocationTarget.reported("US"))], SourcePolicy())  # type: ignore[list-item]
        assert result.resolved_country == "US" and result.resolved_source == GeoSourceKind.VENDOR

    def test_excluded_source_never_wins(self) -> None:
        policy = SourcePolicy(sources=[GeoSourceKind.DATABASE])
        result = resolve([vendor_candidate(LocationTarget.reported("US"))], policy)  # type: ignore[list-item]
        assert result.resolved_country is None and result.resolved_source is None

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

    def test_first_rule_uses_independent_source_even_when_vendor_ranks_first(self) -> None:
        policy = SourcePolicy(conflict_rule=ConflictRule.FIRST, sources=[GeoSourceKind.VENDOR, GeoSourceKind.DATABASE])
        result = resolve([db("mm", "GB"), vendor_candidate(LocationTarget.reported("US"))], policy)  # type: ignore[list-item]
        assert result.resolved_country == "US" and result.country_conflict

    def test_agreeing_vendor_is_not_flagged(self) -> None:
        result = resolve([db("mm", "US"), vendor_candidate(LocationTarget.reported("us"))], SourcePolicy())  # type: ignore[list-item]
        assert result.country_conflict is False and result.claimed_country == "US"

    def test_excluded_sources_still_count_as_evidence(self) -> None:
        policy = SourcePolicy(sources=[GeoSourceKind.VENDOR])
        result = resolve([db("mm", "GB"), vendor_candidate(LocationTarget.reported("US"))], policy)  # type: ignore[list-item]
        assert result.resolved_country == "US" and result.country_conflict


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

    def test_policy_may_let_the_claim_win_routing(self) -> None:
        # No database knows the city: under the default policy the vendor's city is what routing gets.
        result = resolve([db("a", "US", state_code="NY"), vendor_candidate(LocationTarget(country="US", city="buffalo"))], SourcePolicy())  # type: ignore[list-item]
        assert result.resolved_city == "buffalo" and result.resolved_city_source == GeoSourceKind.VENDOR
        assert result.resolved_state == "NY" and result.resolved_state_source == GeoSourceKind.DATABASE
        assert result.observed_city is None and result.city_conflict is None
        # A policy without the vendor never routes on its word.
        strict = resolve(
            [db("a", "US", state_code="NY"), vendor_candidate(LocationTarget(country="US", city="buffalo"))],  # type: ignore[list-item]
            SourcePolicy(sources=[GeoSourceKind.DATABASE, GeoSourceKind.ENDPOINT]),
        )
        assert strict.resolved_city is None

    def test_judge_level_directly(self) -> None:
        verdict = judge_level([db("a", "GB", city="London"), endpoint_candidate(LocationTarget(country="GB", city="london"))], SourcePolicy(), lambda c: c.city)  # type: ignore[list-item]
        assert verdict.observed == "london" and verdict.resolved == "london" and verdict.claimed is None and verdict.conflict is None


class TestObservedCountry:
    def test_observed_is_the_independent_answer_even_when_the_vendor_wins(self) -> None:
        policy = SourcePolicy(sources=[GeoSourceKind.VENDOR, GeoSourceKind.DATABASE])
        result = resolve([db("mm", "GB"), vendor_candidate(LocationTarget.reported("US"))], policy)  # type: ignore[list-item]
        assert result.resolved_country == "US" and result.observed_country == "GB" and result.country_conflict

    def test_observed_is_none_without_independent_evidence(self) -> None:
        result = resolve([vendor_candidate(LocationTarget.reported("US"))], SourcePolicy())  # type: ignore[list-item]
        assert result.resolved_country == "US" and result.observed_country is None and result.country_conflict is None
        assert resolve([], SourcePolicy()).observed_country is None
