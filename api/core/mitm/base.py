# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Abstract base class for MITM upstream relay strategies."""

import random
from abc import ABC, abstractmethod

from api.core.mitm.browser_detect import DEFAULT_BROWSER, detect_browser
from api.models.project import MitmBrowser, MitmMode

# Concrete browsers eligible for random selection
_CONCRETE_BROWSERS: list[MitmBrowser] = [
    MitmBrowser.CHROME,
    MitmBrowser.FIREFOX,
    MitmBrowser.SAFARI,
    MitmBrowser.EDGE,
]


class MitmRelay(ABC):
    """Interface for MITM upstream relay strategies.

    Each relay implementation handles connecting to the target server
    (through the upstream proxy) and forwarding HTTP requests/responses.
    The relay determines the TLS fingerprint the target server sees.
    """

    @abstractmethod
    async def send_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        proxy_url: str,
    ) -> tuple[int, str, dict[str, str], bytes]:
        """Send an HTTP request upstream and return the response.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Full target URL (e.g. https://example.com/path)
            headers: HTTP headers to forward
            body: Request body bytes, or None
            proxy_url: Upstream proxy URL

        Returns:
            Tuple of (status_code, reason_phrase, response_headers, response_body).
        """

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources (connections, sessions)."""


class ImpersonationRelay(MitmRelay):
    """Base for relays that impersonate a browser TLS fingerprint.

    Handles the shared header preparation logic: stripping the host
    header (HTTP/2 uses :authority from the URL), and selecting the
    browser profile based on mode (match_ua vs override_ua).
    """

    def __init__(self, mode: MitmMode, browser: MitmBrowser | None = None) -> None:
        self._mode = mode
        self._configured_browser = browser or DEFAULT_BROWSER

    def _prepare_headers(
        self, headers: dict[str, str],
    ) -> tuple[dict[str, str], MitmBrowser]:
        """Prepare headers for forwarding and select the browser profile.

        Strips the host header (HTTP/2 engines set :authority from the URL).
        In override_ua mode, removes User-Agent so the engine sets its own.
        In match_ua mode, detects the browser from User-Agent; if unrecognized,
        removes it and falls back to the default browser.

        Returns:
            Tuple of (forward_headers, browser).
        """
        # Strip host — the engine sets :authority from the URL in HTTP/2
        forward_headers = {
            k: v for k, v in headers.items()
            if k.lower() != "host"
        }

        if self._mode == MitmMode.OVERRIDE_UA:
            # Remove User-Agent so engine sets its own consistent one
            forward_headers = {
                k: v for k, v in forward_headers.items()
                if k.lower() != "user-agent"
            }
            browser = self._configured_browser
            if browser == MitmBrowser.RANDOM:
                browser = random.choice(_CONCRETE_BROWSERS)  # noqa: S311
        else:
            # match_ua: detect browser from client's User-Agent
            user_agent = headers.get("User-Agent", headers.get("user-agent", ""))
            detected = detect_browser(user_agent)
            browser = detected if detected is not None else DEFAULT_BROWSER
            if detected is None:
                # UA doesn't match any known browser — remove it so the
                # engine sets a consistent one for the impersonation profile
                forward_headers = {
                    k: v for k, v in forward_headers.items()
                    if k.lower() != "user-agent"
                }

        return forward_headers, browser
