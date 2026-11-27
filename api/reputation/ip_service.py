# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The exit IPs view of exit reputation: a page of a project's records, one IP's, and per-IP totals, each record as it
stands when answered under the project's policy."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from api.core import utc_now
from api.db.geo_repository import ObservationRepository
from api.db.repository import ConnectorRepository, ProjectRepository
from api.db.reputation_repository import ExitReputationRepository
from api.models import exit_reputation
from api.models.exit_reputation import (
    ExitIpConnector,
    ExitIpReputation,
    ExitIpSummary,
    ExitReputationPage,
    ExitReputationRecord,
    ExitReputationSummaryResponse,
)
from api.reputation.record import ReputationPolicy, ReputationStatus
from api.reputation.site import canonical_ip
from api.services.errors import InputRejectedError, NotFoundError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.reputation_repository import ReputationSort
    from api.db.session import SessionFactory
    from api.models.exit_reputation import ExitReputationFilters
    from api.models.project import Project


class ExitIpService:
    """Reads a project's exit IP records: a filtered page, one IP's across its sites, and per-IP totals."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def of_project(
        self,
        project_id: str,
        filters: ExitReputationFilters,
        *,
        sort: ReputationSort,
        limit: int,
        offset: int,
    ) -> ExitReputationPage:
        """One page of the project's records, one per exit IP and site, filtered on the server, ``limit`` held to
        1-1000 and ``offset`` to 0 or more; the total is counted no further than 10,000 rows past the page."""
        async with self._session_factory() as session:
            project = await _project(session, project_id)
            limit = max(1, min(limit, 1000))
            offset = max(0, offset)
            now = utc_now()
            policy = ReputationPolicy.of(project)
            records, total, exact = await ExitReputationRepository(session).page(
                project.id,
                now,
                ip=(filters.ip or "").strip() or None,
                site=(filters.site or "").strip() or None,
                status=None if filters.status == "any" else ReputationStatus(filters.status),
                sort=sort,
                limit=limit,
                offset=offset,
            )
        return ExitReputationPage(
            records=[ExitReputationRecord.of(record, now, policy) for record in records],
            total=total,
            total_exact=exact,
            limit=limit,
            offset=offset,
        )

    async def get(self, project_id: str, ip: str) -> ExitIpReputation:
        """One exit IP in the project: its sites' records, those in force first, and the project's connectors that
        handed it out; the project is checked before the IP."""
        async with self._session_factory() as session:
            project = await _project(session, project_id)
            canonical = canonical_ip(ip)
            if canonical is None:
                raise InputRejectedError("Not a valid IP address")
            now = utc_now()
            policy = ReputationPolicy.of(project)
            repository = ExitReputationRepository(session)
            # Read through its module, where the answer's description names it too.
            records = await repository.of_ip(
                project.id, canonical, now, limit=exit_reputation.MAX_IP_SITES
            )
            totals = (await repository.summaries(project.id, [canonical], now)).get(canonical)
            names = {
                connector.id: connector.name
                for connector in await ConnectorRepository(session).get_by_project(project.id)
            }
            # One row per connector at most: the IP is exact.
            seen, _total, _exact = await ObservationRepository(session).exit_ips(
                connector_ids=list(names), ip=canonical, limit=max(1, len(names))
            )
        return ExitIpReputation(
            ip=canonical,
            sites=totals.sites if totals else 0,
            avoided_sites=totals.avoided_sites if totals else 0,
            records=[ExitReputationRecord.of(record, now, policy) for record in records],
            connectors=[
                ExitIpConnector(
                    connector_id=row["connector_id"],
                    connector_name=names[row["connector_id"]],
                    first_seen=row["first_seen"],
                    last_seen=row["last_seen"],
                    sightings=row["sightings"],
                )
                for row in seen
            ],
        )

    async def summaries(self, project_id: str, ips: Sequence[str]) -> ExitReputationSummaryResponse:
        """Per IP, its records over every site, in the order asked; an IP with no record, or that is not an IP, is left
        out."""
        async with self._session_factory() as session:
            project = await _project(session, project_id)
            asked = {text: canonical_ip(text) for text in ips}
            found = await ExitReputationRepository(session).summaries(
                project.id, {ip for ip in asked.values() if ip is not None}, utc_now()
            )
        return ExitReputationSummaryResponse(
            ips=[
                ExitIpSummary.of(text, found[ip])
                for text, ip in asked.items()
                if ip is not None and ip in found
            ]
        )


async def _project(session: AsyncSession, project_id: str) -> Project:
    """The project ``project_id`` names; not found when there is none."""
    project = await ProjectRepository(session).get_by_id(project_id)
    if project is None:
        raise NotFoundError("Project not found")
    return project
