# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Abstract base class for MITM upstream relay strategies."""

from abc import ABC, abstractmethod


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
