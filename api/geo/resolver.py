# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Combine what the databases, the vendor and an echo endpoint say about one IP.

Pure functions: no I/O, no clock. The service layer gathers the candidates
and persists the result; this module only decides.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from api.geo.models import (
    ConflictRule,
    GeoSourceKind,
    IpLocation,
    LocationCandidate,
    Resolution,
    SourcePolicy,
)
from api.models.location import LocationTarget

VENDOR_ORIGIN = "vendor"
ENDPOINT_ORIGIN = "endpoint"


def _place_candidate(kind: GeoSourceKind, origin: str, place: LocationTarget | None) -> LocationCandidate | None:
    if not place:
        return None
    return LocationCandidate(
        source=kind,
        origin=origin,
        country=place.country,
        location=IpLocation(country=place.country, state_code=place.state, city=place.city),
    )


def vendor_candidate(place: LocationTarget | None) -> LocationCandidate | None:
    """The vendor's claim as a candidate, or None when it made none.

    The claim is whatever was promised about the exit: the country of a listed
    IP or a geo-targeted slot, a city the vendor's list names, or the place a
    request asked for below the country.
    """
    return _place_candidate(GeoSourceKind.VENDOR, VENDOR_ORIGIN, place)


def endpoint_candidate(place: LocationTarget | None) -> LocationCandidate | None:
    """What the endpoint requested through the proxy reported, or None when it gave nothing.

    Discovery and echo endpoints both land here; which one answered is not
    kept, only that an independent endpoint said so.
    """
    return _place_candidate(GeoSourceKind.ENDPOINT, ENDPOINT_ORIGIN, place)


def _merged_location(candidates: list[LocationCandidate]) -> IpLocation | None:
    """Fold database records together, highest priority first, filling unknown fields."""
    merged: IpLocation | None = None
    for candidate in candidates:
        if candidate.source != GeoSourceKind.DATABASE or candidate.location is None:
            continue
        merged = candidate.location if merged is None else merged.merged_with(candidate.location)
    return merged


@dataclass(frozen=True)
class LevelVerdict:
    """One level's resolution: what won, what independent evidence saw, and how the claim fared.

    ``conflict`` is None when there is no verdict: nothing was claimed,
    nothing independent answered at this level, or the independent sources
    disagreed among themselves under ``consensus``.
    """

    resolved: str | None = None
    source: GeoSourceKind | None = None
    origin: str | None = None
    observed: str | None = None
    claimed: str | None = None
    conflict: bool | None = None
    disagreement: bool = False


def judge_level(
    candidates: list[LocationCandidate],
    policy: SourcePolicy,
    value_of: Callable[[LocationCandidate], str | None],
) -> LevelVerdict:
    """Resolve one level (country, state or city) of an IP's place.

    The winner is the first candidate, in ``policy.sources`` order, with an
    answer at this level. The claim is the vendor's answer. The observed
    value is the first independent answer in that order, so it can never be
    the claim. ``conflict`` follows ``policy.conflict_rule``:

    * ``consensus``: every independent source agrees and all of them
      disagree with the claim. Independent sources that disagree among
      themselves set ``disagreement`` and leave the claim unjudged.
    * ``first``: the highest-precedence independent answer disagrees with the claim.
    """
    ordered: list[LocationCandidate] = []
    for kind in policy.sources:
        ordered.extend(c for c in candidates if c.source == kind and value_of(c))
    # Sources the policy left out still count as evidence about the claim, so
    # keep them for the verdict, after the ones the policy ranks.
    ranked_ids = {id(c) for c in ordered}
    evidence = ordered + [c for c in candidates if id(c) not in ranked_ids and value_of(c)]

    winner = ordered[0] if ordered else None
    claimed = next((value_of(c) for c in evidence if c.source == GeoSourceKind.VENDOR), None)
    independent = [c for c in evidence if c.source != GeoSourceKind.VENDOR]
    values = {value_of(c) for c in independent}
    disagreement = len(values) > 1
    observed = next((c for c in ordered if c.source != GeoSourceKind.VENDOR), None)
    if observed is None and independent:
        observed = independent[0]

    conflict: bool | None = None
    if claimed is not None and independent:
        if policy.conflict_rule == ConflictRule.CONSENSUS:
            conflict = None if disagreement else claimed not in values
        else:
            conflict = value_of(observed) != claimed if observed else None
    return LevelVerdict(
        resolved=value_of(winner) if winner else None,
        source=winner.source if winner else None,
        origin=winner.origin if winner else None,
        observed=value_of(observed) if observed else None,
        claimed=claimed,
        conflict=conflict,
        disagreement=disagreement,
    )


def resolve(candidates: list[LocationCandidate], policy: SourcePolicy) -> Resolution:
    """Resolve an IP's country, state and city and judge the claims at each level.

    Every level runs the same :func:`judge_level`; a claim below the country
    (a vendor-listed city, a ``-st-`` or ``-city-`` request, a hand-set pin)
    is judged exactly as the vendor's country is, and every level reports
    one nullable verdict, which is how the observation tables store them.
    """
    country = judge_level(candidates, policy, lambda c: c.country)
    state = judge_level(candidates, policy, lambda c: c.state)
    city = judge_level(candidates, policy, lambda c: c.city)
    return Resolution(
        resolved_country=country.resolved,
        resolved_source=country.source,
        resolved_origin=country.origin,
        observed_country=country.observed,
        country_conflict=country.conflict,
        claimed_country=country.claimed,
        location=_merged_location(candidates),
        candidates=list(candidates),
        claimed_state=state.claimed,
        observed_state=state.observed,
        resolved_state=state.resolved,
        resolved_state_source=state.source,
        state_conflict=state.conflict,
        claimed_city=city.claimed,
        observed_city=city.observed,
        resolved_city=city.resolved,
        resolved_city_source=city.source,
        city_conflict=city.conflict,
    )
