# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Verify where a selected upstream really exits before forwarding a client's request.

A residential or mobile exit belongs to the *session*, not to the proxy row,
so the check runs against the proxy the request was routed to, right after
selection: one echo request through it, resolve the IP, compare with the
location the request *requires*: the ``-cc-``, ``-st-`` and ``-city-`` values,
else the country the vendor promised for the slot (a geo-targeted session)
or an operator pinned by hand. A location merely observed is not a
requirement: an untargeted residential slot may move countries freely, and
verifying it against where it happened to be last time would reject or
rotate it for a location nobody asked for. The verdict is
cached in Redis per (project, proxy) for the settings' session TTL, so a
session pays one extra round trip and the rest of its requests pay nothing.

Only session-bound rows are checked: dynamic-sessions gateways and pooled
session slots, whose exit the vendor can move. A fixed exit (static, ISP,
datacenter, list) was placed by discovery and is re-read by every health
check that reports its IP, so an echo per request would only repeat what
attribution already knows.

A dynamic-sessions gateway row is different: its exit belongs to the vendor
session rendered for the request, so the verdict is cached per (project,
proxy, vendor session) when the client named a session, and a request
without one is echoed only when sampled (the connector's ``exit_sample_percent``),
uncached, with nothing to verify unless it asked for a location. Those
sightings exist for provider accuracy and unique exits: nothing but an echo
ever sees where a dynamic request went.

This module only produces verdicts. What a mismatch does (forward anyway, try
another proxy, reject) is the proxy server's call, and what it does to the
proxy (rotate a vendor session) is the proxy manager's.

Failures of the echo request itself never block traffic: an unreachable echo
endpoint is an operations problem, not evidence about the proxy.
"""

from __future__ import annotations

import json
import random
from typing import NamedTuple

import structlog

from api.core.signals import exit_ip_changed
from api.db.redis import GEO_PREFLIGHT_KEY, RedisClient
from api.geo.models import (
    META_COUNTRY_SOURCE,
    IpObservation,
    ObservationSource,
    PreflightMode,
)
from api.geo.service import MANUAL_SOURCE, GeoService
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy
from api.providers.sdk.descriptor import DEFAULT_EXIT_SAMPLE_PERCENT
from api.providers.sdk.strategies import (
    META_COUNTRY,
    META_EXIT_SAMPLE_PERCENT,
    META_GEO_CITY,
    META_GEO_STATE,
    META_SESSION_ID,
    is_dynamic_gateway,
)

logger = structlog.get_logger()


class PreflightVerdict(NamedTuple):
    """What one echo through the selected upstream found.

    ``expected`` is the location the request required and ``observed`` where
    attribution placed the exit at those levels (country always, state and
    city when asked). One verdict per level, with the polarity of an
    observation's ``conflict``: True contradicted, False confirmed, None when
    nothing was asked at that level or nothing was observed there. ``ok`` is
    False only for a confirmed mismatch at some level: an unknown answer
    never fails the check.
    """

    ok: bool
    expected: LocationTarget | None
    observed: LocationTarget | None
    ip: str | None
    reason: str
    endpoint_country: str | None = None
    country_conflict: bool | None = None
    state_conflict: bool | None = None
    city_conflict: bool | None = None

    @property
    def failed_levels(self) -> tuple[str, ...]:
        """The levels that were contradicted, most general first."""
        return tuple(
            level
            for level, conflict in (
                ("country", self.country_conflict), ("state", self.state_conflict), ("city", self.city_conflict)
            )
            if conflict
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "expected": self.expected.to_dict() if self.expected else None,
                "observed": self.observed.to_dict() if self.observed else None,
                "ip": self.ip,
                "reason": self.reason,
                "endpoint_country": self.endpoint_country,
                "country_conflict": self.country_conflict,
                "state_conflict": self.state_conflict,
                "city_conflict": self.city_conflict,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> PreflightVerdict | None:
        try:
            data = json.loads(raw)
            expected = data.get("expected")
            observed = data.get("observed")
            if isinstance(expected, str) or isinstance(observed, str):
                # A verdict cached by a previous version: unreadable, so a miss and verified again.
                return None
            return cls(
                ok=bool(data["ok"]),
                expected=LocationTarget.from_dict(expected),
                observed=LocationTarget.from_dict(observed),
                ip=data.get("ip"),
                reason=str(data.get("reason", "")),
                endpoint_country=data.get("endpoint_country"),
                country_conflict=data.get("country_conflict"),
                state_conflict=data.get("state_conflict"),
                city_conflict=data.get("city_conflict"),
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

    # --- what a request requires ---------------------------------------------------

    @staticmethod
    def expected_location(requested: LocationTarget | None, proxy: Proxy) -> LocationTarget | None:
        """The location the request requires, or None when nothing was asked or promised.

        The request's own ``-cc-``, ``-st-`` and ``-city-`` win. Without a
        country the vendor's claim for the proxy (its listed country or the
        geo it was provisioned for or rendered with) or a country an operator
        pinned by hand stands in; a state or city rendered into a dynamic
        request (``geo_state``, ``geo_city``) counts the same way. The
        attributed location is never used: it is what was observed, not what
        was required.
        """
        target = requested or None
        # The parser keeps a -cc- value that is not a country code as typed so
        # it matches no connector; it must not become the claim either, since
        # the observation stores the claim as a two-letter code.
        country = target.country if target else None
        if country is None:
            country = GeoService.claimed_country_of(proxy)
        if country is None and proxy.metadata.get(META_COUNTRY_SOURCE) == MANUAL_SOURCE:
            country = proxy.metadata.get(META_COUNTRY) or None
        state = (target.state if target else None) or proxy.metadata.get(META_GEO_STATE) or None
        city = (target.city if target else None) or proxy.metadata.get(META_GEO_CITY) or None
        if country is None:
            # A state or city is meaningless without the country; nothing to verify.
            return None
        return LocationTarget(country=country, state=state, city=city)

    @classmethod
    def applies(cls, project: Project, requested: LocationTarget | None, proxy: Proxy) -> bool:
        """Whether this request needs a preflight at all.

        Only rows whose exit the vendor can move are checked. A dynamic row
        always applies once preflight is on: even with nothing to verify its
        requests are sampled for attribution (``check`` decides). A pooled
        session slot applies when something was asked or promised. A fixed
        exit never does: discovery placed it and health checks re-read it.
        """
        if project.location_preflight == PreflightMode.OFF:
            return False
        if is_dynamic_gateway(proxy):
            return True
        if not proxy.metadata.get(META_SESSION_ID):
            return False
        return cls.expected_location(requested, proxy) is not None

    @staticmethod
    def _sample_rotating(proxy: Proxy) -> bool:
        """Whether this session-less request is one the connector wants echoed.

        The percentage travels on the rendered proxy (``render_request``), so the
        checker needs no connector lookup on the request path.
        """
        raw = proxy.metadata.get(META_EXIT_SAMPLE_PERCENT, DEFAULT_EXIT_SAMPLE_PERCENT)
        try:
            percent = int(raw)
        except (TypeError, ValueError):
            percent = DEFAULT_EXIT_SAMPLE_PERCENT
        return percent >= 100 or (percent > 0 and random.random() * 100 < percent)

    async def check(
        self,
        project: Project,
        proxy: Proxy,
        *,
        session_id: str | None,
        requested: LocationTarget | None,
    ) -> PreflightVerdict:
        """Verify ``proxy``'s exit against the expected location.

        ``proxy`` must carry resolved credentials (the echo request goes
        through it). Returns a verdict; callers reject only when the project's
        mode is ``reject`` and ``ok`` is False.

        The expected country is handed to the resolver as the claim, so it
        is judged under the project's source policy and conflict rule
        exactly as attribution judges a vendor's claim: contradicted when the
        independent sources say otherwise, no verdict when they disagree
        among themselves. What was observed is the independent answer, never
        the claim, whatever the policy lets win. A state or city is confirmed
        when any loaded database agrees and contradicted when every database
        that places the IP at that level says somewhere else; with no
        city-level database loaded there is no verdict below the country.

        A verdict is cached per project and proxy for the session TTL, since a
        vendor session keeps its exit for about that long. The cache is
        dropped early when a health check or refresh sees the proxy exiting
        from a different IP (``exit_ip_changed``), so a vendor rotating the
        exit mid-session costs at most one health check interval of trust.
        """
        expected = self.expected_location(requested, proxy)
        dynamic = is_dynamic_gateway(proxy)
        if expected is None and not dynamic:
            return SKIP

        # Dynamic rows: the rendered proxy names the vendor session when the
        # client did (see DescriptorProvider.render_request); a rotating
        # request has none, shares nothing with the next one, and is sampled.
        vendor_session = proxy.metadata.get(META_SESSION_ID) if dynamic else None
        rotating = dynamic and not vendor_session
        # Sampling only thins observation. When a location was asked or promised
        # and the project chose a hard mode, every request is verified: reject
        # and retry are guarantees, and a sampled guarantee is none.
        guaranteed = expected is not None and project.location_preflight in (PreflightMode.RETRY, PreflightMode.REJECT)
        if rotating and not guaranteed and not self._sample_rotating(proxy):
            return SKIP

        key: str | None = GEO_PREFLIGHT_KEY.format(project_id=project.id, proxy_id=proxy.id)
        if vendor_session:
            # The same client session may ask for another place next time; the
            # vendor then routes it elsewhere, so the verdict is per place too.
            key = f"{key}:{vendor_session}:{expected.key if expected else '-'}"
        elif rotating:
            key = None
        cached = await self._cached(key) if key else None
        if cached is not None:
            return cached

        self.checks += 1
        ip, endpoint = await self._geo_service.discoverer().discover_with_place(
            proxy.url,
            log_context={"proxy_id": proxy.id, "project_id": project.id, "preflight": True},
        )
        if ip is None:
            verdict = PreflightVerdict(
                ok=True, expected=expected, observed=None, ip=None, reason="echo request failed"
            )
            # Short cache so a broken echo endpoint does not add a timeout to every request.
            if key:
                await self._store(key, verdict, ttl=30)
            return verdict

        # The expected place is the claim under judgement; the answer comes
        # from the databases and the echo endpoint alone (``observed_country``).
        resolution = self._geo_service.resolve_ip(
            ip,
            policy=project.source_policy(self._geo_service.default_policy),
            claimed=expected,
            endpoint=endpoint,
        )
        observed_country = resolution.observed_country
        country_conflict = resolution.country_conflict
        ok = not (country_conflict or resolution.state_conflict or resolution.city_conflict)
        observed: LocationTarget | None = None
        if expected is not None:
            observed = LocationTarget(
                country=observed_country,
                state=resolution.observed_state if expected.state else None,
                city=resolution.observed_city if expected.city else None,
            ) or None
        elif observed_country:
            observed = LocationTarget(country=observed_country)
        if expected is None:
            reason = "observed"  # a sampled rotating request: nothing was asked, the exit is recorded
        elif ok:
            reason = "match" if observed_country else "location unknown"
        else:
            reason = "mismatch"
        verdict = PreflightVerdict(
            ok=ok,
            expected=expected,
            observed=observed,
            ip=ip,
            reason=reason,
            endpoint_country=endpoint.country if endpoint else None,
            country_conflict=country_conflict,
            state_conflict=resolution.state_conflict,
            city_conflict=resolution.city_conflict,
        )

        self._geo_service.record(
            IpObservation(
                proxy_id=proxy.id,
                connector_id=proxy.connector_id,
                project_id=project.id,
                session_id=vendor_session or session_id,
                source=ObservationSource.PREFLIGHT,
                ip=ip,
                claimed_country=expected.country if expected else None,
                endpoint_country=endpoint.country if endpoint else None,
                resolved_country=observed_country,
                resolved_source=resolution.resolved_source,
                country_conflict=country_conflict,
                claimed_state=resolution.claimed_state,
                claimed_city=resolution.claimed_city,
                resolved_state=resolution.resolved_state,
                resolved_city=resolution.resolved_city,
                state_conflict=resolution.state_conflict,
                city_conflict=resolution.city_conflict,
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
                expected=expected.key if expected else None,
                observed=observed.key if observed else None,
                ip=ip,
                mode=project.location_preflight.value,
            )
        if key:
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
        expected = verdict.expected.describe() if verdict.expected else "?"
        observed = verdict.observed.describe() if verdict.observed else "unknown"
        levels = " and ".join(verdict.failed_levels) or "location"
        return f"Exit {levels} mismatch: requested {expected}, observed {observed} ({verdict.ip})"
