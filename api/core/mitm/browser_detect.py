# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Browser detection from User-Agent strings."""

from api.models.project import MitmBrowser

# Default browser impersonation when UA detection fails
DEFAULT_BROWSER = MitmBrowser.CHROME


def detect_browser(user_agent: str) -> MitmBrowser | None:
    """Detect browser family from User-Agent string.

    Returns a MitmBrowser enum value that can be used to select a matching
    TLS fingerprint impersonation profile, or None if the UA doesn't match
    any known browser pattern.

    Detection order matters: Edge UA contains "Chrome", Chrome UA contains "Safari".
    """
    if not user_agent or not user_agent.strip():
        return None

    if "Edg/" in user_agent:
        return MitmBrowser.EDGE
    if "Chrome/" in user_agent:
        return MitmBrowser.CHROME
    if "Firefox/" in user_agent:
        return MitmBrowser.FIREFOX
    if "Safari/" in user_agent:
        return MitmBrowser.SAFARI

    return None
