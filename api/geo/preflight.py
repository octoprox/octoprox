# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Verify where a selected upstream really exits before forwarding a client's request: one echo through it, judged and cached.

Only session-bound rows are checked, pooled slots and dynamic gateways, whose exit the vendor can move.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

import structlog

from api.core import utc_now
from api.core.logging import error_text
from api.db.redis import GEO_PREFLIGHT_KEY, RedisClient
from api.geo.attributor import ProxyAttributor
from api.geo.models import (
    ContradictedAction,
    IpObservation,
    LocationCandidate,
    LocationRules,
    ObservationSource,
    UnknownAction,
)
from api.geo.service import GeoService
from api.jobs.messages import SightingMessage
from api.models.location import LocationTarget
from api.models.project import Project
from api.models.proxy import Proxy
from api.providers.sdk.descriptor import DEFAULT_EXIT_SAMPLE_PERCENT
from api.providers.sdk.strategies import (
    META_EXIT_SAMPLE_PERCENT,
    META_GEO_CITY,
    META_GEO_STATE,
    META_SESSION_ID,
    META_SESSION_SECONDS,
    is_dynamic_gateway,
)

if TYPE_CHECKING:
    from api.gateway.sightings import SightingPublisher
    from api.routing.exclusions import LocalExclusions

logger = structlog.get_logger()

# Who set a preflight requirement (``PreflightVerdict.required_by``).
REQUIRED_BY_REQUEST = "request"
REQUIRED_BY_VENDOR = "vendor"


class PreflightVerdict(NamedTuple):
    """What one echo through the selected upstream found, a verdict per level with an observation's ``conflict`` polarity.

    ``ok`` is False only for a confirmed mismatch at some level; whatever else refuses the request is the project's
    rules' call (``acceptable``).
    """

    ok: bool
    # The location the request required, and where attribution placed the exit
    # at those levels (the country always, the state and city when asked).
    expected: LocationTarget | None
    observed: LocationTarget | None
    ip: str | None
    reason: str
    endpoint_country: str | None = None
    # True contradicted, False confirmed, None when nothing was asked or
    # observed at that level.
    country_conflict: bool | None = None
    state_conflict: bool | None = None
    city_conflict: bool | None = None
    # Who set the requirement: the client (``request``), or the place rendered
    # into a dynamic session when the client named nothing (``vendor``).
    required_by: str | None = None
    # Set by the verifier when the slot left the pool while it was being checked.
    replaced: bool = False
    # When the echo answered, naive UTC; None for a verdict no echo answered.
    checked_at: datetime | None = None
    # True when the verdict was read from the cache rather than echoed for this request; never stored.
    cached: bool = False

    def acceptable(self, rules: LocationRules, allowed: Sequence[str] = ()) -> bool:
        """Whether this verdict lets the request through under the project's ``rules``; one with no requirement never refuses.

        A client's requirement is exact, as is a vendor's promise when contradictions are refused; else a promise holds
        the exit to the declared countries (``allowed``). A failed echo checked nothing, so it refuses where
        :meth:`can_refuse` says an answer could have.
        """
        if self.expected is None:
            return True
        if self.ip is None:
            return not self.can_refuse(rules, self.expected, self.required_by, allowed)
        contradicted = not self.ok
        unplaced = self.country_conflict is None
        refuse_unknown = rules.unknown == UnknownAction.REFUSE
        origin = self.required_by or REQUIRED_BY_REQUEST
        if origin == REQUIRED_BY_REQUEST or rules.contradicted == ContradictedAction.REFUSE:
            # The requirement is exact.
            if contradicted:
                return rules.contradicted == ContradictedAction.VENDOR
            return not (refuse_unknown and self.open_levels)
        # The vendor's promise, with the vendor not held to its exact word: a
        # trusted vendor only needs the exit placed when unknown exits are refused.
        if refuse_unknown and unplaced:
            return False
        if rules.contradicted == ContradictedAction.VENDOR or unplaced:
            return True
        observed = self.observed.country if self.observed else None
        return not allowed or observed in allowed

    @staticmethod
    def can_refuse(rules: LocationRules, expected: LocationTarget, required_by: str | None, allowed: Sequence[str]) -> bool:
        """Whether a verdict on this requirement could refuse under ``rules``, judged on the two worst an echo could give.

        By choice, under ``evidence`` a vendor's promise is held to the connector's declared countries only by an answer
        that placed the exit, so the list decides nothing here: whether every such request is echoed, and whether a
        failed echo refuses it, follow ``location_unknown`` alone.
        """
        if required_by == REQUIRED_BY_VENDOR and rules.contradicted == ContradictedAction.EVIDENCE:
            allowed = ()
        # ``ZZ`` is a user-assigned code no connector declares: an exit placed, and outside every list. Both carry
        # an ip, so judging them never comes back here.
        contradicted = PreflightVerdict(
            ok=False, expected=expected, observed=LocationTarget(country="ZZ"), ip="?", reason="mismatch",
            country_conflict=True, required_by=required_by,
        )
        unplaced = PreflightVerdict(
            ok=True, expected=expected, observed=None, ip="?", reason="location unknown",
            country_conflict=None, required_by=required_by,
        )
        return not (contradicted.acceptable(rules, allowed) and unplaced.acceptable(rules, allowed))

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
                "checked_at": self.checked_at.isoformat() if self.checked_at else None,
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
            checked_at = data.get("checked_at")
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
                checked_at=datetime.fromisoformat(checked_at) if checked_at else None,
            )
        except (TypeError, ValueError, KeyError):
            return None


SKIP = PreflightVerdict(ok=True, expected=None, observed=None, ip=None, reason="not applicable")


class PreflightChecker:
    """Runs and caches per-proxy exit verification on a gateway.

    Args:
        redis_client: the verdict cache; None runs without one.
    """

    def __init__(
        self,
        geo_service: GeoService,
        redis_client: RedisClient | None,
        *,
        sightings: SightingPublisher,
        exclusions: LocalExclusions,
    ) -> None:
        self._geo_service = geo_service
        self._redis = redis_client
        self._sightings = sightings
        self._exclusions = exclusions
        self.checks = 0
        self.rejections = 0

    async def invalidate(self, project_id: str, proxy_id: str) -> None:
        """Drop the cached verdict for a proxy, on every gateway; the next request through it is verified again.

        A worker that learns of an exit move from anything but preflight drops the same key.
        """
        if self._redis is None:
            return
        key = GEO_PREFLIGHT_KEY.format(project_id=project_id, proxy_id=proxy_id)
        try:
            async with self._redis.request_deadline():
                await self._redis.client.delete(key)
        except Exception as exc:
            logger.debug("Could not drop cached preflight verdict", proxy_id=proxy_id, error=error_text(exc))

    @property
    def max_attempts(self) -> int:
        """Proxies to try under ``retry`` before giving up."""
        return self._geo_service.settings.preflight_max_attempts

    # --- what a request requires ---------------------------------------------------

    @staticmethod
    def requirement(requested: LocationTarget | None, proxy: Proxy) -> tuple[LocationTarget | None, str | None]:
        """The location the request requires and who required it; ``(None, None)`` when nothing was asked or promised.

        For a client that named nothing, a dynamic gateway's rendered place stands in as the vendor's promise.
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
        """Whether this request needs a preflight at all: always on a dynamic gateway, on a pooled slot when a place was named.

        A fixed exit never does, since discovery placed it and health checks re-read it.
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
        """Verify the exit of ``proxy``, its credentials resolved, against the location the request requires.

        A verdict is cached per project and proxy (and per vendor session and place on a dynamic gateway) for the
        session TTL, and the echo of a pooled slot is also a sighting for the workers.

        Args:
            allowed: the connector's declared countries, which with the rules decide whether a vendor's promise can refuse.

        Raises:
            The upstream's error (:data:`~api.providers.sdk.sources.UPSTREAM_ERRORS`) when it failed the echo.
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
        guaranteed = expected is not None and PreflightVerdict.can_refuse(project.location_rules, expected, required_by, allowed)
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
        started = time.monotonic()
        # An upstream failing the echo raises here, uncached: it is the
        # request's own failure, and the next request asks the vendor again.
        ip, endpoint = await self._geo_service.discoverer().discover_with_place(
            proxy.url,
            log_context={"proxy_id": proxy.id, "project_id": project.id, "preflight": True},
            raise_upstream=True,
        )
        observed_at = utc_now()
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
            checked_at=observed_at,
        )

        if dynamic:
            self._geo_service.record(
                IpObservation(
                    observed_at=observed_at,
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
                )
            )
        else:
            self._sighting(project, proxy, ip, endpoint, session_id=session_id, candidates=candidates, observed_at=observed_at)
        if not ok:
            self.rejections += 1
            # Debug: a refusal is routine under strict location rules, and the
            # Failures page keeps the count and examples (preflight_mismatch).
            logger.debug(
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
            await self._store(key, verdict, ttl=self._verdict_ttl(proxy, elapsed=time.monotonic() - started))
        return verdict

    def _verdict_ttl(self, proxy: Proxy, *, elapsed: float) -> int:
        """How long a verdict is trusted: the preflight TTL, or what is left of the vendor session when that is shorter.

        Past its length the vendor may hand the same session another exit, and its clock started with the echo that
        took ``elapsed`` seconds; ``render_request`` puts the length on the request.
        """
        ttl = self._geo_service.settings.preflight_session_ttl_seconds
        try:
            session = int(proxy.metadata.get(META_SESSION_SECONDS) or 0)
        except (TypeError, ValueError):
            session = 0
        return min(ttl, max(1, session - int(elapsed))) if session > 0 else ttl

    def _sighting(
        self,
        project: Project,
        proxy: Proxy,
        ip: str,
        endpoint: LocationTarget | None,
        *,
        session_id: str | None,
        candidates: Sequence[LocationCandidate],
        observed_at: datetime,
    ) -> None:
        """Hand a pooled slot's sighting to the workers, repeats included, retiring the slot here when they will rotate it.

        A copy of the row is judged under the worker's own rules (:meth:`ProxyAttributor.is_repeat`,
        :meth:`ProxyAttributor.is_refused_slot`); the row itself is never changed, and the worker records the one
        observation.
        """
        if not ProxyAttributor.is_repeat(proxy, ip, ObservationSource.PREFLIGHT):
            slot = proxy.model_copy(deep=True)
            self._geo_service.judge(
                slot,
                ip,
                policy=project.source_policy(self._geo_service.default_policy),
                endpoint=endpoint,
                candidates=candidates,
                checked_at=observed_at,
            )
            if ProxyAttributor.is_refused_slot(slot, project):
                self._exclusions.retire(proxy.id)
        self._sightings.push(
            SightingMessage(
                proxy_id=proxy.id,
                ip=ip,
                source=ObservationSource.PREFLIGHT,
                endpoint_country=endpoint.country if endpoint else None,
                endpoint_state=endpoint.state if endpoint else None,
                endpoint_city=endpoint.city if endpoint else None,
                session_id=session_id,
                candidates=list(candidates),
                observed_at=observed_at,
            )
        )

    async def _cached(self, key: str) -> PreflightVerdict | None:
        """The verdict cached under ``key``, marked ``cached``; None when there is none, or Redis did not answer within the deadline."""
        if self._redis is None:
            return None
        try:
            async with self._redis.request_deadline():
                raw = await self._redis.client.get(key)
        except Exception:
            return None
        verdict = PreflightVerdict.from_json(raw) if raw else None
        return verdict._replace(cached=True) if verdict else None

    async def _store(self, key: str, verdict: PreflightVerdict, *, ttl: int) -> None:
        """Cache ``verdict`` under ``key`` for ``ttl`` seconds; a Redis that does not answer in time keeps none."""
        if self._redis is None:
            return
        try:
            async with self._redis.request_deadline():
                await self._redis.client.set(key, verdict.to_json(), ex=max(int(ttl), 1))
        except Exception as exc:
            logger.debug("Could not cache preflight verdict", error=error_text(exc))

    @staticmethod
    def rejection_message(verdict: PreflightVerdict, rules: LocationRules, allowed: Sequence[str] = ()) -> str:
        expected = verdict.expected.describe() if verdict.expected else "?"
        if verdict.ip is None:
            return f"Exit location not checked, echo request failed: requested {expected}"
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
