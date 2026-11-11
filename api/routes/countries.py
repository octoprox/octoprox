# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The Countries page: which connectors serve each country of a project, edited across connectors in one write.

Where requests go comes from the gateway's own router over a compile of the project (``ApiRuntime.preview_router``),
as on the Connect page; edits go through :class:`~api.services.connectors.ConnectorService`.
"""

from fastapi import APIRouter, HTTPException, Request

from api.core.auth import RequireEditorDep
from api.models.cloud_options import COUNTRIES
from api.models.countries import CountriesOverview, CountryConnector, CountryRoutes, CountryUpdate
from api.models.location import LocationTarget, normalize_country_code
from api.routes.common import actor_of, api_runtime_of, connector_service_of, require_project

router = APIRouter(prefix="/projects/{project_id}/countries")


@router.get("", response_model=CountriesOverview)
async def get_countries(request: Request, project_id: str) -> CountriesOverview:
    """The project's connectors with their country settings, and where a request for each country would go now.

    Every country a connector lists, reserves or excludes, or holds an exit in, is listed; ``other`` stands for any
    other country, and ``untargeted`` for requests naming none.
    """
    await require_project(request, project_id)
    preview = await api_runtime_of(request).preview_router(project_id)
    in_play = preview.countries_in_play(project_id)
    # Any country no connector names or exits in routes alike, so one of them stands for all; the list's first
    # entry is the empty "no geo-targeting" option, which is no country.
    stand_in = next((option.code for option in COUNTRIES if option.code and option.code not in in_play), None)
    return CountriesOverview(
        connectors=[
            CountryConnector(
                id=view.connector.id,
                name=view.connector.name,
                credential_type=view.connector.credential_type,
                enabled=view.connector.enabled,
                weight=view.weight,
                countries=list(view.declared_countries),
                provisions_per_country=view.provider is not None and view.provider.provisions_per_country,
                reserved_countries=sorted(view.reserved),
                excluded_countries=sorted(view.excluded),
                dynamic=view.provider is not None and view.provider.is_dynamic,
            )
            for view in preview.snapshot.connector_views(project_id)
            if not view.connector.pending_deletion
        ],
        countries=[
            CountryRoutes(country=country, routes=preview.route_preview(project_id, LocationTarget(country=country)))
            for country in in_play
        ],
        other=preview.route_preview(project_id, LocationTarget(country=stand_in)) if stand_in else [],
        untargeted=preview.route_preview(project_id, None),
    )


@router.put("/{country}", response_model=CountriesOverview)
async def set_country(
    request: Request, project_id: str, country: str, update: CountryUpdate, user: RequireEditorDep
) -> CountriesOverview:
    """Set how connectors of the project stand to ``country`` (off, serves, reserved), all or none; the new overview.

    A connector listing countries takes the country on or off its list, which provisions or drops its slots; one
    listing none excludes it or not.
    """
    await require_project(request, project_id)
    try:
        code = normalize_country_code(country)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if code is None:
        raise HTTPException(status_code=422, detail="A country code is required")
    await connector_service_of(request).set_country(project_id, code, update.connectors, actor_of(request, user))
    return await get_countries(request, project_id)
