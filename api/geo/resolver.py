# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Combine what the databases, the vendor and an echo endpoint say about one IP.

Pure functions: no I/O, no clock. The service layer gathers the candidates
and persists the result; this module only decides.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
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
from api.geo.places import CityLocator, Point, distance_km
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
    disagreed among themselves under ``consensus``. ``observed_by`` and
    ``claimed_by`` are the candidates behind ``observed`` and ``claimed``.
    """

    resolved: str | None = None
    source: GeoSourceKind | None = None
    origin: str | None = None
    observed: str | None = None
    claimed: str | None = None
    conflict: bool | None = None
    disagreement: bool = False
    observed_by: LocationCandidate | None = None
    claimed_by: LocationCandidate | None = None


# Whether two candidates' answers at one level name the same place.
Agreement = Callable[[LocationCandidate, LocationCandidate], bool]


class CityProximity:
    """Whether two answers at the city level name the same place: the same slug, or points within the radius.

    A database answer is placed at its own coordinates. Any other answer
    (the claim, the echo endpoint's city, a database without coordinates)
    is placed where a database answering for the same IP put a city of that
    name, since every answer here is about one exit: the echo's ``camden``
    is DB-IP's Camden. Failing that, it is placed at the point ``locate``
    finds for its name, which is a loaded GeoNames file. Without ``locate``
    (no file loaded) only a database's own coordinates place anything, so
    the claim never is and cities agree by name alone, as they do with a
    radius of 0. Points are looked up once per candidate.
    """

    def __init__(
        self, locate: CityLocator | None, radius_km: float, candidates: Iterable[LocationCandidate] = ()
    ) -> None:
        self._locate = locate
        self._radius_km = radius_km
        self._points: dict[int, Point | None] = {}
        # Where the databases put each city they named for this IP, the highest-priority database first.
        self._named: dict[tuple[str | None, str | None], Point] = {}
        for candidate in candidates:
            own = self._own_point(candidate)
            if own is not None:
                self._named.setdefault((candidate.country, candidate.city), own)

    @property
    def active(self) -> bool:
        """Whether a distance can decide anything: a radius, and a way to place a named city."""
        return self._locate is not None and self._radius_km > 0

    def point(self, candidate: LocationCandidate) -> Point | None:
        key = id(candidate)
        if key not in self._points:
            self._points[key] = self._place(candidate)
        return self._points[key]

    @staticmethod
    def _own_point(candidate: LocationCandidate) -> Point | None:
        """A database answer's own coordinates for the city it named."""
        location = candidate.location
        if candidate.source != GeoSourceKind.DATABASE or location is None or not candidate.city:
            return None
        if location.latitude is None or location.longitude is None:
            return None
        return location.latitude, location.longitude

    def _place(self, candidate: LocationCandidate) -> Point | None:
        if candidate.location is None or not candidate.city:
            return None
        own = self._own_point(candidate)
        if own is not None or self._locate is None:
            return own
        named = self._named.get((candidate.country, candidate.city))
        if named is not None:
            return named
        return self._locate(candidate.country, candidate.state, candidate.city)

    def _km(self, a: LocationCandidate, b: LocationCandidate) -> float | None:
        pa, pb = self.point(a), self.point(b)
        return distance_km(pa, pb) if pa is not None and pb is not None else None

    def distance(self, a: LocationCandidate | None, b: LocationCandidate | None) -> float | None:
        """Kilometres between two answers, to 0.1 km for display, or None when either cannot be placed."""
        if a is None or b is None:
            return None
        found = self._km(a, b)
        return round(found, 1) if found is not None else None

    def same(self, a: LocationCandidate, b: LocationCandidate) -> bool:
        if a.city == b.city:
            return True
        # The exact distance: the rounded one would let an answer just past the radius confirm.
        found = self._km(a, b)
        return found is not None and found <= self._radius_km


def judge_level(
    candidates: list[LocationCandidate],
    policy: SourcePolicy,
    value_of: Callable[[LocationCandidate], str | None],
    agree: Agreement | None = None,
) -> LevelVerdict:
    """Resolve one level (country, state or city) of an IP's place.

    The winner is the first candidate, in ``VALUE_PRECEDENCE`` order (the
    databases, then the echo endpoint, then the vendor), with an answer at
    this level. The claim is the vendor's answer. The observed value is the
    first independent answer, so it can never be the claim. Two answers
    agree when ``agree`` says so, by default when they are the same value;
    the city level passes :meth:`CityProximity.same` so a neighbouring city
    within the radius agrees too. ``conflict`` follows ``policy.conflict_rule``:

    * ``consensus``: every independent answer agreeing with the claim
      confirms it; none agreeing, while they agree with each other,
      contradicts it. Independent sources that disagree among themselves
      otherwise set ``disagreement`` and leave the claim unjudged.
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
    same = agree or (lambda a, b: value_of(a) == value_of(b))

    winner = ordered[0] if ordered else None
    claim = next((c for c in ordered if c.source == GeoSourceKind.VENDOR), None)
    independent = [c for c in ordered if c.source != GeoSourceKind.VENDOR]
    observed = independent[0] if independent else None
    disagreement = observed is not None and not all(same(observed, c) for c in independent[1:])

    conflict: bool | None = None
    if claim is not None and observed is not None:
        matches = [same(c, claim) for c in independent]
        if policy.conflict_rule == ConflictRule.CONSENSUS:
            if all(matches):
                conflict = False
            elif not any(matches) and not disagreement:
                conflict = True
        elif policy.conflict_rule == ConflictRule.ANY:
            conflict = not any(matches)
            if not conflict:
                # The agreeing source is both what routing uses and what was observed:
                # a confirmed claim must never report an observation that contradicts it.
                winner = observed = next(c for c, match in zip(independent, matches, strict=True) if match)
        else:
            conflict = not matches[0]
    return LevelVerdict(
        resolved=value_of(winner) if winner else None,
        source=winner.source if winner else None,
        origin=winner.origin if winner else None,
        observed=value_of(observed) if observed else None,
        claimed=value_of(claim) if claim else None,
        conflict=conflict,
        disagreement=disagreement,
        observed_by=observed,
        claimed_by=claim,
    )


def resolve(candidates: list[LocationCandidate], policy: SourcePolicy, locate: CityLocator | None = None) -> Resolution:
    """Resolve an IP's country, state and city and judge the claims at each level.

    Every level runs the same :func:`judge_level`; a claim below the country
    (a vendor-listed city, a ``-st-`` or ``-city-`` request, a hand-set pin)
    is judged exactly as the vendor's country is, and every level reports
    one nullable verdict, which is how the observation tables store them.
    The city level alone also agrees by distance: ``locate`` places a named
    city (the loaded GeoNames files; None when none is loaded), and an
    answer within ``policy.city_radius_km`` of the claimed city agrees with
    it (see :class:`CityProximity`).
    """
    country = judge_level(candidates, policy, lambda c: c.country)
    state = judge_level(candidates, policy, lambda c: c.state)
    proximity = CityProximity(locate, policy.city_radius_km, candidates)
    city = judge_level(candidates, policy, lambda c: c.city, proximity.same if proximity.active else None)
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
        city_distance_km=proximity.distance(city.observed_by, city.claimed_by),
    )
