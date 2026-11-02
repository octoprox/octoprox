# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What the Countries page shows and edits: which connectors of a project serve each country, and how."""

from enum import StrEnum

from pydantic import BaseModel

from api.models.connect import ConnectRoute


class CountryRole(StrEnum):
    """How one connector stands to one country, as the Countries page sets it on the connector's settings."""

    # Not served: off the list of a connector that lists countries, else excluded.
    OFF = "off"
    # Served: on the list, or not excluded by a connector that lists none.
    SERVES = "serves"
    # Served, and requests naming the country go only to the connectors reserving it.
    RESERVED = "reserved"


class CountryConnector(BaseModel):
    """One connector of the project, with the country settings each country's role is read from."""

    id: str
    name: str
    credential_type: str
    enabled: bool
    weight: int
    # The countries it lists; empty when it lists none and serves every country it does not exclude.
    countries: list[str]
    # Whether its proxies are held per listed country, so taking a country on or off its list creates or drops them.
    provisions_per_country: bool
    reserved_countries: list[str]
    excluded_countries: list[str]
    # A dynamic-sessions gateway, which renders the country into each request.
    dynamic: bool


class CountryRoutes(BaseModel):
    """Where a request naming the country would go now; no routes when it would fail."""

    country: str
    routes: list[ConnectRoute]


class CountriesOverview(BaseModel):
    """A project's connectors and, per country any of them names or exits in, where its requests go now."""

    connectors: list[CountryConnector]
    countries: list[CountryRoutes]
    # A country none of the connectors names or exits in, and a request naming no country.
    other: list[ConnectRoute]
    untargeted: list[ConnectRoute]


class CountryUpdate(BaseModel):
    """The roles to set for one country, by connector id; connectors left out keep theirs."""

    connectors: dict[str, CountryRole]
