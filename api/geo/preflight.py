# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Verify where a selected upstream really exits before forwarding a client's request.

A residential or mobile exit belongs to the *session*, not to the proxy row,
so the check runs against the proxy the request was routed to, right after
selection: one echo request through it, resolve the IP, compare with the
country the request *requires*: the ``-cc-`` country, else the country the
vendor promised for the proxy (a geo-targeted slot, a listed IP) or an
operator pinned by hand. A country attribution merely observed is not a
requirement: an untargeted residential slot may move countries freely, and
verifying it against where it happened to be last time would reject or
rotate it for a location nobody asked for. The verdict is
cached in Redis per (project, proxy) for the settings' session TTL, so a
session pays one extra round trip and the rest of its requests pay nothing.

This module only produces verdicts. What a mismatch does (forward anyway, try
another proxy, reject) is the proxy server's call, and what it does to the
proxy (flag a fixed exit, rotate a vendor session) is the proxy manager's.

Failures of the echo request itself never block traffic: an unreachable echo
endpoint is an operations problem, not evidence about the proxy.
"""

from __future__ import annotations

import json
from typing import NamedTuple

import structlog

from api.core.signals import exit_ip_changed
from api.db.redis import GEO_PREFLIGHT_KEY, RedisClient
from api.geo.models import (
    META_COUNTRY_SOURCE,
    IpObservation,
    ObservationSource,
    PreflightMode,
    normalize_country,
)
from api.geo.service import MANUAL_SOURCE, GeoService
from api.models.project import Project
from api.models.proxy import Proxy
from api.providers.sdk.strategies import META_COUNTRY, META_DISCOVERED_IP

logger = structlog.get_logger()


class PreflightVerdict(NamedTuple):
    ok: bool
    expected: str | None
    observed: str | None
    ip: str | None
    reason: str
    endpoint_country: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "expected": self.expected,
                "observed": self.observed,
                "ip": self.ip,
                "reason": self.reason,
                "endpoint_country": self.endpoint_country,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> PreflightVerdict | None:
        try:
            data = json.loads(raw)
            return cls(
                ok=bool(data["ok"]),
                expected=data.get("expected"),
                observed=data.get("observed"),
                ip=data.get("ip"),
                reason=str(data.get("reason", "")),
                endpoint_country=data.get("endpoint_country"),
            )
        except (TypeError, ValueError, KeyError):
            return None


SKIP = PreflightVerdict(ok=True, expected=None, observed=None, ip=None, reason="not applicable")


class PreflightChecker:
    """Runs and caches per-proxy exit verification."""

    def __init__(self, geo_service: GeoService, redis_client: RedisClient | None) -> None:
        self._geo_service = geo_service
        self._redis = redis_client
        self.checks = 0
        self.rejections = 0

    # --- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        """Listen for exits moving, so a cached verdict never outlives the exit it judged."""
        exit_ip_changed.connect(self._on_exit_ip_changed)

    def stop(self) -> None:
        exit_ip_changed.disconnect(self._on_exit_ip_changed)

    async def _on_exit_ip_changed(
        self, sender: object, proxy_id: str, project_id: str | None, **_: object
    ) -> None:
        if project_id:
            await self.invalidate(project_id, proxy_id)

    async def invalidate(self, project_id: str, proxy_id: str) -> None:
        """Drop the cached verdict for a proxy; the next request through it is verified again.

        The cache is keyed by the project owning the proxy, and the key is in
        Redis, so one instance noticing the change clears it for all of them.
        """
        if self._redis is None:
            return
        key = GEO_PREFLIGHT_KEY.format(project_id=project_id, proxy_id=proxy_id)
        try:
            await self._redis.client.delete(key)
        except Exception as exc:
            logger.debug("Could not drop cached preflight verdict", proxy_id=proxy_id, error=str(exc))

    @property
    def max_attempts(self) -> int:
        """Proxies to try under ``retry`` before giving up."""
        return self._geo_service.settings.preflight_max_attempts

    @staticmethod
    def expected_country(requested_country: str | None, proxy: Proxy) -> str | None:
        """The country the request requires, or None when nothing was asked or promised.

        The ``-cc-`` country wins. Otherwise the vendor's claim for the proxy
        (its listed country or the geo it was provisioned for) or a country
        an operator pinned by hand. The attributed country is never used: it
        is what was observed, not what was required.
        """
        requested = normalize_country(requested_country)
        if requested:
            return requested
        claimed = GeoService.claimed_country_of(proxy)
        if claimed:
            return claimed
        if proxy.metadata.get(META_COUNTRY_SOURCE) == MANUAL_SOURCE:
            return normalize_country(proxy.metadata.get(META_COUNTRY))
        return None

    @classmethod
    def applies(cls, project: Project, requested_country: str | None, proxy: Proxy) -> bool:
        """Whether this request needs a preflight at all."""
        if project.location_preflight == PreflightMode.OFF:
            return False
        return cls.expected_country(requested_country, proxy) is not None

    async def check(
        self,
        project: Project,
        proxy: Proxy,
        *,
        session_id: str | None,
        requested_country: str | None,
    ) -> PreflightVerdict:
        """Verify ``proxy``'s exit against the expected country.

        ``proxy`` must carry resolved credentials (the echo request goes
        through it). Returns a verdict; callers reject only when the project's
        mode is ``reject`` and ``ok`` is False.

        A verdict is cached per project and proxy for the session TTL, since a
        vendor session keeps its exit for about that long. The cache is
        dropped early when a health check or refresh sees the proxy exiting
        from a different IP (``exit_ip_changed``), so a vendor rotating the
        exit mid-session costs at most one health check interval of trust.
        """
        expected = self.expected_country(requested_country, proxy)
        if expected is None:
            return SKIP

        key = GEO_PREFLIGHT_KEY.format(project_id=project.id, proxy_id=proxy.id)
        cached = await self._cached(key)
        if cached is not None:
            return cached

        self.checks += 1
        ip, endpoint_country = await self._geo_service.discoverer().discover_with_country(
            proxy.url,
            log_context={"proxy_id": proxy.id, "project_id": project.id, "preflight": True},
        )
        if ip is None:
            verdict = PreflightVerdict(
                ok=True, expected=expected, observed=None, ip=None, reason="echo request failed"
            )
            # Short cache so a broken echo endpoint does not add a timeout to every request.
            await self._store(key, verdict, ttl=30)
            return verdict

        # The expected country is what is being verified, so it must not take
        # part in the answer: only the databases and the echo endpoint do.
        resolution = self._geo_service.resolve_ip(
            ip,
            policy=project.source_policy(self._geo_service.default_policy),
            endpoint_country=endpoint_country,
        )
        observed = resolution.country
        ok = observed is None or observed == expected
        reason = (
            "match" if ok and observed else ("location unknown" if observed is None else "mismatch")
        )
        verdict = PreflightVerdict(
            ok=ok,
            expected=expected,
            observed=observed,
            ip=ip,
            reason=reason,
            endpoint_country=normalize_country(endpoint_country),
        )

        self._geo_service.record(
            IpObservation(
                proxy_id=proxy.id,
                connector_id=proxy.connector_id,
                project_id=project.id,
                session_id=session_id,
                source=ObservationSource.PREFLIGHT,
                ip=ip,
                claimed_country=expected,
                endpoint_country=normalize_country(endpoint_country),
                resolved_country=observed,
                resolved_source=resolution.source,
                conflict=not ok,
                disagreement=resolution.disagreement,
                # A session exit the health checker has not seen yet is a hand-out
                # in its own right; re-verifying the exit it already knows is not.
                new_exit=proxy.metadata.get(META_DISCOVERED_IP) != ip,
                candidates=resolution.compact_candidates(),
                instance_id=self._geo_service._settings.instance_id,
            )
        )
        if not ok:
            self.rejections += 1
            logger.warning(
                "Preflight: exit location mismatch",
                project_id=project.id,
                proxy_id=proxy.id,
                expected=expected,
                observed=observed,
                ip=ip,
                mode=project.location_preflight.value,
            )
        await self._store(
            key, verdict, ttl=self._geo_service.settings.preflight_session_ttl_seconds
        )
        return verdict

    async def _cached(self, key: str) -> PreflightVerdict | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.client.get(key)
        except Exception:
            return None
        return PreflightVerdict.from_json(raw) if raw else None

    async def _store(self, key: str, verdict: PreflightVerdict, *, ttl: int) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.client.set(key, verdict.to_json(), ex=max(int(ttl), 1))
        except Exception as exc:
            logger.debug("Could not cache preflight verdict", error=str(exc))

    @staticmethod
    def rejection_message(verdict: PreflightVerdict) -> str:
        return (
            f"Exit location mismatch: requested {verdict.expected}, "
            f"observed {verdict.observed} ({verdict.ip})"
        )
