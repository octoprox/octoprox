# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The Connect page: how a client reaches a project's proxy, for the place and session it asks for.

The username is read by the parser the proxy listener uses, and where it would route is previewed with the gateway's own
router over a compile of the project.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from api.core.username_params import (
    CITY_SEPARATOR,
    COUNTRY_SEPARATOR,
    RESERVED_SEPARATORS,
    SESSID_SEPARATOR,
    STATE_SEPARATOR,
    format_username_params,
    parse_username_params,
)
from api.db.repository import ProjectRepository
from api.models.connect import ConnectEndpoint, ConnectInfo, ConnectLocation
from api.services.errors import NotFoundError

if TYPE_CHECKING:
    from api.db.session import SessionFactory
    from api.feed.preview import RoutingPreview

_SEPARATORS = (COUNTRY_SEPARATOR, STATE_SEPARATOR, CITY_SEPARATOR, SESSID_SEPARATOR)
# Every reserved delimiter in a string, overlapping ones included: "-sessid-st-ny" holds two.
_DELIMITERS = re.compile(
    "(?=" + "|".join(re.escape(separator) for separator in RESERVED_SEPARATORS) + ")"
)


class ConnectService:
    """Composes the username for a place and session as the proxy reads it, and previews where it would route.

    Args:
        preview: Compiles one project as a fresh gateway would, with the quarantines and traffic blocks stored now.
        proxy_endpoint: ``(scheme, host, port)`` clients reach the proxy at (``Settings.proxy_endpoint``).
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        preview: RoutingPreview,
        proxy_endpoint: tuple[Literal["http", "https"], str | None, int | None],
    ) -> None:
        self._session_factory = session_factory
        self._preview = preview
        self._proxy_endpoint = proxy_endpoint

    async def info(
        self,
        project_id: str,
        country: str | None,
        state: str | None,
        city: str | None,
        sessid: str | None,
    ) -> ConnectInfo:
        """The proxy endpoint, the username for the place and session, and the connectors it would spread over.

        A refusal is the one the proxy would answer the username with, and then nothing routes.
        """
        async with self._session_factory() as session:
            project = await ProjectRepository(session).get_by_id(project_id)
        if project is None:
            raise NotFoundError("Project not found")

        values = [
            (separator, value.strip())
            for separator, value in zip(_SEPARATORS, (country, state, city, sessid), strict=True)
            if value and value.strip()
        ]
        composed = project.username + "".join(f"{separator}{value}" for separator, value in values)
        params = parse_username_params(composed)
        error = params.location_error
        if params.username != project.username:
            error = "The project username contains a reserved delimiter (-cc-, -st-, -city-, -sessid-, -prxy- or -tag-), so the proxy cannot read it"
        elif len(_DELIMITERS.findall(composed, len(project.username))) != len(values):
            # Only the delimiters put there may follow the username: a value holding one, or
            # forming one with the dash before or after it ("st-ny" after -sessid- reads as
            # -st-ny), would be read as another parameter.
            error = "A value cannot contain -cc-, -st-, -city-, -sessid-, -prxy- or -tag-, including one formed with the dash before or after it"
        elif params.sessid and ":" in params.sessid:
            # Basic auth splits user and password at the first colon.
            error = "A session ID cannot contain a colon"
        location = params.location
        preview = await self._preview.project(project_id)
        countries, any_country, places = preview.served_places(project_id)

        scheme, host, port = self._proxy_endpoint

        return ConnectInfo(
            endpoint=ConnectEndpoint(scheme=scheme, host=host, port=port),
            username=composed
            if error
            else format_username_params(project.username, params.sessid, location),
            sessid=params.sessid,
            location=ConnectLocation(
                country=location.country, state=location.state, city=location.city
            )
            if location
            else None,
            error=error,
            routes=[] if error else preview.route_preview(project_id, location),
            countries=countries,
            any_country=any_country,
            places=places,
        )
