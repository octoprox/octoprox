# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Decides what to forward through when preflight disagrees with the selection.

The project's two location decisions say whether a verdict refuses the
request, and the routing strategy whether a refused one may move to another
eligible proxy, up to ``preflight_max_attempts``; what a mismatch does to the
proxy itself was decided by its sighting preflight, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from api.geo.preflight import PreflightChecker
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy
from api.providers.sdk.strategies import is_dynamic_gateway

if TYPE_CHECKING:
    from api.routing.router import Router

logger = structlog.get_logger()


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

    def __init__(self, preflight_checker: PreflightChecker, router: Router) -> None:
        self._preflight_checker = preflight_checker
        self._router = router

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
        rules = project.location_rules
        # A session-less request on a dynamic gateway retries by re-rendering:
        # selecting the same row again mints a fresh vendor session, so the
        # row is not excluded and the strategy's promise about rows holds.
        rerender = is_dynamic_gateway(proxy) and not session_id
        can_retry = rerender or self._router.strategy_for_project(project.id).allows_exit_reselection(session_id)
        excluded: set[str] = set()
        attempts = 0
        while True:
            attempts += 1
            allowed = self._router.declared_countries(proxy.connector_id)
            try:
                verdict = await self._preflight_checker.check(
                    project, proxy, session_id=session_id, requested=location, allowed=allowed
                )
            except Exception as exc:
                logger.warning("Preflight check errored", proxy_id=proxy.id, error=str(exc))
                return ExitDecision(proxy)
            if not rerender and self._router.get_proxy(proxy.id) is None:
                # The slot left the pool while it was being checked: its own
                # sighting judged it out under ``refuse`` and preflight retired
                # it, or the feed dropped it meanwhile. The request's own place
                # may have matched, but the slot is gone: drop the verdict just
                # cached for it and treat the pick as refused.
                await self._preflight_checker.invalidate(project.id, proxy.id)
                verdict = verdict._replace(ok=False, replaced=True)
            elif verdict.acceptable(rules, allowed):
                return ExitDecision(proxy)
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
            if can_retry and attempts < self._preflight_checker.max_attempts:
                if not rerender:
                    excluded.add(proxy.id)
                replacement = await self._router.select_proxy_for_project(
                    project.id, session_id, target_host, location, exclude=frozenset(excluded)
                )
                if replacement is not None:
                    logger.info(
                        "Preflight retrying with another proxy",
                        project_id=project.id, rejected=proxy.id, next=replacement.id, attempt=attempts + 1,
                    )
                    proxy = replacement
                    continue
            if verdict.replaced:
                return ExitDecision(None, "Proxy replaced while it was being checked")
            return ExitDecision(None, PreflightChecker.rejection_message(verdict, rules, allowed))
