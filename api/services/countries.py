# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The Countries page: which connectors serve each country of a project, and where a request for each would go now.

Where requests go comes from the gateway's own router over a compile of the project (:class:`RoutingPreview`), as on
the Connect page; edits go through :meth:`~api.services.connectors.ConnectorService.set_country`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.db.repository import ProjectRepository
from api.models.cloud_options import COUNTRIES
from api.models.countries import CountriesOverview, CountryConnector, CountryRoutes
from api.models.location import LocationTarget
from api.services.errors import NotFoundError

if TYPE_CHECKING:
    from api.db.session import SessionFactory
    from api.feed.preview import RoutingPreview


class CountryService:
    """Reads a project's connectors with their country settings, and where each country routes now."""

    def __init__(self, session_factory: SessionFactory, *, preview: RoutingPreview) -> None:
        self._session_factory = session_factory
        self._preview = preview

    async def overview(self, project_id: str) -> CountriesOverview:
        """The project's connectors with their country settings, and where a request for each country they name or
        hold an exit in would go now, ``other`` standing for any other country and ``untargeted`` for requests naming
        none."""
        async with self._session_factory() as session:
            if await ProjectRepository(session).get_by_id(project_id) is None:
                raise NotFoundError("Project not found")
        preview = await self._preview.project(project_id)
        in_play = preview.countries_in_play(project_id)
        # Any country no connector names or exits in routes alike, so one of them stands for all; the list's first
        # entry is the empty "no geo-targeting" option, which is no country.
        stand_in = next(
            (option.code for option in COUNTRIES if option.code and option.code not in in_play),
            None,
        )
        return CountriesOverview(
            connectors=[
                CountryConnector(
                    id=view.connector.id,
                    name=view.connector.name,
                    credential_type=view.connector.credential_type,
                    enabled=view.connector.enabled,
                    weight=view.weight,
                    countries=list(view.declared_countries),
                    provisions_per_country=view.provider is not None
                    and view.provider.provisions_per_country,
                    reserved_countries=sorted(view.reserved),
                    excluded_countries=sorted(view.excluded),
                    dynamic=view.provider is not None and view.provider.is_dynamic,
                )
                for view in preview.snapshot.connector_views(project_id)
                if not view.connector.pending_deletion
            ],
            countries=[
                CountryRoutes(
                    country=country,
                    routes=preview.route_preview(project_id, LocationTarget(country=country)),
                )
                for country in in_play
            ],
            other=preview.route_preview(project_id, LocationTarget(country=stand_in))
            if stand_in
            else [],
            untargeted=preview.route_preview(project_id, None),
        )
