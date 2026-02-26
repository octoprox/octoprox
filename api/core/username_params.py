# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Username parameter parsing for proxy authentication.

Parses proxy authentication usernames to extract session parameters.
Format: <username>-sessid-<session_id>
"""

from typing import NamedTuple

from api.models.project import Project

SESSID_SEPARATOR = "-sessid-"


class AuthResult(NamedTuple):
    """Result of proxy authentication with extracted session parameters."""

    project: Project
    sessid: str | None


def parse_proxy_username(raw_username: str) -> tuple[str, str | None]:
    """Parse a proxy username to extract the real username and session ID.

    The session ID is encoded in the username using the '-sessid-' delimiter.

    Examples:
        'myuser' -> ('myuser', None)
        'myuser-sessid-abc123' -> ('myuser', 'abc123')
        'my-project-sessid-abc-123' -> ('my-project', 'abc-123')
        'myuser-sessid-' -> ('myuser', None)

    Args:
        raw_username: The raw username from Proxy-Authorization header.

    Returns:
        Tuple of (real_username, session_id). session_id is None if not present.
    """
    if SESSID_SEPARATOR in raw_username:
        username, sessid = raw_username.split(SESSID_SEPARATOR, 1)
        if username:
            return username, sessid if sessid else None
    return raw_username, None
