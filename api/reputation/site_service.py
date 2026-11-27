# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The Reputation tab and the project form's preview: one site across every place, how its connectors' values moved at
one place over time, and what a draft Strength and Lowest factor would do, as gateways would weigh them.

Each read checks the project in a short session, builds the preview router (Redis and a compile), then reads the
records in one session, and works the eligibility out beside the event loop once that session is closed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

from api.core import utc_now
from api.db.repository import ConnectorRepository, ProjectRepository
from api.db.reputation_repository import ExitReputationRepository
from api.models.exit_reputation import (
    PREVIEW_PLACES,
    HistoryRange,
    PlaceHistoryView,
    ReputationSite,
    SitePlacesView,
    SiteSummary,
    StrengthPreviewConnector,
    StrengthPreviewPlace,
    StrengthPreviewResponse,
    place_label,
)
from api.reputation.history import FactorHistory
from api.reputation.record import place_levels
from api.reputation.site import report_site
from api.reputation.split import ReputationSplit, SitePlaces, place_key
from api.reputation.weight import ReputationRule
from api.services.errors import InputRejectedError, NotFoundError

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from api.db.session import SessionFactory
    from api.feed.preview import RoutingPreview
    from api.models.project import Project
    from api.routing.router import Router


class SiteReputationService:
    """Reads one site of a project across its places, a place's history, and a draft Strength's shares."""

    def __init__(self, session_factory: SessionFactory, *, preview: RoutingPreview) -> None:
        self._session_factory = session_factory
        self._preview = preview

    async def places(self, project_id: str, site: str | None) -> SitePlacesView:
        """One site across every place its records name; ``site`` None or blank, the one the Reputation tab opens on.
        A site that names none is refused before the project is looked up."""
        try:
            wanted = report_site(site) if site is not None and site.strip() else None
        except ValueError as exc:
            raise InputRejectedError(str(exc)) from None
        project = await self._project(project_id)
        preview = await self._preview.project(project.id)
        async with self._session_factory() as session:
            repository = ExitReputationRepository(session)
            sites = [
                ReputationSite(site=name, reports=reports)
                for name, reports in await repository.busiest_sites(project.id)
            ]
            connectors = sorted(
                await ConnectorRepository(session).get_by_project(project.id),
                key=lambda connector: connector.name.lower(),
            )
            ids = [connector.id for connector in connectors]
            now = utc_now()
            if wanted is None and sites:
                wanted = await _opening_site(session, project, preview, sites, ids, now)
            elif wanted is not None and all(listed.site != wanted for listed in sites):
                sites.insert(0, ReputationSite(site=wanted, reports=0))
            if wanted is None:
                mode = project.exit_reputation
                rule = ReputationRule.of(project)
                return SitePlacesView(
                    site=None,
                    mode=mode,
                    reputation_applied=mode.avoids,
                    reputation_strength=rule.strength,
                    reputation_floor=rule.floor,
                    sites=sites,
                    connectors=[],
                    places=[],
                    summary=SiteSummary(lowered_connectors=0, serving_connectors=0, hardest=None),
                )
            split = await ReputationSplit.read(
                session, project, preview, sites=[wanted], levels=None, now=now, connector_ids=ids
            )
        # One eligibility pass per place: worked out beside the event loop, on a router built for this request alone.
        return await asyncio.to_thread(
            lambda: SitePlaces(split, wanted, ids).view(project, connectors, sites)
        )

    async def history(
        self, project_id: str, site: str, place: str | None, range: HistoryRange
    ) -> PlaceHistoryView:
        """How the project's connectors taking the requests to ``site`` from ``place`` moved over ``range``. A site
        or a place that names none is refused before the project is looked up."""
        try:
            wanted = report_site(site)
            wanted_place = place_key(place)
        except ValueError as exc:
            raise InputRejectedError(str(exc)) from None
        project = await self._project(project_id)
        preview = await self._preview.project(project.id)
        async with self._session_factory() as session:
            history = await FactorHistory.read(
                session,
                project,
                preview,
                site=wanted,
                place=wanted_place,
                range=range,
                now=utc_now(),
            )
        # The points of every moment for every connector: worked out beside the event loop.
        return await asyncio.to_thread(history.view)

    async def preview_strength(
        self, project_id: str, site: str | None, *, strength: int, floor: int
    ) -> StrengthPreviewResponse:
        """Each connector's share of the requests to a site from its most reported places, under the project's saved
        and draft Strength and Lowest factor; ``site`` None or blank, the one the Reputation tab opens on. A site
        that names none is refused before the project is looked up."""
        try:
            wanted = report_site(site) if site is not None and site.strip() else None
        except ValueError as exc:
            raise InputRejectedError(str(exc)) from None
        project = await self._project(project_id)
        preview = await self._preview.project(project.id)
        async with self._session_factory() as session:
            repository = ExitReputationRepository(session)
            connectors = sorted(
                await ConnectorRepository(session).get_by_project(project.id),
                key=lambda connector: connector.name.lower(),
            )
            ids = [connector.id for connector in connectors]
            now = utc_now()
            if wanted is None:
                sites = [
                    ReputationSite(site=name, reports=reports)
                    for name, reports in await repository.busiest_sites(project.id)
                ]
                if not sites:
                    return StrengthPreviewResponse(site=None, places=[])
                wanted = await _opening_site(session, project, preview, sites, ids, now)
            places = await repository.busiest_places(project.id, wanted, limit=PREVIEW_PLACES)
            if not places:
                return StrengthPreviewResponse(site=wanted, places=[])
            current = await ReputationSplit.read(
                session,
                project,
                preview,
                sites=[wanted],
                levels={level for place, _reports in places for level in place_levels(place)},
                now=now,
                connector_ids=ids,
            )
        draft = current.under(
            project.model_copy(
                update={"exit_reputation_strength": strength, "exit_reputation_floor": floor}
            )
        )
        site_name = wanted

        def previewed() -> list[StrengthPreviewPlace]:
            listed: list[StrengthPreviewPlace] = []
            for place, reports in places:
                now_shares = current.shares(site_name, place, ids)
                draft_shares = draft.shares(site_name, place, ids)
                listed.append(
                    StrengthPreviewPlace(
                        place=place,
                        label=place_label(place),
                        reports=reports,
                        connectors=[
                            StrengthPreviewConnector(
                                connector_id=connector.id,
                                name=connector.name,
                                current_share=round(now_shares[connector.id].avoid_share, 2),
                                draft_share=round(draft_shares[connector.id].avoid_share, 2),
                            )
                            for connector in connectors
                            if now_shares[connector.id].takes
                        ],
                    )
                )
            return listed

        # One eligibility pass per place, shared by both splits: worked out beside the event loop.
        return StrengthPreviewResponse(site=wanted, places=await asyncio.to_thread(previewed))

    async def _project(self, project_id: str) -> Project:
        """The project ``project_id`` names, read in a session closed before the preview reads Redis; not found when
        there is none."""
        async with self._session_factory() as session:
            project = await ProjectRepository(session).get_by_id(project_id)
        if project is None:
            raise NotFoundError("Project not found")
        return project


async def _opening_site(
    session: AsyncSession,
    project: Project,
    preview: Router,
    sites: Sequence[ReputationSite],
    connector_ids: Sequence[str],
    now: datetime,
) -> str:
    """The site the Reputation tab opens on: the most reported one with a place to list, else the most reported."""
    names = [listed.site for listed in sites]
    listed_first = await ReputationSplit.busiest_listed(
        session, project, preview, sites=names, now=now, connector_ids=connector_ids
    )
    return listed_first or names[0]
