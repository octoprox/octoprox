# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Decides what to forward through when preflight disagrees with the selection.

The project's two location decisions say whether a verdict refuses the
request, and the routing strategy whether a refused one may move to another
eligible proxy, up to ``preflight_max_attempts``; what a mismatch does to the
proxy itself was decided by its sighting preflight, not here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog
from python_socks import ProxyConnectionError

from api.core.http_tunnel import TunnelRefusedError
from api.geo.attributor import ProxyAttributor
from api.geo.models import LocationRules
from api.geo.preflight import PreflightChecker, PreflightVerdict
from api.geo.readers import is_ip
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy
from api.providers.sdk.sources import UPSTREAM_ERRORS
from api.providers.sdk.strategies import META_DISCOVERED_IP, is_dynamic_gateway

if TYPE_CHECKING:
    from api.routing.router import Router

logger = structlog.get_logger()


class ExitSource(StrEnum):
    """Where the exit IP a client is told about came from."""

    # The echo ran for this request.
    PREFLIGHT = "preflight"
    # An earlier echo on the same session, within the verdict's TTL.
    CACHED = "cached"
    # The row's last recorded exit (discovery, a health check or an earlier preflight): no echo answered for the request.
    LAST_SEEN = "last-seen"


@dataclass(frozen=True)
class ExitIp:
    """The exit IP a request goes out through as far as Octoprox knows it, where that came from and when it was seen."""

    # Always a valid address: it goes into a header line, and echo endpoints and vendor lists supply it unchecked.
    ip: str
    source: ExitSource
    # Naive UTC; None when nothing says.
    checked_at: datetime | None = None

    @classmethod
    def of_verdict(cls, verdict: PreflightVerdict) -> ExitIp | None:
        """What the echo behind ``verdict`` saw; None when no echo answered with an IP."""
        if verdict.ip is None or not is_ip(verdict.ip):
            return None
        return cls(verdict.ip.strip(), ExitSource.CACHED if verdict.cached else ExitSource.PREFLIGHT, verdict.checked_at)

    @classmethod
    def last_seen(cls, proxy: Proxy) -> ExitIp | None:
        """The exit recorded on ``proxy``'s row; None when there is none, and on a dynamic gateway, whose exit is per request."""
        ip = proxy.metadata.get(META_DISCOVERED_IP)
        if is_dynamic_gateway(proxy) or not isinstance(ip, str) or not is_ip(ip):
            return None
        return cls(ip.strip(), ExitSource.LAST_SEEN, ProxyAttributor.checked_at(proxy))


@dataclass(frozen=True)
class ExitDecision:
    """The upstream to forward through, why the request is refused, or what the upstream failed its echo with."""

    # On a refusal, the proxy whose echo refused it.
    proxy: Proxy
    rejection: str | None = None
    # ``proxy`` failed the echo with it: the request fails as if its own connection had.
    upstream_error: BaseException | None = None
    # What preflight saw of the exit it lets the request through on; None when no echo answered.
    exit_ip: ExitIp | None = None

    @property
    def rejected(self) -> bool:
        return self.rejection is not None

    def known_exit(self) -> ExitIp | None:
        """The exit the request goes out through as far as Octoprox knows: preflight's answer, else the row's last recorded one."""
        return self.exit_ip or ExitIp.last_seen(self.proxy)


def _worth_retrying(error: BaseException) -> bool:
    """Whether another try could get past an upstream failing the echo: out of reach, silent, or a 5xx answer.

    A 4xx is given again by the same vendor (:func:`_refused_by_vendor`), and a SOCKS refusal is not retried.
    """
    if isinstance(error, TunnelRefusedError):
        return error.error_code is not None and error.error_code >= 500
    return isinstance(error, OSError)


def _refused_by_vendor(error: BaseException) -> bool:
    """Whether the vendor refused the echo with a 4xx: a place it cannot serve, the connector's credentials or rate limit.

    Its connector would give the same answer again; another connector may serve the request.
    """
    return isinstance(error, TunnelRefusedError) and error.error_code is not None and error.error_code < 500


def _upstream_at_fault(error: BaseException) -> bool:
    """Whether the failure can only be the upstream's: out of reach, or refusing our request with a 4xx.

    An upstream answers a CONNECT it could not carry through with a 5xx or not at all, and a SOCKS one names the
    target unreachable: our echo endpoint out of its reach looks the same as the upstream failing.
    """
    return _refused_by_vendor(error) or isinstance(error, ProxyConnectionError)


class ExitVerifier:
    """Runs preflight for a selected upstream and applies the project's mode."""

    def __init__(self, preflight_checker: PreflightChecker, router: Router) -> None:
        self._preflight_checker = preflight_checker
        self._router = router

    @staticmethod
    def applies(project: Project, location: LocationTarget | None, proxy: Proxy) -> bool:
        """Whether the request through ``proxy`` gets a preflight at all; :meth:`verify` passes any other straight on."""
        return PreflightChecker.applies(project, location, proxy)

    async def verify(
        self,
        project: Project,
        proxy: Proxy,
        *,
        session_id: str | None,
        location: LocationTarget | None,
        target_host: str | None,
    ) -> ExitDecision:
        """Return the proxy to forward through, a rejection, or the upstream's failure of the echo.

        ``session_id`` is the client's explicit ``-sessid-`` value, handed
        back to the selector on a retry so a dynamic-sessions row keeps the
        same vendor session.

        An upstream that fails the echo is tried past on another proxy where
        the strategy allows and another try could help (:func:`_worth_retrying`),
        or on another connector after its vendor's 4xx (:func:`_refused_by_vendor`).
        Then a failure that can only be its own (:func:`_upstream_at_fault`)
        fails the request with it; any other does so only where the project's
        rules could refuse, and the request goes through elsewhere, as after an
        echo that failed past the upstream. Such an echo checked nothing: it
        refuses the request wherever the rules could have refused on an
        answer, and lets it through elsewhere.
        """
        if not self.applies(project, location, proxy):
            return ExitDecision(proxy)
        rules = project.location_rules
        # A session-less request on a dynamic gateway retries by re-rendering:
        # selecting the same row again mints a fresh vendor session, so the
        # row is not excluded and the strategy's promise about rows holds.
        rerender = is_dynamic_gateway(proxy) and not session_id
        can_retry = rerender or self._router.strategy_for_project(project.id).allows_exit_reselection(session_id)
        excluded: set[str] = set()
        excluded_connectors: set[str] = set()
        attempts = 0
        while True:
            attempts += 1
            allowed = self._router.declared_countries(proxy.connector_id)
            upstream_error: BaseException | None = None
            try:
                verdict = await self._preflight_checker.check(
                    project, proxy, session_id=session_id, requested=location, allowed=allowed
                )
            except UPSTREAM_ERRORS as exc:
                upstream_error = exc
                logger.debug(
                    "Upstream failed the preflight echo",
                    proxy_id=proxy.id, project_id=project.id, error=str(exc), attempt=attempts,
                )
            except Exception as exc:
                logger.warning("Preflight check errored", proxy_id=proxy.id, error=str(exc))
                return ExitDecision(proxy)
            else:
                if not rerender and self._router.get_proxy(proxy.id) is None:
                    # The slot left the pool while it was being checked: its own
                    # sighting judged it out under ``refuse`` and preflight retired
                    # it, or the feed dropped it meanwhile. The request's own place
                    # may have matched, but the slot is gone: drop the verdict just
                    # cached for it and treat the pick as refused.
                    await self._preflight_checker.invalidate(project.id, proxy.id)
                    verdict = verdict._replace(ok=False, replaced=True)
                elif verdict.acceptable(rules, allowed):
                    return ExitDecision(proxy, exit_ip=ExitIp.of_verdict(verdict))
                logger.debug(
                    "Request refused for its exit location",
                    proxy_id=proxy.id,
                    project_id=project.id,
                    expected=verdict.expected.key if verdict.expected else None,
                    observed=verdict.observed.key if verdict.observed else None,
                    ip=verdict.ip,
                    replaced=verdict.replaced,
                    attempt=attempts,
                )
            refused = upstream_error is not None and _refused_by_vendor(upstream_error)
            if refused:
                excluded_connectors.add(proxy.connector_id)
            retry = can_retry and (upstream_error is None or refused or _worth_retrying(upstream_error))
            if retry and attempts < self._preflight_checker.max_attempts:
                if not rerender:
                    excluded.add(proxy.id)
                replacement = await self._router.select_proxy_for_project(
                    project.id, session_id, target_host, location,
                    exclude=frozenset(excluded), exclude_connectors=frozenset(excluded_connectors),
                )
                if replacement is not None:
                    logger.info(
                        "Preflight retrying with another proxy",
                        project_id=project.id, rejected=proxy.id, next=replacement.id, attempt=attempts + 1,
                    )
                    proxy = replacement
                    continue
            if upstream_error is not None:
                if _upstream_at_fault(upstream_error) or self._could_refuse(rules, location, proxy, allowed):
                    return ExitDecision(proxy, upstream_error=upstream_error)
                # Maybe only our echo endpoint out of the upstream's reach: as
                # an echo that failed past it, forwarded where nothing refuses.
                return ExitDecision(proxy)
            if verdict.replaced:
                return ExitDecision(proxy, "Proxy replaced while it was being checked")
            return ExitDecision(proxy, PreflightChecker.rejection_message(verdict, rules, allowed))

    @staticmethod
    def _could_refuse(rules: LocationRules, location: LocationTarget | None, proxy: Proxy, allowed: Sequence[str]) -> bool:
        """Whether the project's rules could refuse this request on an echo's answer."""
        expected, required_by = PreflightChecker.requirement(location, proxy)
        return expected is not None and PreflightVerdict.can_refuse(rules, expected, required_by, allowed)
