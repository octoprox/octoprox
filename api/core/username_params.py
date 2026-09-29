# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Username parameter parsing for proxy authentication.

Parses proxy authentication usernames to extract routing parameters.
Parameters are appended to the project username as ``-<key>-<value>``
segments and may appear in any order:

    <username>[-sessid-<session_id>][-cc-<country>][-st-<state>][-city-<city>]

Examples:
    'myuser'                          -> username only
    'myuser-sessid-abc123'            -> sticky session 'abc123'
    'myuser-cc-us'                    -> route through connectors serving US
    'myuser-cc-de-sessid-abc'         -> both, in either order
    'myuser-cc-us-st-ny-city-new_york' -> New York City exits (state and city need the country)
"""

from typing import NamedTuple

from api.models.connector import normalize_country_code
from api.models.location import LocationTarget, normalize_state_code, slugify_place
from api.models.project import Project

SESSID_SEPARATOR = "-sessid-"
COUNTRY_SEPARATOR = "-cc-"
STATE_SEPARATOR = "-st-"
CITY_SEPARATOR = "-city-"

# Every reserved delimiter and the parameter it introduces. Values run
# until the next delimiter (or the end of the string), so a value may
# contain hyphens but must not contain another delimiter.
_PARAM_SEPARATORS: dict[str, str] = {
    SESSID_SEPARATOR: "sessid",
    COUNTRY_SEPARATOR: "country",
    STATE_SEPARATOR: "state",
    CITY_SEPARATOR: "city",
}

LOCATION_NEEDS_COUNTRY = "State and city targeting need a country: add -cc-<code> to the username"
LOCATION_INVALID_COUNTRY = "Country must be a two-letter ISO 3166-1 code, such as -cc-us"
LOCATION_INVALID_STATE = "State must be the subdivision part of an ISO 3166-2 code, such as -st-ny or -st-eng"
LOCATION_INVALID_CITY = "City must be a name with letters or digits, such as -city-new_york"


class UsernameParams(NamedTuple):
    """Routing parameters extracted from a proxy username.

    ``location`` is what the request asked for, or None when it named no
    place. ``location_error`` says why a named place cannot be honoured (a
    state or city without a country); the request must be refused rather
    than routed anywhere.
    """

    username: str
    sessid: str | None
    location: LocationTarget | None
    location_error: str | None = None

    @property
    def country(self) -> str | None:
        """The requested country, for callers that only care about it."""
        return self.location.country if self.location else None


class AuthResult(NamedTuple):
    """Result of proxy authentication with extracted routing parameters."""

    project: Project
    sessid: str | None
    location: LocationTarget | None = None
    location_error: str | None = None


def parse_username_params(raw_username: str) -> UsernameParams:
    """Parse a proxy username into the real username and its routing parameters.

    Each reserved delimiter is matched at its first occurrence; the value
    extends up to the next delimiter. A delimiter at position 0 (empty
    username) disables parsing and the raw string is returned unchanged.
    Empty values are treated as absent. Country codes are normalised to
    upper case, with UK accepted as an alias of GB. The state is the
    upper-cased ISO 3166-2 subdivision part (``ny``, ``US-NY`` and ``NY``
    all give ``NY``), the city a slug (``new york``, ``New-York`` and
    ``new_york`` all give ``new_york``). This is the one place client input
    is normalised: a value that cannot be read (a country that is not a
    two-letter code, a state that is not a subdivision code, a city with
    nothing to slug), or a state or city without a country, sets
    ``location_error`` and the request is refused rather than routed.

    Examples:
        'myuser' -> ('myuser', None, None)
        'myuser-sessid-abc-123' -> ('myuser', 'abc-123', None)
        'myuser-cc-us' -> ('myuser', None, LocationTarget('US'))
        'myuser-sessid-abc-cc-gb' -> ('myuser', 'abc', LocationTarget('GB'))
        'myuser-cc-us-st-ny-city-new_york' -> ('myuser', None, LocationTarget('US', 'NY', 'new_york'))
        'myuser-sessid-' -> ('myuser', None, None)
        '-sessid-abc' -> ('-sessid-abc', None, None)
    """
    positions: list[tuple[int, str]] = []
    for separator in _PARAM_SEPARATORS:
        index = raw_username.find(separator)
        if index > 0:
            positions.append((index, separator))

    if not positions:
        return UsernameParams(raw_username, None, None)

    positions.sort()
    username = raw_username[: positions[0][0]]

    values: dict[str, str | None] = {name: None for name in _PARAM_SEPARATORS.values()}
    for i, (index, separator) in enumerate(positions):
        start = index + len(separator)
        end = positions[i + 1][0] if i + 1 < len(positions) else len(raw_username)
        value = raw_username[start:end]
        values[_PARAM_SEPARATORS[separator]] = value if value else None

    # A value that cannot be read is refused, not kept: it would otherwise be
    # rendered into a dynamic request and recorded as the claim to verify, and
    # a request must never be widened to whatever part of it did parse.
    error: str | None = None
    raw_country = values["country"]
    country: str | None = None
    if raw_country is not None:
        try:
            country = normalize_country_code(raw_country)
        except ValueError:
            country = raw_country.strip().upper() or None
            error = LOCATION_INVALID_COUNTRY

    raw_state = values["state"]
    state: str | None = None
    if raw_state is not None:
        try:
            state = normalize_state_code(raw_state)
        except ValueError:
            state = raw_state.strip().upper() or None
            error = LOCATION_INVALID_STATE

    raw_city = values["city"]
    city = slugify_place(raw_city)
    if raw_city is not None and raw_city.strip() and city is None:
        error = error or LOCATION_INVALID_CITY

    location = LocationTarget(country=country, state=state, city=city)
    if location.is_empty and error is None:
        return UsernameParams(username, values["sessid"], None)
    if error is None and location.below_country and country is None:
        error = LOCATION_NEEDS_COUNTRY
    return UsernameParams(username, values["sessid"], location or None, error)


def parse_proxy_username(raw_username: str) -> tuple[str, str | None]:
    """Parse a proxy username to extract the real username and session ID.

    Thin wrapper over :func:`parse_username_params` kept for callers that
    only care about the session ID.

    Args:
        raw_username: The raw username from Proxy-Authorization header.

    Returns:
        Tuple of (real_username, session_id). session_id is None if not present.
    """
    params = parse_username_params(raw_username)
    return params.username, params.sessid
