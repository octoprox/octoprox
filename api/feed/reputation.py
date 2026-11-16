# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The exit reputation section of the routing feed: what the controller reads from Postgres for the projects whose mode
avoids, and how it is cut to fit every gateway.

Per project, the avoids of the exit IPs its rows with IPs were last seen exiting from, and the factors of its dynamic
connectors. Read for one project when a reputation entry names it, for every project at a full read, and for a project
whose scope a compile moved; never on every compile, and never for the api's previews.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

import structlog

from api.core import utc_now
from api.db.reputation_repository import ExitReputationRepository
from api.feed.models import ConnectorSection, ProjectReputation, ReputationSection, static_exit_ip
from api.providers.sdk.strategies import is_dynamic_gateway
from api.reputation.record import place_levels

if TYPE_CHECKING:
    from api.db.session import SessionFactory
    from api.models.project import Project

logger = structlog.get_logger()

# The most exit IP and site avoids, and dynamic connector factors, the section carries for the whole install: every
# gateway decodes it on each change. Past a cap the avoids ending last are kept, and the factors of the coarsest levels,
# lowest first.
MAX_FEED_IP_AVOIDS = 100_000
MAX_FEED_FACTORS = 20_000

# An exit IP's avoid as the section carries it: (end, project, IP, site).
type _Avoid = tuple[datetime, str, str, str]
# A connector's factor as the section carries it: (levels of its place, factor, project, connector, site, place).
type _Factor = tuple[int, float, str, str, str, str]


class ReputationScope(NamedTuple):
    """What one avoiding project's exit reputation is read for: the exit IPs its rows with IPs were last seen exiting
    from, and its dynamic connectors."""

    ips: frozenset[str] = frozenset()
    connectors: frozenset[str] = frozenset()

    @classmethod
    def of(cls, projects: Iterable[Project], sections: Iterable[ConnectorSection]) -> dict[str, ReputationScope]:
        """Each project whose mode avoids, by id, with what a compile's connector sections give it to read for: every
        row of a section counts whatever its status, pooled slots by their last sighting, and a connector with a dynamic
        gateway row is dynamic."""
        avoiding = {project.id for project in projects if project.exit_reputation.avoids}
        ips: dict[str, set[str]] = {project_id: set() for project_id in avoiding}
        connectors: dict[str, set[str]] = {project_id: set() for project_id in avoiding}
        for section in sections:
            project_id = section.connector.project_id
            if project_id not in avoiding:
                continue
            for proxy in section.proxies:
                if is_dynamic_gateway(proxy):
                    connectors[project_id].add(section.connector.id)
                elif (ip := static_exit_ip(proxy)) is not None:
                    ips[project_id].add(ip)
        return {
            project_id: cls(frozenset(ips[project_id]), frozenset(connectors[project_id]))
            for project_id in sorted(avoiding)
        }


class _Read(NamedTuple):
    """One project's records as last read: exit IP -> site -> the end of its avoid, and dynamic connector -> site ->
    level of the requested place -> its stored factor."""

    ip_avoids: dict[str, dict[str, datetime]]
    factors: dict[str, dict[str, dict[str, float]]]


class FeedReputation:
    """The controller's exit reputation for the feed: the scope of the last compile, the records last read for it, and
    the section they make.

    Postgres is read only when asked (:meth:`read`); a compile adopts its scope without a query, and the section is
    built from what was last read.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._scopes: dict[str, ReputationScope] = {}
        self._reads: dict[str, _Read] = {}
        # The section the reads make, the same object while they make the same one.
        self._section = ReputationSection()
        # Whether the last section built was cut to the caps: a cut is logged when it starts.
        self._cut = False

    def section(self) -> ReputationSection:
        """The section of the last reads, as the scopes of the last compile hold them."""
        return self._section

    def adopt(self, scopes: Mapping[str, ReputationScope]) -> frozenset[str]:
        """Take a compile's scopes, by project; the projects whose scope is new or moved, each owing a read.

        A project that no longer avoids leaves the section at once with its read, and the IPs and connectors a scope
        lost leave with it; nothing is read. A compile that moved no scope leaves the section as it is, unbuilt: at its
        caps a build takes a few hundred milliseconds, and most compiles move no exit.
        """
        moved = frozenset(project_id for project_id, scope in scopes.items() if self._scopes.get(project_id) != scope)
        if not moved and scopes.keys() == self._scopes.keys():
            return moved
        self._scopes = dict(scopes)
        self._reads = {project_id: read for project_id, read in self._reads.items() if project_id in scopes}
        self._build()
        return moved

    async def read(self, project_ids: Collection[str] | None = None) -> None:
        """Read again the records of ``project_ids`` (None: every project in scope) from Postgres, in one session, and
        build the section anew.

        Raises when Postgres cannot be read, keeping the last read; a project with nothing to read for costs no query.
        """
        wanted = sorted(
            project_id
            for project_id in (self._scopes if project_ids is None else project_ids)
            if project_id in self._scopes
        )
        exits = {project_id: self._scopes[project_id].ips for project_id in wanted if self._scopes[project_id].ips}
        connectors = sorted({connector for project_id in wanted for connector in self._scopes[project_id].connectors})
        reads = {project_id: _Read({}, {}) for project_id in wanted}
        if exits or connectors:
            now = utc_now()
            async with self._session_factory() as session:
                repository = ExitReputationRepository(session)
                # One past the cap per project, so a section cut to it knows it was.
                avoids = await repository.ip_avoids(exits, now, per_project=MAX_FEED_IP_AVOIDS + 1) if exits else []
                factors = await repository.connector_factors(connectors) if connectors else []
            for avoid in avoids:
                end = avoid.avoided_until(now)
                if end is not None:
                    reads[avoid.project_id].ip_avoids.setdefault(avoid.ip, {})[avoid.site] = end
            for record in factors:
                read = reads.get(record.project_id)
                if read is not None and record.factor is not None:
                    read.factors.setdefault(record.connector_id, {}).setdefault(record.site, {})[record.place] = (
                        record.factor
                    )
        self._reads.update(reads)
        self._build()

    def _build(self) -> None:
        """Make the section from the reads under the scopes, cut to the caps; the object in hand when nothing moved."""
        avoids: list[_Avoid] = []
        factors: list[_Factor] = []
        for project_id, scope in sorted(self._scopes.items()):
            read = self._reads.get(project_id)
            if read is None:
                continue
            for ip in sorted(read.ip_avoids.keys() & scope.ips):
                avoids.extend((end, project_id, ip, site) for site, end in sorted(read.ip_avoids[ip].items()))
            for connector_id in sorted(read.factors.keys() & scope.connectors):
                for site, by_place in sorted(read.factors[connector_id].items()):
                    factors.extend(
                        (len(place_levels(place)), factor, project_id, connector_id, site, place)
                        for place, factor in sorted(_telling(by_place).items())
                    )
        cut = len(avoids) > MAX_FEED_IP_AVOIDS or len(factors) > MAX_FEED_FACTORS
        if cut and not self._cut:
            logger.warning(
                "Exit reputation cut to fit the routing feed",
                ip_avoids=len(avoids),
                max_ip_avoids=MAX_FEED_IP_AVOIDS,
                factors=len(factors),
                max_factors=MAX_FEED_FACTORS,
            )
        self._cut = cut
        if len(avoids) > MAX_FEED_IP_AVOIDS:
            # Stable: avoids ending together keep their key order.
            avoids = sorted(avoids, key=lambda avoid: avoid[0], reverse=True)[:MAX_FEED_IP_AVOIDS]
        if len(factors) > MAX_FEED_FACTORS:
            factors = sorted(factors, key=lambda factor: (factor[0], factor[1]))[:MAX_FEED_FACTORS]
        section = self._sectioned(avoids, factors)
        if section != self._section:
            self._section = section

    @staticmethod
    def _sectioned(avoids: Iterable[_Avoid], factors: Iterable[_Factor]) -> ReputationSection:
        """The section holding ``avoids`` and ``factors``, every map in key order."""
        ip_avoids: dict[str, dict[str, dict[str, datetime]]] = {}
        for end, project_id, ip, site in sorted(avoids, key=lambda avoid: avoid[1:]):
            ip_avoids.setdefault(project_id, {}).setdefault(ip, {})[site] = end
        connector_factors: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
        for _levels, factor, project_id, connector_id, site, place in sorted(factors, key=lambda entry: entry[2:]):
            connector_factors.setdefault(project_id, {}).setdefault(connector_id, {}).setdefault(site, {})[place] = factor
        return ReputationSection(
            projects={
                project_id: ProjectReputation(
                    ip_avoids=ip_avoids.get(project_id, {}), connector_factors=connector_factors.get(project_id, {})
                )
                for project_id in sorted(ip_avoids.keys() | connector_factors.keys())
            }
        )


def _telling(factors: Mapping[str, float]) -> dict[str, float]:
    """The factors of one connector and site, by level of the requested place, that change what routing takes: each
    that differs from what its coarser levels give (the finest of them with a factor, 1 when none has one).

    Routing takes the finest level with a factor, so leaving out the others gives every request the same weight.
    """
    telling: dict[str, float] = {}
    for place, factor in factors.items():
        coarser = next((factors[level] for level in place_levels(place)[1:] if level in factors), 1.0)
        if factor != coarser:
            telling[place] = factor
    return telling
