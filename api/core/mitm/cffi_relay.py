# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""curl_cffi MITM relay with browser TLS fingerprint impersonation.

Uses curl_cffi (built on libcurl + BoringSSL) to make upstream requests
with browser-grade TLS fingerprints. The target server sees a genuine
Chrome/Firefox/Safari/Edge TLS fingerprint (JA3/JA4).
"""

from typing import Any

import structlog
from curl_cffi.requests import AsyncSession

from api.core.mitm.base import ImpersonationRelay
from api.models.project import MitmBrowser, MitmMode

logger = structlog.get_logger()


class CffiRelay(ImpersonationRelay):
    """Relay requests via curl_cffi with browser impersonation."""

    def __init__(self, mode: MitmMode, browser: MitmBrowser | None = None) -> None:
        super().__init__(mode, browser)
        self._session: AsyncSession[Any] | None = None
        self._current_impersonate: MitmBrowser | None = None

    def _get_or_create_session(self, browser: MitmBrowser) -> AsyncSession[Any]:
        """Get existing session or create a new one if browser changed."""
        if self._session is None or browser != self._current_impersonate:
            if self._session is not None:
                # Can't await close here; old session will be garbage collected
                self._session = None
            self._session = AsyncSession(impersonate=browser)  # type: ignore[arg-type]
            self._current_impersonate = browser
        return self._session

    async def send_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        proxy_url: str,
    ) -> tuple[int, str, dict[str, str], bytes]:
        """Send request via curl_cffi with browser impersonation."""
        forward_headers, browser = self._prepare_headers(headers)
        session = self._get_or_create_session(browser)

        response = await session.request(
            method,  # type: ignore[arg-type]
            url,
            headers=forward_headers,
            data=body,
            proxy=proxy_url,
            allow_redirects=False,
            timeout=30,
        )

        response_headers = dict(response.headers)
        return (
            response.status_code,
            response.reason or "OK",
            response_headers,
            response.content,
        )

    async def close(self) -> None:
        """Close the curl_cffi session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
