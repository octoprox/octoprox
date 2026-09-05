# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Data sources the provisioning strategies pull from.

* :class:`IpDiscoverer` learns a slot's exit IP by calling a discovery URL
  *through* the proxy (no credentials are sent to the discovery host).
* :class:`KnownIpsSource` asks the vendor API which exit IPs the account owns.
* :class:`ListSource` asks the vendor API for concrete proxy endpoints.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from api.models.proxy import ProxyProtocol
from api.providers.sdk.descriptor import IpDiscoverySpec, KnownIpsSpec, ListSourceSpec
from api.providers.sdk.extract import ValueExtractor
from api.providers.sdk.http import HttpCallError, HttpCallExecutor
from api.providers.sdk.templating import RenderContext

logger = structlog.get_logger()

ProxiedClientFactory = Callable[[str, float], httpx.AsyncClient]


def default_proxied_client_factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(proxy=proxy_url, timeout=timeout)


class IpDiscoverer:
    """Discovers the exit IP behind a proxy URL."""

    def __init__(
        self,
        spec: IpDiscoverySpec,
        extractor: ValueExtractor | None = None,
        client_factory: ProxiedClientFactory | None = None,
    ) -> None:
        self._spec = spec
        self._extractor = extractor or ValueExtractor()
        self._client_factory = client_factory or default_proxied_client_factory

    async def discover(self, proxy_url: str, *, log_context: dict[str, Any] | None = None) -> str | None:
        """Return the discovered IP, or ``None`` when the request fails."""
        context = log_context or {}
        try:
            async with self._client_factory(proxy_url, self._spec.timeout_seconds) as client:
                response = await client.get(self._spec.url)
        except httpx.TimeoutException:
            logger.warning("IP discovery timed out", **context)
            return None
        except httpx.HTTPError as exc:
            logger.warning("IP discovery request failed", error=str(exc), **context)
            return None
        if response.status_code != 200:
            logger.warning("IP discovery returned an error", status_code=response.status_code, **context)
            return None
        ip = self._extract_ip(response)
        if not ip:
            logger.warning("IP discovery response had no IP", **context)
        return ip

    def _extract_ip(self, response: httpx.Response) -> str | None:
        if self._spec.ip_path == "@text":
            text = response.text.strip()
            return text or None
        try:
            document = response.json()
        except ValueError:
            return None
        value = self._extractor.extract_str(self._spec.ip_path, document)
        return value.strip() if value else None


@dataclass(frozen=True)
class KnownIp:
    ip: str
    country: str


class KnownIpsSource:
    """Vendor-provided list of exit IPs for port mode."""

    def __init__(self, spec: KnownIpsSpec, executor: HttpCallExecutor, extractor: ValueExtractor) -> None:
        self._spec = spec
        self._executor = executor
        self._extractor = extractor

    async def fetch(self, ctx: RenderContext) -> list[KnownIp] | None:
        """Return the account's IPs, or ``None`` when the API call failed."""
        try:
            result = await self._executor.execute(self._spec.call, ctx)
        except HttpCallError as exc:
            logger.warning("Known-IP list unavailable", error=str(exc))
            return None
        entries: list[KnownIp] = []
        for item in result.items(self._extractor, self._spec.items):
            ip = self._extractor.extract_str(self._spec.ip, item)
            if not ip:
                continue
            country = self._extractor.extract_str(self._spec.country, item) or ""
            entries.append(KnownIp(ip=ip, country=country))
        return entries


@dataclass(frozen=True)
class ListedProxy:
    """One endpoint returned by a list-mode source."""

    identity: str
    host: str
    port: int
    username: str | None
    password: str | None
    protocol: ProxyProtocol
    country: str
    raw: dict[str, Any]


class ListSource:
    """Vendor API returning concrete proxy endpoints."""

    def __init__(self, spec: ListSourceSpec, executor: HttpCallExecutor, extractor: ValueExtractor) -> None:
        self._spec = spec
        self._executor = executor
        self._extractor = extractor

    async def fetch(self, ctx: RenderContext) -> list[ListedProxy]:
        """Return the listed proxies; raises :class:`HttpCallError` on failure."""
        result = await self._executor.execute(self._spec.call, ctx)
        proxies: list[ListedProxy] = []
        seen: set[str] = set()
        for item in result.items(self._extractor, self._spec.items):
            if not self._extractor.truthy(self._spec.filter, item):
                continue
            host = self._extractor.extract_str(self._spec.host, item)
            port_text = self._extractor.extract_str(self._spec.port, item)
            if not host or not port_text:
                continue
            try:
                port = int(float(port_text))
            except ValueError:
                continue
            identity = self._extractor.extract_str(self._spec.identity, item) or f"{host}:{port}"
            if identity in seen:
                continue
            seen.add(identity)
            protocol_text = (self._extractor.extract_str(self._spec.protocol, item) or "http").lower()
            try:
                protocol = ProxyProtocol(protocol_text)
            except ValueError:
                protocol = ProxyProtocol.HTTP
            proxies.append(
                ListedProxy(
                    identity=identity,
                    host=host,
                    port=port,
                    username=self._extractor.extract_str(self._spec.username, item),
                    password=self._extractor.extract_str(self._spec.password, item),
                    protocol=protocol,
                    country=(self._extractor.extract_str(self._spec.country, item) or ""),
                    raw=item if isinstance(item, dict) else {},
                )
            )
        return proxies
