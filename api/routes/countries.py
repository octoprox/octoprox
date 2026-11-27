# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The Countries page: which connectors serve each country of a project, edited across connectors in one write.

Where requests go comes from :class:`~api.services.countries.CountryService`; edits go through
:class:`~api.services.connectors.ConnectorService`.
"""

from fastapi import APIRouter, Request

from api.core.auth import RequireEditorDep
from api.models.countries import CountriesOverview, CountryUpdate
from api.routes.common import actor_of, connector_service_of, country_service_of

router = APIRouter(prefix="/projects/{project_id}/countries")


@router.get("", response_model=CountriesOverview)
async def get_countries(request: Request, project_id: str) -> CountriesOverview:
    """The project's connectors with their country settings, and where a request for each country would go now.

    Every country a connector lists, reserves or excludes, or holds an exit in, is listed; ``other`` stands for any
    other country, and ``untargeted`` for requests naming none.
    """
    return await country_service_of(request).overview(project_id)


@router.put("/{country}", response_model=CountriesOverview)
async def set_country(
    request: Request, project_id: str, country: str, update: CountryUpdate, user: RequireEditorDep
) -> CountriesOverview:
    """Set how connectors of the project stand to ``country`` (off, serves, reserved), all or none; the new overview.

    A connector listing countries takes the country on or off its list, which provisions or drops its slots; one
    listing none excludes it or not.
    """
    await connector_service_of(request).set_country(project_id, country, update.connectors, actor_of(request, user))
    return await country_service_of(request).overview(project_id)
