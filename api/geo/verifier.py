# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Decides what to forward through when preflight disagrees with the selection.

The proxy server hands the verifier the upstream it picked; the verifier
returns the upstream to use or a rejection. Whether a verdict refuses the
request is the project's assurance level (``observe`` never refuses).
Whether a refused request may move is the routing strategy's call: the
verifier asks the :class:`ProxySelector` for another eligible proxy,
excluding the ones that failed, up to ``preflight_max_attempts``, unless the
strategy says the session must stay put (sticky sessions were promised one
exit), in which case the request fails at once. What a mismatch does *to*
the proxy is not decided here: the verifier emits ``exit_location_mismatch``
and the attributor reacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import structlog

from api.core.event_bus import event_bus
from api.core.signals import exit_location_mismatch
from api.geo.preflight import PreflightChecker
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy
from api.providers.sdk.strategies import is_dynamic_gateway
from api.strategies.base import RoutingStrategy

logger = structlog.get_logger()


class ProxySelector(Protocol):
    """What the verifier needs from routing."""

    async def select_proxy_for_project(
        self,
        project_id: str,
        session_id: str | None = None,
        target_host: str | None = None,
        location: LocationTarget | None = None,
        exclude: frozenset[str] | None = None,
    ) -> Proxy | None: ...

    def strategy_for_project(self, project_id: str) -> RoutingStrategy: ...


@dataclass(frozen=True)
class ExitDecision:
    """The upstream to forward through, or why the request is refused."""

    proxy: Proxy | None
    rejection: str | None = None

    @property
    def rejected(self) -> bool:
        return self.rejection is not None


class ExitVerifier:
    """Runs preflight for a selected upstream and applies the project's mode."""

    def __init__(self, preflight_checker: PreflightChecker, proxy_selector: ProxySelector) -> None:
        self._preflight_checker = preflight_checker
        self._proxy_selector = proxy_selector

    async def verify(
        self,
        project: Project,
        proxy: Proxy,
        *,
        session_id: str | None,
        location: LocationTarget | None,
        target_host: str | None,
    ) -> ExitDecision:
        """Return the proxy to forward through, or a rejection.

        ``session_id`` is the client's explicit ``-sessid-`` value, handed
        back to the selector on a retry so a dynamic-sessions row keeps the
        same vendor session.

        An echo outage never refuses anything: a verdict without an exit lets
        the request through under every level.
        """
        if not PreflightChecker.applies(project, location, proxy):
            return ExitDecision(proxy)
        assurance = project.location_assurance
        # A session-less request on a dynamic gateway retries by re-rendering:
        # selecting the same row again mints a fresh vendor session, so the
        # row is not excluded and the strategy's promise about rows holds.
        rerender = is_dynamic_gateway(proxy) and not session_id
        can_retry = rerender or self._proxy_selector.strategy_for_project(project.id).allows_exit_reselection(session_id)
        excluded: set[str] = set()
        attempts = 0
        while True:
            attempts += 1
            try:
                verdict = await self._preflight_checker.check(project, proxy, session_id=session_id, requested=location)
            except Exception as exc:
                logger.warning("Preflight check errored", proxy_id=proxy.id, error=str(exc))
                return ExitDecision(proxy)
            if verdict.acceptable(assurance):
                return ExitDecision(proxy)
            if not verdict.ok:
                # A contradicted exit: the attributor rotates a pooled vendor
                # session. An open one (refused under ``confirmed``) is not a
                # contradiction, and rotating on missing evidence would churn a
                # pool the databases do not cover for sessions just as unknown.
                await event_bus.publish(
                    exit_location_mismatch,
                    self,
                    proxy_id=proxy.id,
                    project_id=project.id,
                    expected=verdict.expected.key if verdict.expected else "",
                    observed=verdict.observed.key if verdict.observed else None,
                    ip=verdict.ip,
                )
            if can_retry and attempts < self._preflight_checker.max_attempts:
                if not rerender:
                    excluded.add(proxy.id)
                replacement = await self._proxy_selector.select_proxy_for_project(
                    project.id, session_id, target_host, location, exclude=frozenset(excluded)
                )
                if replacement is not None:
                    logger.info(
                        "Preflight retrying with another proxy",
                        project_id=project.id, rejected=proxy.id, next=replacement.id, attempt=attempts + 1,
                    )
                    proxy = replacement
                    continue
            return ExitDecision(None, PreflightChecker.rejection_message(verdict))
