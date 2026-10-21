# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""curl_cffi MITM relay with browser TLS fingerprint impersonation.

Uses curl_cffi (built on libcurl + BoringSSL) to make upstream requests
with browser-grade TLS fingerprints. The target server sees a genuine
Chrome/Firefox/Safari/Edge TLS fingerprint (JA3/JA4).
"""

import asyncio
import re
from typing import Any

import certifi
import structlog
from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from api.core.mitm.base import ImpersonationRelay, UpstreamRefusedError
from api.models.project import MitmBrowser, MitmMode

logger = structlog.get_logger()

# libcurl's words when the upstream proxy answers the CONNECT with anything
# but a 2xx; the status is in the text only. Since libcurl 8.20 the error
# code is COULDNT_CONNECT, which curl_cffi raises as a ConnectionError (a
# ProxyError before), so any request error is checked for them.
_TUNNEL_REFUSED = re.compile(r"CONNECT tunnel failed, response (\d{3})")


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
            # Sites and https:// proxies are verified against certifi's CA
            # bundle plus the certificate directory libcurl was built with
            # (/etc/ssl/certs in the image, Debian's store), which is what
            # curl_cffi trusted before 0.15. Since 0.15 it takes the OS bundle
            # as its CA file instead; naming certifi's keeps 5.x's trust.
            # libcurl has no clean way to drop the directory, so a private CA
            # added to the OS store is trusted too. Curl options are applied
            # last, so neither REQUESTS_CA_BUNDLE nor CURL_CA_BUNDLE replaces
            # certifi's bundle.
            ca_bundle = certifi.where()
            session = AsyncSession(
                impersonate=browser, curl_options={CurlOpt.CAINFO: ca_bundle, CurlOpt.PROXY_CAINFO: ca_bundle}
            )
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

        try:
            response = await session.request(
                method,  # type: ignore[arg-type]
                url,
                headers=forward_headers,
                data=body,
                proxy=proxy_url,
                allow_redirects=False,
                timeout=30,
            )
        except RequestException as e:
            refused = _TUNNEL_REFUSED.search(str(e))
            if refused is None:
                raise
            raise UpstreamRefusedError(int(refused[1])) from e

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
