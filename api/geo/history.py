# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The stored observations and exit IPs as the admin pages read them: the log a page at a time, and per connector how
the vendor's claims were judged, how often exits recur and how a project's connectors share them.

Rows name their connector and project from those rows, read in the same session; a deleted one shows its id only.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from api.core import as_naive_utc, utc_now
from api.db.geo_repository import READ_TIMEOUT_MS, ObservationRepository
from api.db.repository import ConnectorRepository, ProjectRepository
from api.models.geo import (
    AccuracyResponse,
    ConnectorAccuracy,
    ConnectorExits,
    ConnectorSharing,
    CountResponse,
    ExitIp,
    ExitIpFilters,
    ExitIpsResponse,
    ExitsResponse,
    LevelAccuracy,
    ObservationFilters,
    ObservationsResponse,
    SharedPair,
    SharingResponse,
)
from api.services.errors import NotFoundError, TimedOutError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.session import SessionFactory
    from api.models.connector import Connector
    from api.models.geo import ExitCoverages


class ObservationHistory:
    """Reads the observation log and each connector's exit statistics.

    Args:
        coverage: What exit statistics can see of each dynamic-sessions connector, from the read model.
    """

    def __init__(self, session_factory: SessionFactory, *, coverage: ExitCoverages) -> None:
        self._session_factory = session_factory
        self._coverage = coverage

    async def page(
        self, filters: ObservationFilters, limit: int, offset: int
    ) -> ObservationsResponse:
        """One page of IP observations, newest first, ``limit`` clamped to 1-1000 and the total counted no further
        than 10,000 rows past the page. A page that reads past the time limit is timed out."""
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        async with self._session_factory() as session:
            page = await ObservationRepository(session).recent(
                **_observation_query(filters), limit=limit, offset=offset
            )
            if page is None:
                raise _log_timed_out()
            rows, total, exact = page
            # A deleted connector or project shows its id only.
            connectors = await _connectors_of(session, (row["connector_id"] for row in rows))
            projects = await _project_names(session, (row["project_id"] for row in rows))
        for row in rows:
            connector = connectors.get(row["connector_id"]) if row["connector_id"] else None
            row["connector_name"] = connector.name if connector else None
            row["project_name"] = projects.get(row["project_id"]) if row["project_id"] else None
        return ObservationsResponse(
            observations=rows, total=total, total_exact=exact, limit=limit, offset=offset
        )

    async def count(self, filters: ObservationFilters) -> CountResponse:
        """Every observation under the log's filters, counted outright; timed out past the time limit."""
        async with self._session_factory() as session:
            total = await ObservationRepository(session).count_matching(
                **_observation_query(filters)
            )
        if total is None:
            raise _count_timed_out()
        return CountResponse(total=total)

    async def accuracy(
        self, project_id: str | None, connector_id: str | None, days: int
    ) -> AccuracyResponse:
        """Per connector: of the distinct exits seen in the last ``days`` (1-3650), how the vendor's claims were judged."""
        since = utc_now() - timedelta(days=max(1, min(days, 3650)))
        async with self._session_factory() as session:
            rows = await ObservationRepository(session).exit_accuracy(
                connector_ids=await _scope(session, project_id, connector_id), since=since
            )
            connectors = await _connectors_of(session, (row["connector_id"] for row in rows))
        coverage = await self._coverage(row["connector_id"] for row in rows)
        result: list[ConnectorAccuracy] = []
        for row in rows:
            connector = connectors.get(row["connector_id"])
            result.append(
                ConnectorAccuracy(
                    connector_id=row["connector_id"],
                    connector_name=connector.name if connector else None,
                    project_id=connector.project_id if connector else None,
                    coverage=coverage.get(row["connector_id"]),
                    exits=row["exits"],
                    country=LevelAccuracy.from_counts(row["country"]),
                    state=LevelAccuracy.from_counts(row["state"]),
                    city=LevelAccuracy.from_counts(row["city"]),
                    breakdown=row["breakdown"],
                )
            )
        result.sort(key=lambda c: (-c.country.claimed, c.connector_name or ""))
        return AccuracyResponse(since=since, connectors=result)

    async def exits(
        self, project_id: str | None, connector_id: str | None, days: int
    ) -> ExitsResponse:
        """Per connector: how many distinct exit IPs it has handed out, and how often they recur, in the last ``days``."""
        since = utc_now() - timedelta(days=max(1, min(days, 3650)))
        async with self._session_factory() as session:
            rows = await ObservationRepository(session).exit_summary(
                connector_ids=await _scope(session, project_id, connector_id), since=since
            )
            connectors = await _connectors_of(session, (row["connector_id"] for row in rows))
        coverage = await self._coverage(row["connector_id"] for row in rows)
        result = []
        for row in rows:
            connector = connectors.get(row["connector_id"])
            result.append(
                ConnectorExits(
                    connector_name=connector.name if connector else None,
                    project_id=connector.project_id if connector else None,
                    coverage=coverage.get(row["connector_id"]),
                    **row,
                )
            )
        result.sort(key=lambda c: (-c.unique_total, c.connector_name or ""))
        return ExitsResponse(since=since, connectors=result)

    async def sharing(self, project_id: str, days: int) -> SharingResponse:
        """How a project's connectors' exits seen in the last ``days`` overlap: per connector, per pair and across the
        project; timed out past the time limit."""
        async with self._session_factory() as session:
            if await ProjectRepository(session).get_by_id(project_id) is None:
                raise NotFoundError("Project not found")
            since = utc_now() - timedelta(days=max(1, min(days, 3650)))
            connectors = {
                c.id: c for c in await ConnectorRepository(session).get_by_project(project_id)
            }
            sharing = await ObservationRepository(session).exit_sharing(
                connector_ids=list(connectors), since=since
            )
        if sharing is None:
            raise TimedOutError(
                f"Comparing the exits took longer than {READ_TIMEOUT_MS // 1000} s. Pick a shorter window."
            )
        rows = sharing["connectors"]
        coverage = await self._coverage(row["connector_id"] for row in rows)
        result = []
        for row in rows:
            connector = connectors.get(row["connector_id"])
            result.append(
                ConnectorSharing(
                    connector_name=connector.name if connector else None,
                    provider=connector.credential_type if connector else None,
                    coverage=coverage.get(row["connector_id"]),
                    **row,
                )
            )
        result.sort(key=lambda c: (-c.exits, c.connector_name or ""))
        return SharingResponse(
            since=since,
            exits=sharing["exits"],
            shared=sharing["shared"],
            connectors=result,
            pairs=[SharedPair(**pair) for pair in sharing["pairs"]],
        )

    async def exit_ips(self, filters: ExitIpFilters, limit: int, offset: int) -> ExitIpsResponse:
        """One page of distinct exit IPs with their latest state, a row per connector and IP, most recently seen
        first; ``verdict`` is the country verdict, and the total is counted as for observations."""
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        async with self._session_factory() as session:
            query = await _exit_ip_query(session, filters)
            rows, total, exact = await ObservationRepository(session).exit_ips(
                **query, limit=limit, offset=offset
            )
            connectors = await _connectors_of(session, (row["connector_id"] for row in rows))
            projects = await _project_names(
                session, (connector.project_id for connector in connectors.values())
            )
        ips = []
        for row in rows:
            connector = connectors.get(row["connector_id"])
            ips.append(
                ExitIp(
                    connector_name=connector.name if connector else None,
                    project_id=connector.project_id if connector else None,
                    project_name=projects.get(connector.project_id) if connector else None,
                    **row,
                )
            )
        return ExitIpsResponse(ips=ips, total=total, total_exact=exact, limit=limit, offset=offset)

    async def count_exit_ips(self, filters: ExitIpFilters) -> CountResponse:
        """Every exit IP under the view's filters, counted outright; timed out past the time limit."""
        async with self._session_factory() as session:
            total = await ObservationRepository(session).count_exit_ips(
                **await _exit_ip_query(session, filters)
            )
        if total is None:
            raise _count_timed_out()
        return CountResponse(total=total)


def _observation_query(filters: ObservationFilters) -> dict[str, Any]:
    """The log's filters as the repository takes them: blank text narrows nothing, and ``until`` is naive UTC."""
    return {
        "connector_id": filters.connector_id,
        "proxy_id": (filters.proxy_id or "").strip() or None,
        "project_id": filters.project_id,
        "source": filters.source,
        "ip": (filters.ip or "").strip() or None,
        "claimed_country": (filters.claimed_country or "").strip() or None,
        "resolved_country": (filters.resolved_country or "").strip() or None,
        # The country's: the state and city verdicts come per row and move no badge.
        "verdict": filters.verdict,
        "conflicts_only": filters.conflicts_only,
        "until": as_naive_utc(filters.until),
    }


async def _exit_ip_query(session: AsyncSession, filters: ExitIpFilters) -> dict[str, Any]:
    """The exit IPs view's filters as the repository takes them; a project becomes its connectors."""
    return {
        "connector_ids": await _scope(session, filters.project_id, filters.connector_id),
        "ip": (filters.ip or "").strip() or None,
        "proxy_id": (filters.proxy_id or "").strip() or None,
        "country": (filters.country or "").strip() or None,
        "claimed_country": (filters.claimed_country or "").strip() or None,
        "verdict": filters.verdict,
    }


def _count_timed_out() -> TimedOutError:
    """The timeout a count past the read time limit answers with."""
    return TimedOutError(
        f"Counting took longer than {READ_TIMEOUT_MS // 1000} s. Narrow the filters first."
    )


def _log_timed_out() -> TimedOutError:
    """The timeout a log page past the read time limit answers with."""
    return TimedOutError(
        f"Reading the log took longer than {READ_TIMEOUT_MS // 1000} s. If these filters match only older rows, set Until to around then."
    )


async def _connectors_of(
    session: AsyncSession, connector_ids: Iterable[str | None]
) -> dict[str, Connector]:
    """The connectors that name a page's rows, by id, in one read; a deleted one is absent (its rows show the id)."""
    wanted = {connector_id for connector_id in connector_ids if connector_id}
    return {
        connector.id: connector for connector in await ConnectorRepository(session).get_many(wanted)
    }


async def _project_names(
    session: AsyncSession, project_ids: Iterable[str | None]
) -> dict[str, str]:
    """The names of the projects that name a page's rows, by id, in one read; a deleted one is absent."""
    wanted = {project_id for project_id in project_ids if project_id}
    return {
        project.id: project.name for project in await ProjectRepository(session).get_many(wanted)
    }


async def _scope(
    session: AsyncSession, project_id: str | None, connector_id: str | None
) -> list[str] | None:
    """The connectors a view is limited to: the one named, a project's, or None for every connector."""
    if connector_id:
        return [connector_id]
    if project_id:
        return [
            connector.id
            for connector in await ConnectorRepository(session).get_by_project(project_id)
        ]
    return None
