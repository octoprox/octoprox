# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Username parameter parsing for proxy authentication.

Parses proxy authentication usernames to extract routing parameters.
Parameters are appended to the project username as ``-<key>-<value>``
segments and may appear in any order:

    <username>[-sessid-<session_id>][-cc-<country_code>]

Examples:
    'myuser'                          -> username only
    'myuser-sessid-abc123'            -> sticky session 'abc123'
    'myuser-cc-us'                    -> route through connectors serving US
    'myuser-cc-de-sessid-abc'         -> both, in either order
"""

from typing import NamedTuple

from api.models.project import Project

SESSID_SEPARATOR = "-sessid-"
COUNTRY_SEPARATOR = "-cc-"

# Every reserved delimiter and the parameter it introduces. Values run
# until the next delimiter (or the end of the string), so a value may
# contain hyphens but must not contain another delimiter.
_PARAM_SEPARATORS: dict[str, str] = {
    SESSID_SEPARATOR: "sessid",
    COUNTRY_SEPARATOR: "country",
}


class UsernameParams(NamedTuple):
    """Routing parameters extracted from a proxy username."""

    username: str
    sessid: str | None
    country: str | None


class AuthResult(NamedTuple):
    """Result of proxy authentication with extracted routing parameters."""

    project: Project
    sessid: str | None
    country: str | None = None


def parse_username_params(raw_username: str) -> UsernameParams:
    """Parse a proxy username into the real username and its routing parameters.

    Each reserved delimiter is matched at its first occurrence; the value
    extends up to the next delimiter. A delimiter at position 0 (empty
    username) disables parsing and the raw string is returned unchanged.
    Empty values are treated as absent. Country codes are normalised to
    upper case.

    Examples:
        'myuser' -> ('myuser', None, None)
        'myuser-sessid-abc-123' -> ('myuser', 'abc-123', None)
        'myuser-cc-us' -> ('myuser', None, 'US')
        'myuser-sessid-abc-cc-gb' -> ('myuser', 'abc', 'GB')
        'myuser-cc-gb-sessid-abc' -> ('myuser', 'abc', 'GB')
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

    country = values["country"]
    if country is not None:
        country = country.strip().upper() or None

    return UsernameParams(username, values["sessid"], country)


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
