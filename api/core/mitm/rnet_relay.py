# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""rnet MITM relay with browser TLS fingerprint impersonation.

Uses rnet (built on Rust wreq + BoringSSL) to make upstream requests
with browser-grade TLS fingerprints. Supports 40+ browser profiles
with genuine JA3/JA4 fingerprints.
"""

import structlog
from rnet import Client, Impersonate, Method

from api.core.mitm.base import ImpersonationRelay
from api.core.mitm.browser_detect import DEFAULT_BROWSER
from api.models.project import MitmBrowser, MitmMode

logger = structlog.get_logger()

# Map browser names to rnet Impersonate enum values
_IMPERSONATE_MAP: dict[MitmBrowser, Impersonate] = {
    MitmBrowser.CHROME: Impersonate.Chrome137,
    MitmBrowser.FIREFOX: Impersonate.Firefox139,
    MitmBrowser.SAFARI: Impersonate.Safari18,
    MitmBrowser.EDGE: Impersonate.Edge134,
}

# Map HTTP method strings to rnet Method enum
_METHOD_MAP: dict[str, Method] = {
    "GET": Method.GET,
    "POST": Method.POST,
    "PUT": Method.PUT,
    "DELETE": Method.DELETE,
    "HEAD": Method.HEAD,
    "OPTIONS": Method.OPTIONS,
    "PATCH": Method.PATCH,
    "TRACE": Method.TRACE,
}


class RnetRelay(ImpersonationRelay):
    """Relay requests via rnet with browser impersonation."""

    def __init__(self, mode: MitmMode, browser: MitmBrowser | None = None) -> None:
        super().__init__(mode, browser)
        self._client: Client | None = None
        self._current_impersonate: MitmBrowser | None = None

    def _get_or_create_client(self, browser: MitmBrowser) -> Client:
        """Get existing client or create a new one if browser changed."""
        if self._client is None or browser != self._current_impersonate:
            self._client = None
            profile = _IMPERSONATE_MAP.get(browser, _IMPERSONATE_MAP[DEFAULT_BROWSER])
            self._client = Client(impersonate=profile)
            self._current_impersonate = browser
        return self._client

    async def send_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        proxy_url: str,
    ) -> tuple[int, str, dict[str, str], bytes]:
        """Send request via rnet with browser impersonation."""
        forward_headers, browser = self._prepare_headers(headers)
        client = self._get_or_create_client(browser)

        rnet_method = _METHOD_MAP.get(method.upper(), Method.GET)
        kwargs: dict[str, object] = {
            "headers": forward_headers,
            "proxy": proxy_url,
        }
        if body is not None:
            kwargs["body"] = body
        response = await client.request(rnet_method, url, **kwargs)  # type: ignore[arg-type]

        # rnet response.status is already int
        response_body = await response.bytes()
        # rnet headers may contain bytes keys/values — decode to str.
        # Drop content-encoding — rnet auto-decompresses the body,
        # so forwarding the header would cause browsers to double-decompress.
        raw_items: list[tuple[object, object]] = (
            list(response.headers.items()) if response.headers else []  # type: ignore[arg-type]
        )
        response_headers: dict[str, str] = {
            (k.decode("latin-1") if isinstance(k, bytes) else str(k)):
            (v.decode("latin-1") if isinstance(v, bytes) else str(v))
            for k, v in raw_items
            if (k.decode("latin-1") if isinstance(k, bytes) else str(k)).lower()
            != "content-encoding"
        }

        return response.status, "OK", response_headers, response_body

    async def close(self) -> None:
        """Clean up the rnet client."""
        self._client = None
