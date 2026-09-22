# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Combine what the databases, the vendor and an echo endpoint say about one IP.

Pure functions: no I/O, no clock. The service layer gathers the candidates
and persists the result; this module only decides.
"""

from __future__ import annotations

from api.geo.models import (
    ConflictRule,
    GeoSourceKind,
    IpLocation,
    LocationCandidate,
    Resolution,
    SourcePolicy,
    normalize_country,
)

VENDOR_ORIGIN = "vendor"
ENDPOINT_ORIGIN = "endpoint"


def vendor_candidate(country: str | None) -> LocationCandidate | None:
    """The vendor's claim as a candidate, or None when it made none."""
    code = normalize_country(country)
    if code is None:
        return None
    return LocationCandidate(source=GeoSourceKind.VENDOR, origin=VENDOR_ORIGIN, country=code)


def endpoint_candidate(country: str | None) -> LocationCandidate | None:
    """What the endpoint requested through the proxy reported, or None when it gave no country.

    Discovery and echo endpoints both land here; which one answered is not
    kept, only that an independent endpoint said so.
    """
    code = normalize_country(country)
    if code is None:
        return None
    return LocationCandidate(source=GeoSourceKind.ENDPOINT, origin=ENDPOINT_ORIGIN, country=code)


def _merged_location(candidates: list[LocationCandidate]) -> IpLocation | None:
    """Fold database records together, highest priority first, filling unknown fields."""
    merged: IpLocation | None = None
    for candidate in candidates:
        if candidate.source != GeoSourceKind.DATABASE or candidate.location is None:
            continue
        merged = candidate.location if merged is None else merged.merged_with(candidate.location)
    return merged


def resolve(candidates: list[LocationCandidate], policy: SourcePolicy) -> Resolution:
    """Pick the country for an IP and judge the vendor's claim.

    The winner is the first candidate, in ``policy.sources`` order, with a
    country. ``conflict`` follows ``policy.conflict_rule``:

    * ``consensus``: every independent source (database, endpoint) agrees and
      all of them disagree with the vendor. Independent sources that disagree
      among themselves set ``disagreement`` instead and never flag the vendor.
    * ``first``: the highest-precedence independent answer disagrees with the vendor.

    A missing vendor claim never conflicts: there is nothing to contradict.
    """
    ordered: list[LocationCandidate] = []
    for kind in policy.sources:
        ordered.extend(c for c in candidates if c.source == kind)
    # Sources the policy left out still count as evidence about the vendor, so
    # keep them for the conflict judgement, after the ones the policy ranks.
    ranked_ids = {id(c) for c in ordered}
    evidence = ordered + [c for c in candidates if id(c) not in ranked_ids]

    winner = next((c for c in ordered if c.country), None)
    claimed = next((c.country for c in candidates if c.source == GeoSourceKind.VENDOR and c.country), None)

    independent = [c for c in evidence if c.source != GeoSourceKind.VENDOR and c.country]
    independent_countries = {c.country for c in independent}
    disagreement = len(independent_countries) > 1

    conflict = False
    if claimed is not None and independent:
        if policy.conflict_rule == ConflictRule.CONSENSUS:
            conflict = not disagreement and claimed not in independent_countries
        else:
            first_independent = next((c for c in ordered if c.source != GeoSourceKind.VENDOR and c.country), independent[0])
            conflict = first_independent.country != claimed

    return Resolution(
        country=winner.country if winner else None,
        source=winner.source if winner else None,
        origin=winner.origin if winner else None,
        conflict=conflict,
        disagreement=disagreement,
        claimed_country=claimed,
        location=_merged_location(candidates),
        candidates=list(candidates),
    )
