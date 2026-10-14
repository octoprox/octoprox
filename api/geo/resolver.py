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
    VALUE_PRECEDENCE,
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

    The winner is the first candidate, in ``VALUE_PRECEDENCE`` order (the
    databases, then the echo endpoint, then the vendor), with an answer at
    this level. The claim is the vendor's answer. The observed value is the
    first independent answer, so it can never be the claim. ``conflict``
    follows ``policy.conflict_rule``:

    * ``consensus``: every independent source agrees and all of them
      disagree with the claim. Independent sources that disagree among
      themselves set ``disagreement`` and leave the claim unjudged.
    * ``first``: the highest-precedence independent answer disagrees with the claim.
    * ``any``: no independent answer agrees with the claim; one agreeing
      confirms it, and the value is then that agreeing answer, so a confirmed
      claim is never routed on a country its own verdict disagrees with.

    Otherwise the rule decides the verdict only, never the winner: when the
    databases disagree with each other the top-ranked one decides the value,
    and ``consensus`` merely leaves the claim open instead of flagging it. A
    project that wants no routing on unsettled evidence refuses open claims
    (``UnknownAction.REFUSE``). A contradicted claim carries the independent
    answer here, always: a project that routes contradictions on the vendor
    (``ContradictedAction.VENDOR``) applies that choice when it writes the
    row, so a resolution never reports a claim as what was resolved.
    """
    ordered: list[LocationCandidate] = []
    for kind in VALUE_PRECEDENCE:
        ordered.extend(c for c in candidates if c.source == kind and value_of(c))

    winner = ordered[0] if ordered else None
    claimed = next((value_of(c) for c in ordered if c.source == GeoSourceKind.VENDOR), None)
    independent = [c for c in ordered if c.source != GeoSourceKind.VENDOR]
    values = {value_of(c) for c in independent}
    disagreement = len(values) > 1
    observed = independent[0] if independent else None

    conflict: bool | None = None
    if claimed is not None and independent:
        if policy.conflict_rule == ConflictRule.CONSENSUS:
            conflict = None if disagreement else claimed not in values
        elif policy.conflict_rule == ConflictRule.ANY:
            conflict = claimed not in values
            if not conflict:
                # The agreeing source is both what routing uses and what was observed:
                # a confirmed claim must never report an observation that contradicts it.
                winner = observed = next(c for c in independent if value_of(c) == claimed)
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
