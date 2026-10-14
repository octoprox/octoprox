# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Verify where a selected upstream really exits before forwarding a client's request.

A residential or mobile exit belongs to the *session*, not to the proxy row,
so the check runs against the proxy the request was routed to, right after
selection: one echo request through it, resolve the IP, compare with the
location the request *requires*: the ``-cc-``, ``-st-`` and ``-city-`` values
the client named. A client that named nothing has no expectation, so a
pooled slot is not echoed for it: the slot's own claim is judged on the row
by every sighting of its exit. A dynamic gateway is: the place rendered into
its vendor session stands in as the vendor's promise. The verdict is
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

This module produces the verdict for the request. What the echo found about
a pooled slot is handed to the attributor on ``exit_ip_observed``, exactly as
a health check's sighting is, so the row learns its new exit, verdict and,
under a refusing project, its replacement at once rather than a health check
interval later. Whether a verdict refuses the request is the project's two
decisions (``PreflightVerdict.acceptable``), and whether a refused request
moves to another proxy is the exit verifier's call.

Failures of the echo request itself never block traffic: an unreachable echo
endpoint is an operations problem, not evidence about the proxy.
"""

from __future__ import annotations

import json
import random
from collections.abc import Sequence
from typing import NamedTuple

import structlog

from api.core.event_bus import event_bus
from api.core.signals import exit_ip_changed, exit_ip_observed
from api.db.redis import GEO_PREFLIGHT_KEY, RedisClient
from api.geo.models import (
    ContradictedAction,
    IpObservation,
    LocationRules,
    ObservationSource,
    UnknownAction,
)
from api.geo.service import GeoService
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy
from api.providers.sdk.descriptor import DEFAULT_EXIT_SAMPLE_PERCENT
from api.providers.sdk.strategies import (
    META_EXIT_SAMPLE_PERCENT,
    META_GEO_CITY,
    META_GEO_STATE,
    META_SESSION_ID,
    is_dynamic_gateway,
)

logger = structlog.get_logger()

# Who set a preflight requirement (``PreflightVerdict.required_by``).
REQUIRED_BY_REQUEST = "request"
REQUIRED_BY_VENDOR = "vendor"

class PreflightVerdict(NamedTuple):
    """What one echo through the selected upstream found.

    ``expected`` is the location the request required and ``observed`` where
    attribution placed the exit at those levels (country always, state and
    city when asked). One verdict per level, with the polarity of an
    observation's ``conflict``: True contradicted, False confirmed, None when
    nothing was asked at that level or nothing was observed there. ``ok`` is
    False only for a confirmed mismatch at some level; what else refuses the
    request is the project's rules' call (``acceptable``). ``required_by``
    says who set the requirement: the client (``request``), or the place
    rendered into a dynamic session when the client named nothing (``vendor``).
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
    required_by: str | None = None
    # Set by the verifier when the slot left the pool while it was being checked.
    replaced: bool = False

    def acceptable(self, rules: LocationRules, allowed: Sequence[str] = ()) -> bool:
        """Whether this verdict lets the request through under the project's ``rules``.

        A verdict without an exit (the echo failed) or without a requirement
        (a sampled sighting) is never a refusal: an echo outage must not
        become a traffic outage. Otherwise the two decisions apply to every
        level that was required:

        * the client asked for the place: a contradicted level is refused
          unless the vendor's claim is trusted over the evidence, and an open
          one is refused when unknown exits are;
        * the vendor promised it: refusing contradictions holds the vendor
          to that exact place, as above; routing on the evidence holds the
          exit to the connector's declared countries (``allowed``) instead,
          and to nothing when it declares none, so a placed exit anywhere in
          the list serves; trusting the vendor verifies nothing but, when
          unknown exits are refused, placement;
        """
        if self.expected is None or self.ip is None:
            return True
        contradicted = not self.ok
        unplaced = self.country_conflict is None
        refuse_unknown = rules.unknown == UnknownAction.REFUSE
        origin = self.required_by or REQUIRED_BY_REQUEST
        if origin == REQUIRED_BY_REQUEST or rules.contradicted == ContradictedAction.REFUSE:
            # The requirement is exact.
            if contradicted:
                return rules.contradicted == ContradictedAction.VENDOR
            return not (refuse_unknown and self.open_levels)
        # The vendor's promise, with the vendor not held to its exact word.
        if refuse_unknown and unplaced:
            return False
        if rules.contradicted == ContradictedAction.VENDOR or unplaced:
            return True
        observed = self.observed.country if self.observed else None
        return not allowed or observed in allowed

    @property
    def open_levels(self) -> tuple[str, ...]:
        """The required levels nothing independent could place, most general first."""
        if self.expected is None:
            return ()
        levels = [("country", self.country_conflict)]
        if self.expected.state:
            levels.append(("state", self.state_conflict))
        if self.expected.city:
            levels.append(("city", self.city_conflict))
        return tuple(level for level, conflict in levels if conflict is None)

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
                "required_by": self.required_by,
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
                required_by=data.get("required_by"),
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
    def requirement(requested: LocationTarget | None, proxy: Proxy) -> tuple[LocationTarget | None, str | None]:
        """The location the request requires and who required it; ``(None, None)`` when nothing was asked or promised.

        The request's own ``-cc-``, ``-st-`` and ``-city-`` are the
        requirement, and a client that named the country is held to what it
        named, not to a state or city the vendor rendered. A client that named
        nothing has no expectation: on a pooled slot there is then nothing to
        verify, since the slot's own claim is judged on the row by every
        sighting; on a dynamic gateway the place rendered into the vendor
        session (``geo``, ``geo_state``, ``geo_city``) stands in as the
        vendor's promise. The attributed location is never used: it is what
        was observed, not what was required.
        """
        target = requested or None
        # The parser keeps a -cc- value that is not a country code as typed so
        # it matches no connector; it must not become the claim either, since
        # the observation stores the claim as a two-letter code.
        country = target.country if target else None
        if country:
            return LocationTarget(country=country, state=target.state if target else None, city=target.city if target else None), REQUIRED_BY_REQUEST
        if not is_dynamic_gateway(proxy):
            return None, None
        country = GeoService.claimed_country_of(proxy)
        if country is None:
            # A state or city is meaningless without the country; nothing to verify.
            return None, None
        promised = LocationTarget(
            country=country,
            state=proxy.metadata.get(META_GEO_STATE) or None,
            city=proxy.metadata.get(META_GEO_CITY) or None,
        )
        return promised, REQUIRED_BY_VENDOR

    @classmethod
    def applies(cls, project: Project, requested: LocationTarget | None, proxy: Proxy) -> bool:
        """Whether this request needs a preflight at all.

        Only rows whose exit the vendor can move are checked. A dynamic row
        always applies once preflight is on: even with nothing to verify its
        requests are sampled for attribution (``check`` decides). A pooled
        session slot applies when the client named a place. A fixed exit
        never does: discovery placed it and health checks re-read it.
        """
        if not project.location_preflight:
            return False
        if is_dynamic_gateway(proxy):
            return True
        if not proxy.metadata.get(META_SESSION_ID):
            return False
        return cls.requirement(requested, proxy)[0] is not None

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
        allowed: Sequence[str] = (),
    ) -> PreflightVerdict:
        """Verify ``proxy``'s exit against the expected location.

        ``allowed`` is the connector's declared countries, which decide, with
        the project's rules, whether a verdict on the vendor's promise can
        refuse at all, and so whether a session-less dynamic request is
        sampled or always verified.

        ``proxy`` must carry resolved credentials (the echo request goes
        through it). Returns a verdict; whether it refuses the request is
        ``PreflightVerdict.acceptable`` under the project's location rules.

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

        For a pooled slot the echo is also a sighting of the slot's exit: it
        is published on ``exit_ip_observed`` before the verdict is cached, so
        the attributor judges the slot's own claim, writes the row, records
        the one observation and, when the exit moved, drops this slot's stale
        cache before the fresh verdict is stored. A dynamic gateway's exit
        belongs to the vendor session alone, so its sighting is recorded here
        and nothing is written on the row.
        """
        expected, required_by = self.requirement(requested, proxy)
        dynamic = is_dynamic_gateway(proxy)
        if expected is None and not dynamic:
            return SKIP

        # Dynamic rows: the rendered proxy names the vendor session when the
        # client did (see DescriptorProvider.render_request); a rotating
        # request has none, shares nothing with the next one, and is sampled.
        vendor_session = proxy.metadata.get(META_SESSION_ID) if dynamic else None
        rotating = dynamic and not vendor_session
        # Sampling only thins observation. When a location was asked or promised
        # and the project's rules can refuse, every request is verified: a
        # refusal is a guarantee, and a sampled guarantee is none.
        guaranteed = expected is not None and self._can_refuse(project.location_rules, expected, required_by, allowed)
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
        # A pooled slot's key names no requirement, and the same slot may serve
        # a request that asked for a place and one that rode on the vendor's
        # promise, which ``acceptable`` judges differently; a verdict is reused
        # only for the requirement it answered.
        if cached is not None and cached.expected == expected and cached.required_by == required_by:
            return cached

        self.checks += 1
        ip, endpoint = await self._geo_service.discoverer().discover_with_place(
            proxy.url,
            log_context={"proxy_id": proxy.id, "project_id": project.id, "preflight": True},
        )
        if ip is None:
            verdict = PreflightVerdict(
                ok=True, expected=expected, observed=None, ip=None, reason="echo request failed", required_by=required_by
            )
            # Short cache so a broken echo endpoint does not add a timeout to every request.
            if key:
                await self._store(key, verdict, ttl=30)
            return verdict

        # The expected place is the claim under judgement; the answer comes
        # from the databases and the echo endpoint alone (``observed_country``).
        candidates = self._geo_service.attribute(ip)
        resolution = self._geo_service.resolve_ip(
            ip,
            policy=project.source_policy(self._geo_service.default_policy),
            claimed=expected,
            endpoint=endpoint,
            candidates=candidates,
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
            required_by=required_by,
        )

        if dynamic:
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
        else:
            # The slot's sighting: the attributor judges the slot's own claim,
            # not the request's, and writes the row.
            await event_bus.publish(
                exit_ip_observed,
                self,
                proxy_id=proxy.id,
                ip=ip,
                source=ObservationSource.PREFLIGHT.value,
                endpoint_country=endpoint.country if endpoint else None,
                endpoint_state=endpoint.state if endpoint else None,
                endpoint_city=endpoint.city if endpoint else None,
                session_id=session_id,
                candidates=candidates,
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
                contradicted=project.location_contradicted.value,
                unknown=project.location_unknown.value,
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
    def _can_refuse(rules: LocationRules, expected: LocationTarget, required_by: str | None, allowed: Sequence[str]) -> bool:
        """Whether a verdict on this requirement could refuse under ``rules``; if not, an echo changes nothing.

        Asked of the acceptance rule itself, on the two worst verdicts the
        echo could produce: a contradiction at the country, and an exit
        nothing independent could place. If both pass, nothing can fail.

        One exception by choice: under ``evidence`` the connector's declared
        countries bound a vendor promise best-effort. The client asked for
        nothing, the operator set a sampling rate for exactly that traffic,
        so the list is checked on the requests that are sampled and is no
        guarantee. ``refuse`` holds the promise exactly and verifies every
        request, as it always did.
        """
        if required_by == REQUIRED_BY_VENDOR and rules.contradicted == ContradictedAction.EVIDENCE:
            allowed = ()
        # ``ZZ`` is a user-assigned code no connector declares: an exit placed, and outside every list.
        contradicted = PreflightVerdict(
            ok=False, expected=expected, observed=LocationTarget(country="ZZ"), ip="?", reason="mismatch",
            country_conflict=True, required_by=required_by,
        )
        unplaced = PreflightVerdict(
            ok=True, expected=expected, observed=None, ip="?", reason="location unknown",
            country_conflict=None, required_by=required_by,
        )
        return not (contradicted.acceptable(rules, allowed) and unplaced.acceptable(rules, allowed))

    @staticmethod
    def rejection_message(verdict: PreflightVerdict, rules: LocationRules, allowed: Sequence[str] = ()) -> str:
        expected = verdict.expected.describe() if verdict.expected else "?"
        observed = verdict.observed.describe() if verdict.observed else "unknown"
        if rules.contradicted == ContradictedAction.EVIDENCE and verdict.required_by == REQUIRED_BY_VENDOR and verdict.observed is not None:
            exit_country = verdict.observed.country
            if exit_country and allowed and exit_country not in allowed:
                return f"Exit country outside the connector's countries: requested {expected}, observed {observed} ({verdict.ip})"
        if verdict.ok:
            # Refused for missing evidence: the first required level nothing independent could place.
            level = verdict.open_levels[0] if verdict.open_levels else "country"
            return f"Exit {level} not confirmed: requested {expected}, observed {observed} ({verdict.ip})"
        levels = " and ".join(verdict.failed_levels) or "location"
        return f"Exit {levels} mismatch: requested {expected}, observed {observed} ({verdict.ip})"
