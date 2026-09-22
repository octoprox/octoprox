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
from api.geo.resolver import endpoint_candidate, resolve, vendor_candidate


def db(origin: str, country: str | None, **location: object) -> LocationCandidate:
    loc = IpLocation(country=country, **location) if country or location else None  # type: ignore[arg-type]
    return LocationCandidate(source=GeoSourceKind.DATABASE, origin=origin, country=country, location=loc)


class TestWinner:
    def test_database_wins_by_default(self) -> None:
        result = resolve([db("mm", "GB"), vendor_candidate("US"), endpoint_candidate("US")], SourcePolicy())  # type: ignore[list-item]
        assert result.country == "GB" and result.source == GeoSourceKind.DATABASE and result.origin == "mm"

    def test_source_order_is_policy(self) -> None:
        policy = SourcePolicy(sources=[GeoSourceKind.VENDOR, GeoSourceKind.DATABASE])
        result = resolve([db("mm", "GB"), vendor_candidate("US")], policy)  # type: ignore[list-item]
        assert result.country == "US" and result.source == GeoSourceKind.VENDOR

    def test_falls_through_when_first_source_has_no_answer(self) -> None:
        result = resolve([db("mm", None), vendor_candidate("US")], SourcePolicy())  # type: ignore[list-item]
        assert result.country == "US" and result.source == GeoSourceKind.VENDOR

    def test_excluded_source_never_wins(self) -> None:
        policy = SourcePolicy(sources=[GeoSourceKind.DATABASE])
        result = resolve([vendor_candidate("US")], policy)  # type: ignore[list-item]
        assert result.country is None and result.source is None

    def test_database_priority_order_is_input_order(self) -> None:
        result = resolve([db("first", "GB"), db("second", "FR")], SourcePolicy())
        assert result.origin == "first"

    def test_no_candidates(self) -> None:
        result = resolve([], SourcePolicy())
        assert result.country is None and not result.conflict and not result.disagreement


class TestConflict:
    def test_no_claim_no_conflict(self) -> None:
        result = resolve([db("mm", "GB"), endpoint_candidate("FR")], SourcePolicy())  # type: ignore[list-item]
        assert not result.conflict and result.disagreement

    def test_consensus_flags_vendor_when_all_independent_agree(self) -> None:
        result = resolve([db("mm", "GB"), db("dbip", "GB"), vendor_candidate("US")], SourcePolicy())  # type: ignore[list-item]
        assert result.conflict and not result.disagreement and result.claimed_country == "US"

    def test_consensus_does_not_flag_when_databases_disagree(self) -> None:
        result = resolve([db("mm", "GB"), db("dbip", "FR"), vendor_candidate("US")], SourcePolicy())  # type: ignore[list-item]
        assert not result.conflict and result.disagreement

    def test_consensus_no_conflict_when_one_independent_agrees_with_vendor(self) -> None:
        result = resolve([db("mm", "US"), endpoint_candidate("GB"), vendor_candidate("US")], SourcePolicy())  # type: ignore[list-item]
        assert not result.conflict

    def test_first_rule_flags_on_top_ranked_disagreement(self) -> None:
        policy = SourcePolicy(conflict_rule=ConflictRule.FIRST)
        result = resolve([db("mm", "GB"), db("dbip", "US"), vendor_candidate("US")], policy)  # type: ignore[list-item]
        assert result.conflict

    def test_first_rule_uses_independent_source_even_when_vendor_ranks_first(self) -> None:
        policy = SourcePolicy(conflict_rule=ConflictRule.FIRST, sources=[GeoSourceKind.VENDOR, GeoSourceKind.DATABASE])
        result = resolve([db("mm", "GB"), vendor_candidate("US")], policy)  # type: ignore[list-item]
        assert result.country == "US" and result.conflict

    def test_agreeing_vendor_is_not_flagged(self) -> None:
        result = resolve([db("mm", "US"), vendor_candidate("us")], SourcePolicy())  # type: ignore[list-item]
        assert not result.conflict and result.claimed_country == "US"

    def test_excluded_sources_still_count_as_evidence(self) -> None:
        policy = SourcePolicy(sources=[GeoSourceKind.VENDOR])
        result = resolve([db("mm", "GB"), vendor_candidate("US")], policy)  # type: ignore[list-item]
        assert result.country == "US" and result.conflict


class TestLocation:
    def test_merges_database_records_in_priority_order(self) -> None:
        result = resolve(
            [db("city", "GB", city="London"), db("asn", None, asn=12345, organization="Example")],
            SourcePolicy(),
        )
        assert result.location is not None
        assert result.location.city == "London" and result.location.asn == 12345

    def test_compact_candidates(self) -> None:
        result = resolve([db("mm", "GB"), vendor_candidate("US")], SourcePolicy())  # type: ignore[list-item]
        assert result.compact_candidates() == [
            {"source": "database", "origin": "mm", "country": "GB"},
            {"source": "vendor", "origin": "vendor", "country": "US"},
        ]


class TestCandidates:
    def test_vendor_candidate_normalises(self) -> None:
        assert vendor_candidate(" gb ") is not None and vendor_candidate(" gb ").country == "GB"  # type: ignore[union-attr]
        assert vendor_candidate("") is None and vendor_candidate(None) is None and vendor_candidate("USA") is None

    def test_endpoint_candidate(self) -> None:
        candidate = endpoint_candidate("de")
        assert candidate is not None and candidate.origin == "endpoint" and candidate.country == "DE"
        assert endpoint_candidate(None) is None
