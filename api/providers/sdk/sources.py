# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Data sources the provisioning strategies pull from.

* :class:`IpDiscoverer` learns a slot's exit IP by calling a discovery URL
  *through* the proxy (no credentials are sent to the discovery host).
* :class:`KnownIpsSource` asks the vendor API which exit IPs the account owns.
* :class:`ListSource` asks the vendor API for concrete proxy endpoints.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any

import httpcore
import httpx
import structlog
from httpx_socks import AsyncProxyTransport
from python_socks import ProxyError

from api.core.http_tunnel import TunnelTransport
from api.models.location import LocationTarget
from api.models.proxy import ProxyProtocol
from api.providers.sdk.descriptor import IpDiscoverySpec, KnownIpsSpec, ListSourceSpec
from api.providers.sdk.extract import ValueExtractor
from api.providers.sdk.http import HttpCallError, HttpCallExecutor
from api.providers.sdk.templating import RenderContext

logger = structlog.get_logger()

ProxiedClientFactory = Callable[[str, float], httpx.AsyncClient]

# What an upstream proxy failing an echo raises before the echo is sent: python-socks' errors, from a SOCKS upstream
# and from Octoprox's own CONNECT (a vendor's refusal among them), and OSError for one out of reach or silent
# (TimeoutError is one). httpx wraps every failure of its own. On a SOCKS upstream python-socks also runs the echo
# endpoint's TLS handshake, so a bare ssl.SSLError there is the endpoint's (``discover_with_place`` keeps it), and a
# timeout can be either.
UPSTREAM_ERRORS = (ProxyError, OSError)


@cache
def shared_ssl_context() -> ssl.SSLContext:
    """The TLS context every client that checks a proxy verifies with.

    httpx builds one per client by default, loading the whole CA bundle each
    time: milliseconds of CPU on the event loop for every preflight check,
    IP discovery and health check, which a busy instance runs many times a
    second. The settings are httpx's own defaults. A context is safe to share
    across clients and connections as long as none of them enables HTTP/2:
    httpcore writes each connection's ALPN list onto the context it is given.
    """
    return httpx.create_ssl_context()


@cache
def _proxy_ssl_context() -> ssl.SSLContext:
    """What httpcore verifies an https:// proxy with when given no context: the system CAs plus certifi.

    Not ``shared_ssl_context``, which trusts certifi alone: a proxy whose
    certificate chains to a CA in the system store would stop verifying.
    """
    return httpcore.default_ssl_context()


def httpx_proxy(proxy_url: str) -> httpx.Proxy:
    """An HTTP(S) proxy URL for httpx, an https:// one verified with one shared context.

    httpcore would otherwise build an https:// proxy a fresh context per
    connection. An http:// proxy takes none: there is no TLS to the proxy.
    """
    ssl_context = _proxy_ssl_context() if httpx.URL(proxy_url).scheme == "https" else None
    return httpx.Proxy(proxy_url, ssl_context=ssl_context)


def default_proxied_client_factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
    """Client that sends every request through ``proxy_url`` (HTTP or SOCKS)."""
    context = shared_ssl_context()
    if proxy_url.startswith(("socks4://", "socks5://")):
        return httpx.AsyncClient(
            transport=AsyncProxyTransport.from_url(proxy_url, rdns=True, verify=context), timeout=timeout
        )
    return httpx.AsyncClient(proxy=httpx_proxy(proxy_url), verify=context, timeout=timeout)


def echo_client_factory(proxy_url: str, timeout: float) -> httpx.AsyncClient:
    """Client for an echo through ``proxy_url``: an https:// echo through an http:// proxy goes over Octoprox's own CONNECT.

    So a vendor refusing the echo's tunnel is worded as it is on a client's own request, with what the vendor said.
    Any other pair goes as :func:`default_proxied_client_factory` sends it.
    """
    if not proxy_url.startswith("http://"):
        return default_proxied_client_factory(proxy_url, timeout)
    context = shared_ssl_context()
    return httpx.AsyncClient(
        proxy=httpx_proxy(proxy_url),
        mounts={"https://": TunnelTransport(proxy_url, context)},
        verify=context,
        timeout=timeout,
    )


def extract_ip_and_place(
    response: httpx.Response,
    ip_path: str,
    country_path: str | None = None,
    state_path: str | None = None,
    city_path: str | None = None,
    extractor: ValueExtractor | None = None,
) -> tuple[str | None, LocationTarget | None]:
    """Pull ``(ip, place)`` out of an echo endpoint's response.

    ``ip_path`` is a JMESPath into the JSON body, or ``"@text"`` for a plain
    text body (httpbin-style ``origin`` values may list several addresses;
    the first is the client). The place holds whatever of country, state and
    city the declared paths yield, normalised; None when none do.
    """
    values = extractor or ValueExtractor()
    if ip_path == "@text":
        text = response.text.strip()
        return (_first_ip(text) or None), None
    try:
        document = response.json()
    except ValueError:
        return None, None
    value = values.extract_str(ip_path, document)
    ip = _first_ip(value) if value else None
    reported = [values.extract_str(path, document) if path else None for path in (country_path, state_path, city_path)]
    return ip, LocationTarget.reported(*reported)


def _first_ip(value: str) -> str:
    return value.split(",", 1)[0].strip()


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
        self._client_factory = client_factory or echo_client_factory

    async def discover_with_place(
        self, proxy_url: str, *, log_context: dict[str, Any] | None = None, raise_upstream: bool = False
    ) -> tuple[str | None, LocationTarget | None]:
        """Return ``(ip, place)``; ip is ``None`` on failure, place is None when nothing was reported.

        The place holds what the spec's ``country_path``, ``state_path`` and
        ``city_path`` yield.

        Args:
            raise_upstream: raise the upstream proxy failing the echo (:data:`UPSTREAM_ERRORS`) instead of returning
                no IP, for preflight, whose request through it would fail the same way.
        """
        context = log_context or {}
        # A failed preflight echo is its request's failure or refusal, which
        # the Failures page counts; at one echo per request a warning each
        # would flood the log.
        log_failure = logger.debug if context.get("preflight") else logger.warning
        try:
            async with self._client_factory(proxy_url, self._spec.timeout_seconds) as client:
                response = await client.get(self._spec.url)
        except (httpx.HTTPError, *UPSTREAM_ERRORS) as exc:
            if raise_upstream and isinstance(exc, UPSTREAM_ERRORS) and not isinstance(exc, ssl.SSLError):
                raise
            if isinstance(exc, httpx.TimeoutException | TimeoutError):
                log_failure("IP discovery timed out", **context)
            else:
                log_failure("IP discovery request failed", error=str(exc), **context)
            return None, None
        if response.status_code != 200:
            log_failure("IP discovery returned an error", status_code=response.status_code, **context)
            return None, None
        ip, place = self._extract(response)
        if not ip:
            log_failure("IP discovery response had no IP", **context)
        return ip, place

    def _extract(self, response: httpx.Response) -> tuple[str | None, LocationTarget | None]:
        spec = self._spec
        return extract_ip_and_place(
            response, spec.ip_path, spec.country_path, spec.state_path, spec.city_path, self._extractor
        )


@dataclass(frozen=True)
class KnownIp:
    ip: str
    # What the vendor says about the IP's place, when its list says anything.
    place: LocationTarget | None


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
            entries.append(KnownIp(ip=ip, place=self._place_of(item)))
        return entries

    def _place_of(self, item: Any) -> LocationTarget | None:
        spec = self._spec
        return LocationTarget.reported(
            self._extractor.extract_str(spec.country, item) if spec.country else None,
            self._extractor.extract_str(spec.state, item) if spec.state else None,
            self._extractor.extract_str(spec.city, item) if spec.city else None,
        )


@dataclass(frozen=True)
class ListedProxy:
    """One endpoint returned by a list-mode source."""

    identity: str
    host: str
    port: int
    username: str | None
    password: str | None
    protocol: ProxyProtocol
    # What the vendor's list says about the exit's place, when it says anything.
    place: LocationTarget | None
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
                    place=LocationTarget.reported(
                        self._extractor.extract_str(self._spec.country, item) if self._spec.country else None,
                        self._extractor.extract_str(self._spec.state, item) if self._spec.state else None,
                        self._extractor.extract_str(self._spec.city, item) if self._spec.city else None,
                    ),
                    raw=item if isinstance(item, dict) else {},
                )
            )
        return proxies
