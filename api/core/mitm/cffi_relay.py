# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""curl_cffi MITM relay with browser TLS fingerprint impersonation.

Uses curl_cffi (built on libcurl + BoringSSL) to make upstream requests
with browser-grade TLS fingerprints. The target server sees a genuine
Chrome/Firefox/Safari/Edge TLS fingerprint (JA3/JA4).
"""

import asyncio
from typing import Any

import structlog
from curl_cffi.requests import AsyncSession

from api.core.mitm.base import ImpersonationRelay
from api.models.project import MitmBrowser, MitmMode

logger = structlog.get_logger()


async def _close_replaced(session: AsyncSession[Any]) -> None:
    """Close a session swapped out mid-connection without failing the request.

    Shielded: curl_cffi's ``close()`` suppresses ``CancelledError`` while it
    waits for its timer task, so a cancel landing there (shutdown cancelling
    connections) would be lost and the connection would carry on serving.
    """
    try:
        await asyncio.shield(session.close())
    except Exception:
        logger.warning("Closing a replaced curl_cffi session failed", exc_info=True)


class CffiRelay(ImpersonationRelay):
    """Relay requests via curl_cffi with browser impersonation."""

    def __init__(self, mode: MitmMode, browser: MitmBrowser | None = None) -> None:
        super().__init__(mode, browser)
        self._session: AsyncSession[Any] | None = None
        self._current_impersonate: MitmBrowser | None = None

    async def _get_or_create_session(self, browser: MitmBrowser) -> AsyncSession[Any]:
        """Get existing session or create a new one if browser changed.

        The replaced session is closed. Garbage collection never reclaims
        it: curl_cffi runs a timer task per session that only ``close()``
        stops, so each dropped session would keep waking the event loop ten
        times a second for the life of the process. The new session is
        installed before that close is awaited, so the relay is never seen
        half-swapped.
        """
        session = self._session
        if session is None or browser != self._current_impersonate:
            replaced = session
            session = AsyncSession(impersonate=browser)  # type: ignore[arg-type]
            self._session, self._current_impersonate = session, browser
            if replaced is not None:
                await _close_replaced(replaced)
        return session

    async def send_request(
        self,
        method: str,
        url: str,
        headers: list[tuple[str, str]],
        body: bytes | None,
        proxy_url: str,
    ) -> tuple[int, str, list[tuple[str, str]], bytes]:
        """Send request via curl_cffi with browser impersonation."""
        forward_headers, browser = self._prepare_headers(headers)
        session = await self._get_or_create_session(browser)

        response = await session.request(
            method,  # type: ignore[arg-type]
            url,
            headers=forward_headers,
            data=body,
            proxy=proxy_url,
            allow_redirects=False,
            timeout=30,
        )

        # Drop content-encoding - curl_cffi auto-decompresses the body,
        # so forwarding the header would cause browsers to double-decompress.
        # Use multi_items() to preserve duplicate headers (e.g. Set-Cookie).
        response_headers = [
            (k, v) for k, v in response.headers.multi_items()
            if k.lower() != "content-encoding"
        ]
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
