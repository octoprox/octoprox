# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Applies IP attribution to proxies under each project's source policy, and records what it judges.

Judging changes only the proxy object it is given; applying an observation, re-judging and recording a sighting each
record what they find.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import structlog

from api.core import utc_now
from api.core.config import Settings
from api.geo.models import (
    JUDGEMENT_SOURCE,
    MANUAL_SOURCE,
    META_CITY,
    META_CITY_SOURCE,
    META_CITY_VERDICT,
    META_COUNTRY_SOURCE,
    META_ENDPOINT_CITY,
    META_ENDPOINT_COUNTRY,
    META_ENDPOINT_STATE,
    META_LOCATION,
    META_LOCATION_CANDIDATES,
    META_LOCATION_CHECKED_AT,
    META_LOCATION_CONFLICT,
    META_LOCATION_VERDICT,
    META_STATE,
    META_STATE_SOURCE,
    META_STATE_VERDICT,
    META_VENDOR_CITY,
    META_VENDOR_COUNTRY,
    META_VENDOR_STATE,
    ContradictedAction,
    ExitJudgement,
    GeoSettings,
    GeoSourceKind,
    IpObservation,
    LocationCandidate,
    LocationVerdict,
    ObservationSource,
    Resolution,
    SourcePolicy,
    UnknownAction,
    normalize_country,
)
from api.geo.observations import ObservationRecorder
from api.geo.readers import is_ip
from api.geo.resolver import endpoint_candidate, resolve, vendor_candidate
from api.geo.settings import GeoSettingsStore
from api.geo.store import GeoDatabaseStore
from api.models.location import LocationTarget
from api.models.proxy import Proxy
from api.providers.sdk.descriptor import IpDiscoverySpec
from api.providers.sdk.sources import IpDiscoverer
from api.providers.sdk.strategies import (
    META_COUNTRY,
    META_DISCOVERED_IP,
    META_GEO_CITY,
    META_GEO_COUNTRY,
    META_GEO_STATE,
)

logger = structlog.get_logger()


# Metadata keys judge may change, compared to decide whether the caller must persist.
# ``location_checked_at`` is not among them: a sighting that confirms what the
# row says moves nothing a reader acts on, and writing the stamp alone would
# put every repeat sighting into the routing feed.
_TRACKED_KEYS = (
    META_COUNTRY,
    META_COUNTRY_SOURCE,
    META_VENDOR_COUNTRY,
    META_STATE,
    META_STATE_SOURCE,
    META_STATE_VERDICT,
    META_CITY,
    META_CITY_SOURCE,
    META_LOCATION,
    META_LOCATION_CONFLICT,
    META_LOCATION_VERDICT,
    META_CITY_VERDICT,
    META_LOCATION_CANDIDATES,
    META_DISCOVERED_IP,
)


class GeoService:
    """Attribution for one process, with no I/O of its own: the databases are memory-mapped, the policy a cached row, and observations buffered.

    Callers persist a proxy whose judgement changed it.
    """

    def __init__(
        self,
        settings: Settings,
        database_store: GeoDatabaseStore,
        settings_store: GeoSettingsStore,
        observation_recorder: ObservationRecorder,
    ) -> None:
        self._settings = settings
        self._database_store = database_store
        self._settings_store = settings_store
        self._observation_recorder = observation_recorder
        # One discoverer per echo spec; rebuilt only when a policy change moves the spec.
        self._discoverer: IpDiscoverer | None = None
        self._discoverer_spec: IpDiscoverySpec | None = None

    # --- access --------------------------------------------------------------------

    @property
    def database_store(self) -> GeoDatabaseStore:
        return self._database_store

    @property
    def settings_store(self) -> GeoSettingsStore:
        return self._settings_store

    @property
    def settings(self) -> GeoSettings:
        return self._settings_store.settings

    @property
    def default_policy(self) -> SourcePolicy:
        return self._settings_store.default_policy

    @property
    def observation_recorder(self) -> ObservationRecorder:
        return self._observation_recorder

    # --- echo endpoint -------------------------------------------------------------

    def echo_spec(self) -> IpDiscoverySpec:
        """Discovery spec for the configured echo endpoint."""
        current = self.settings
        return IpDiscoverySpec(
            url=current.echo_url,
            ip_path=current.echo_ip_path,
            country_path=current.echo_country_path or None,
            state_path=current.echo_state_path or None,
            city_path=current.echo_city_path or None,
            timeout_seconds=current.echo_timeout_seconds,
        )

    def discoverer(self) -> IpDiscoverer:
        """The discoverer for the current echo spec, rebuilt only when a settings change moves the spec."""
        spec = self.echo_spec()
        if self._discoverer is None or self._discoverer_spec != spec:
            self._discoverer = IpDiscoverer(spec)
            self._discoverer_spec = spec
        return self._discoverer

    async def resync(self) -> None:
        """Re-read the settings row and the database list from Postgres; both apply only what changed.

        A re-attribution job runs it first, so it judges with what was committed before it was asked for.
        """
        await self._settings_store.load()
        await self._database_store.sync_all()

    def is_echo_url(self, url: str) -> bool:
        """Whether ``url`` is the echo endpoint, so its response carries an IP we can parse."""
        return url.strip().rstrip("/") == self.settings.echo_url.strip().rstrip("/")

    # --- resolution ----------------------------------------------------------------

    def attribute(self, ip: str) -> list[LocationCandidate]:
        """What the loaded databases say about ``ip``."""
        if not is_ip(ip):
            return []
        return self._database_store.lookup(ip.strip())

    def resolve_ip(
        self,
        ip: str,
        *,
        policy: SourcePolicy | None = None,
        claimed: LocationTarget | None = None,
        endpoint: LocationTarget | None = None,
        candidates: Sequence[LocationCandidate] | None = None,
    ) -> Resolution:
        """Resolve one IP from the databases plus whatever the caller learned.

        A city claim is judged by the policy's radius while a GeoNames file is loaded, and by name without one.

        Args:
            policy: the owning project's source policy; None for the install default.
            claimed: what was promised about the exit at any level: the vendor's word, or the place a request required.
            endpoint: what an independent endpoint requested through the proxy reported.
            candidates: the databases' answer for ``ip`` when the caller looked it up already, so one lookup serves two claims.
        """
        candidates = list(candidates) if candidates is not None else self.attribute(ip)
        vendor = vendor_candidate(claimed)
        if vendor is not None:
            candidates.append(vendor)
        independent = endpoint_candidate(endpoint)
        if independent is not None:
            candidates.append(independent)
        store = self._database_store
        return resolve(candidates, policy or self.default_policy, store.locate_city if store.places_loaded else None)

    # --- applying to proxies -------------------------------------------------------

    @staticmethod
    def claimed_country_of(proxy: Proxy) -> str | None:
        """The vendor's claim for a proxy: its recorded claim, else the geo it was provisioned for."""
        for key in (META_VENDOR_COUNTRY, META_GEO_COUNTRY):
            code = normalize_country(proxy.metadata.get(key))
            if code:
                return code
        return None

    @classmethod
    def outside_declared_countries(cls, proxy: Proxy, declared: Sequence[str]) -> bool:
        """Whether a row the vendor placed in a declared country now resolves outside the connector's list: routing never uses it.

        A row that made no claim, or whose country an operator pinned, is never outside it here.
        """
        if proxy.metadata.get(META_COUNTRY_SOURCE) == MANUAL_SOURCE:
            return False
        return bool(declared) and proxy.country is not None and proxy.country not in declared and cls.claimed_country_of(proxy) is not None

    @classmethod
    def claimed_place_of(cls, proxy: Proxy) -> LocationTarget | None:
        """Everything promised about a proxy's exit, per level: what attribution judges and preflight verifies against.

        The vendor's word or the slot's provisioned place, else what an operator pinned, each normalised when written.
        """
        manual = proxy.manual_location
        metadata = proxy.metadata
        country = cls.claimed_country_of(proxy)
        if country is None and metadata.get(META_COUNTRY_SOURCE) == MANUAL_SOURCE:
            country = metadata.get(META_COUNTRY)
        return LocationTarget(
            country=country or None,
            state=metadata.get(META_VENDOR_STATE) or metadata.get(META_GEO_STATE) or manual.get("state_code") or None,
            city=metadata.get(META_VENDOR_CITY) or metadata.get(META_GEO_CITY) or manual.get("city") or None,
        ) or None

    @staticmethod
    def endpoint_place_of(proxy: Proxy) -> LocationTarget | None:
        """What a third-party endpoint last reported about the proxy's exit, per level."""
        metadata = proxy.metadata
        return LocationTarget(
            country=metadata.get(META_ENDPOINT_COUNTRY) or None,
            state=metadata.get(META_ENDPOINT_STATE) or None,
            city=metadata.get(META_ENDPOINT_CITY) or None,
        ) or None

    def apply_observation(
        self,
        proxy: Proxy,
        ip: str,
        *,
        source: ObservationSource,
        policy: SourcePolicy | None = None,
        endpoint: LocationTarget | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        candidates: Sequence[LocationCandidate] | None = None,
        observed_at: datetime | None = None,
    ) -> tuple[Resolution, bool]:
        """Judge ``ip`` for ``proxy`` (see :meth:`judge`) and record the sighting; the resolution, and whether a persisted field changed.

        A gateway's sighting is applied by a worker seconds later, and both the row's stamp and the observation keep the
        gateway's time.

        Args:
            policy: the owning project's source policy; None for the install's.
            endpoint: what the endpoint requested through the proxy reported about the exit, if anything.
            session_id: the client session the sighting served, when one did.
            observed_at: when the exit was seen, naive UTC; None for now.
        """
        observed_at = observed_at or utc_now()
        resolution, changed = self.judge(
            proxy, ip, policy=policy, endpoint=endpoint, candidates=candidates, checked_at=observed_at
        )
        self.record(
            self._observation(
                proxy, ip, resolution, source=source, project_id=project_id, endpoint=endpoint, session_id=session_id,
                observed_at=observed_at,
            )
        )
        self._log_conflict(proxy, ip, resolution, source.value)
        return resolution, changed

    def _observation(
        self,
        proxy: Proxy,
        ip: str,
        resolution: Resolution,
        *,
        source: ObservationSource,
        project_id: str | None,
        endpoint: LocationTarget | None,
        session_id: str | None,
        observed_at: datetime,
    ) -> IpObservation:
        """The sighting record for ``ip`` behind ``proxy`` as ``resolution`` judged it."""
        return IpObservation(
            observed_at=observed_at,
            proxy_id=proxy.id,
            connector_id=proxy.connector_id,
            project_id=project_id,
            session_id=session_id,
            source=source,
            ip=ip,
            claimed_country=resolution.claimed_country,
            endpoint_country=endpoint.country if endpoint else None,
            resolved_country=resolution.resolved_country,
            resolved_source=resolution.resolved_source,
            country_conflict=resolution.country_conflict,
            claimed_state=resolution.claimed_state,
            claimed_city=resolution.claimed_city,
            resolved_state=resolution.resolved_state,
            resolved_city=resolution.resolved_city,
            state_conflict=resolution.state_conflict,
            city_conflict=resolution.city_conflict,
        )

    def record_sighting(
        self,
        proxy: Proxy,
        ip: str,
        *,
        source: ObservationSource,
        policy: SourcePolicy | None = None,
        endpoint: LocationTarget | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        candidates: Sequence[LocationCandidate] | None = None,
        observed_at: datetime | None = None,
    ) -> Resolution:
        """Resolve ``ip`` seen behind ``proxy`` and record the sighting without touching the row.

        For a dynamic-sessions gateway row and for an exit the row already carries: accuracy and unique exits still learn of it.
        """
        resolution = self.resolve_ip(
            ip, policy=policy, claimed=self.claimed_place_of(proxy), endpoint=endpoint, candidates=candidates
        )
        self.record(
            self._observation(
                proxy, ip, resolution, source=source, project_id=project_id, endpoint=endpoint, session_id=session_id,
                observed_at=observed_at or utc_now(),
            )
        )
        self._log_conflict(proxy, ip, resolution, source.value)
        return resolution

    def rejudge(
        self,
        proxy: Proxy,
        ip: str,
        *,
        policy: SourcePolicy | None = None,
        endpoint: LocationTarget | None = None,
    ) -> tuple[Resolution, bool]:
        """Re-run attribution for the exit already on ``proxy`` after a database changed, and record a judgement, not a sighting.

        Nothing was observed: no log row or hand-out, and the row keeps its ``location_checked_at``.
        """
        resolution, changed = self.judge(proxy, ip, policy=policy, endpoint=endpoint)
        self.record(
            ExitJudgement(
                proxy_id=proxy.id,
                connector_id=proxy.connector_id,
                ip=ip,
                claimed_country=resolution.claimed_country,
                resolved_country=resolution.resolved_country,
                resolved_source=resolution.resolved_source,
                country_conflict=resolution.country_conflict,
                claimed_state=resolution.claimed_state,
                claimed_city=resolution.claimed_city,
                resolved_state=resolution.resolved_state,
                resolved_city=resolution.resolved_city,
                state_conflict=resolution.state_conflict,
                city_conflict=resolution.city_conflict,
            )
        )
        self._log_conflict(proxy, ip, resolution, JUDGEMENT_SOURCE)
        return resolution, changed

    def judge(
        self,
        proxy: Proxy,
        ip: str,
        *,
        policy: SourcePolicy | None = None,
        endpoint: LocationTarget | None = None,
        candidates: Sequence[LocationCandidate] | None = None,
        checked_at: datetime | None = None,
    ) -> tuple[Resolution, bool]:
        """Resolve ``ip`` for ``proxy`` and write the result to its metadata alone; the resolution, and whether a field changed.

        Routing gets the independent answer at each level, except a value set by hand and a claim the project routes on.

        Args:
            checked_at: when the exit was seen, naive UTC, which becomes the row's ``location_checked_at``; None keeps the stamp.
        """
        policy = policy or self.default_policy
        vendor_country = self.claimed_country_of(proxy)
        manual = proxy.metadata.get(META_COUNTRY_SOURCE) == MANUAL_SOURCE
        pinned = proxy.manual_location
        resolution = self.resolve_ip(
            ip, policy=policy, claimed=self.claimed_place_of(proxy), endpoint=endpoint, candidates=candidates
        )
        contradicted_on_claim = policy.contradicted == ContradictedAction.VENDOR
        open_on_claim = policy.unknown == UnknownAction.VENDOR

        def routed(
            resolved: str | None, source: GeoSourceKind | None, conflict: bool | None, claimed: str | None
        ) -> tuple[str | None, GeoSourceKind | None]:
            """The routing value for one level: the evidence, or the claim where the project chose it.

            A confirmed level carries the claim with its confirming source, so a ``-city-`` request reaches an exit within the radius.
            """
            if claimed and ((contradicted_on_claim and conflict) or (open_on_claim and conflict is None)):
                return claimed, GeoSourceKind.VENDOR
            if claimed and conflict is False:
                return claimed, source
            return resolved, source

        before = self._snapshot(proxy)
        proxy.metadata[META_DISCOVERED_IP] = ip
        if vendor_country:
            proxy.metadata[META_VENDOR_COUNTRY] = vendor_country
        country, country_source = routed(
            resolution.resolved_country, resolution.resolved_source, resolution.country_conflict, resolution.claimed_country
        )
        if country and not manual:
            proxy.metadata[META_COUNTRY] = country
            proxy.metadata[META_COUNTRY_SOURCE] = country_source.value if country_source else None
        for key, source_key, pin, (value, source) in (
            (META_STATE, META_STATE_SOURCE, pinned.get("state_code"), routed(resolution.resolved_state, resolution.resolved_state_source, resolution.state_conflict, resolution.claimed_state)),
            (META_CITY, META_CITY_SOURCE, pinned.get("city"), routed(resolution.resolved_city, resolution.resolved_city_source, resolution.city_conflict, resolution.claimed_city)),
        ):
            if pin:
                continue  # the pin is the routing value; attribution only verifies it
            if value:
                proxy.metadata[key] = value
                proxy.metadata[source_key] = source.value if source else None
            else:
                proxy.metadata.pop(key, None)
                proxy.metadata.pop(source_key, None)
        if resolution.location is not None:
            proxy.metadata[META_LOCATION] = resolution.location.model_dump(exclude_none=True)
        else:
            proxy.metadata.pop(META_LOCATION, None)
        proxy.metadata[META_LOCATION_CONFLICT] = resolution.country_conflict is True
        for key, conflict, claimed in (
            (META_LOCATION_VERDICT, resolution.country_conflict, resolution.claimed_country),
            (META_STATE_VERDICT, resolution.state_conflict, resolution.claimed_state),
            (META_CITY_VERDICT, resolution.city_conflict, resolution.claimed_city),
        ):
            verdict = LocationVerdict.of(conflict, claimed)
            if verdict is None:
                proxy.metadata.pop(key, None)
            else:
                proxy.metadata[key] = verdict.value
        proxy.metadata[META_LOCATION_CANDIDATES] = resolution.compact_candidates()
        if checked_at is not None:
            proxy.metadata[META_LOCATION_CHECKED_AT] = checked_at.isoformat()
        return resolution, self._snapshot(proxy) != before

    @staticmethod
    def _log_conflict(proxy: Proxy, ip: str, resolution: Resolution, source: str) -> None:
        if resolution.country_conflict:
            logger.info(
                "Vendor location contradicted",
                proxy_id=proxy.id,
                ip=ip,
                claimed=resolution.claimed_country,
                resolved=resolution.resolved_country,
                source=source,
            )

    def record(self, item: IpObservation | ExitJudgement) -> None:
        """Buffer one observation or judgement for the observations stream."""
        self._observation_recorder.record(item)

    @staticmethod
    def _snapshot(proxy: Proxy) -> tuple[Any, ...]:
        return tuple(repr(proxy.metadata.get(key)) for key in _TRACKED_KEYS)

    # --- admin helpers -------------------------------------------------------------

    def explain(
        self, ip: str, claimed: LocationTarget | None = None, policy: SourcePolicy | None = None
    ) -> Resolution:
        """Resolution for the admin "test an IP" box: databases plus an optional claim."""
        return self.resolve_ip(ip, policy=policy, claimed=claimed)
