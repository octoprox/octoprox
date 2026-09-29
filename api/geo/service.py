# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Applies IP attribution to proxies and feeds the observation pipeline.

Judgement is per project: callers pass the :class:`SourcePolicy` of the
project owning the proxy (the attributor looks it up); without one the
install default applies.

Every code path that learns an exit IP ends up in
:meth:`GeoService.apply_observation`: discovery in the provider syncer, the
static-proxy exit lookup, the health checker, the locate button and the
offline re-attribution after a database change. Preflight goes through
:meth:`GeoService.resolve_ip` and :meth:`GeoService.record` because a
session's exit is not a property of the proxy row.

The service holds no I/O of its own: databases are memory-mapped by the
store, the policy is a cached row, and observations are appended to a buffer.
Callers persist the proxy when ``apply_observation`` reports a change.
"""

from __future__ import annotations

from typing import Any

import structlog

from api.core import utc_now
from api.core.config import Settings
from api.geo.models import (
    JUDGEMENT_SOURCE,
    META_CITY,
    META_CITY_SOURCE,
    META_COUNTRY_SOURCE,
    META_ENDPOINT_CITY,
    META_ENDPOINT_COUNTRY,
    META_ENDPOINT_STATE,
    META_LOCATION,
    META_LOCATION_CANDIDATES,
    META_LOCATION_CHECKED_AT,
    META_LOCATION_CONFLICT,
    META_STATE,
    META_STATE_SOURCE,
    META_VENDOR_CITY,
    META_VENDOR_COUNTRY,
    META_VENDOR_STATE,
    ExitJudgement,
    GeoSettings,
    IpObservation,
    LocationCandidate,
    ObservationSource,
    Resolution,
    SourcePolicy,
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

MANUAL_SOURCE = "manual"

# Metadata keys apply_observation may change, compared to decide whether the caller must persist.
_TRACKED_KEYS = (
    META_COUNTRY,
    META_COUNTRY_SOURCE,
    META_VENDOR_COUNTRY,
    META_STATE,
    META_STATE_SOURCE,
    META_CITY,
    META_CITY_SOURCE,
    META_LOCATION,
    META_LOCATION_CONFLICT,
    META_LOCATION_CANDIDATES,
    META_DISCOVERED_IP,
)


class GeoService:
    """Attribution for one process."""

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

    async def apply_settings_change(self) -> None:
        """The settings row changed on some instance; reload it."""
        await self._settings_store.load()

    async def resync(self) -> None:
        """Safety net alongside the periodic full reload: re-read settings and databases."""
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
    ) -> Resolution:
        """Resolve one IP from the databases plus whatever the caller learned.

        ``policy`` defaults to the install default; callers attributing a
        proxy pass its project's policy. ``claimed`` is what was promised
        about the exit at any level (the vendor's word, or the place a
        request required); ``endpoint`` what an independent endpoint
        requested through the proxy reported.
        """
        candidates = self.attribute(ip)
        vendor = vendor_candidate(claimed)
        if vendor is not None:
            candidates.append(vendor)
        independent = endpoint_candidate(endpoint)
        if independent is not None:
            candidates.append(independent)
        return resolve(candidates, policy or self.default_policy)

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
    def claimed_place_of(cls, proxy: Proxy) -> LocationTarget | None:
        """Everything promised about a proxy's exit, per level.

        The country the vendor listed or the slot was provisioned for; the
        state and city the vendor's list or discovery endpoint named
        (``vendor_state``, ``vendor_city``) or a request asked for and a
        dynamic row was rendered with (``geo_state``, ``geo_city``); and,
        where the vendor said nothing, what an operator pinned by hand. It is
        what attribution judges and what preflight verifies against. Every
        value was normalised when it was written, so none is touched here.
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
    ) -> tuple[Resolution, bool]:
        """Resolve ``ip`` for ``proxy``, write the result to its metadata and record the sighting.

        ``policy`` is the owning project's source policy (default: the
        install's); ``endpoint`` is what the endpoint requested through the
        proxy reported about the exit, if any. Returns the resolution and
        whether any persisted field changed, so the caller knows whether to
        write the proxy.
        """
        resolution, changed = self._attribute(proxy, ip, policy=policy, endpoint=endpoint)
        self.record(self._observation(proxy, ip, resolution, source=source, project_id=project_id, endpoint=endpoint))
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
        session_id: str | None = None,
    ) -> IpObservation:
        """The sighting record for ``ip`` behind ``proxy`` as ``resolution`` judged it."""
        return IpObservation(
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
            candidates=resolution.compact_candidates(),
            instance_id=self._settings.instance_id,
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
    ) -> Resolution:
        """Resolve ``ip`` seen behind ``proxy`` and record the sighting without touching the row.

        For a dynamic-sessions gateway row: the exit belongs to one vendor
        session, not to the row, so the row's country, location and conflict
        flag are left alone while accuracy and unique exits still learn of it.
        """
        resolution = self.resolve_ip(ip, policy=policy, claimed=self.claimed_place_of(proxy), endpoint=endpoint)
        self.record(
            self._observation(
                proxy, ip, resolution, source=source, project_id=project_id, endpoint=endpoint, session_id=session_id,
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
        """Re-run attribution for the exit already on ``proxy`` and record a judgement, not a sighting.

        Used after a database changed. Nothing was observed: the IP is the one
        the proxy already had, so the exit's verdict is rewritten and no log
        row, hand-out or sighting time is produced.
        """
        resolution, changed = self._attribute(proxy, ip, policy=policy, endpoint=endpoint)
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
                instance_id=self._settings.instance_id,
            )
        )
        self._log_conflict(proxy, ip, resolution, JUDGEMENT_SOURCE)
        return resolution, changed

    def _attribute(
        self,
        proxy: Proxy,
        ip: str,
        *,
        policy: SourcePolicy | None,
        endpoint: LocationTarget | None,
    ) -> tuple[Resolution, bool]:
        """Resolve ``ip`` for ``proxy`` and write the result to its metadata.

        The claim is read off the proxy (see ``claimed_place_of``). A country,
        state or city set by hand is kept as the routing value and treated as
        the claim to verify when the vendor made none. Otherwise routing gets
        what the policy resolved at each level, with its source. Returns the
        resolution and whether any persisted field changed.
        """
        vendor_country = self.claimed_country_of(proxy)
        manual = proxy.metadata.get(META_COUNTRY_SOURCE) == MANUAL_SOURCE
        pinned = proxy.manual_location
        resolution = self.resolve_ip(ip, policy=policy, claimed=self.claimed_place_of(proxy), endpoint=endpoint)

        before = self._snapshot(proxy)
        proxy.display_host = ip
        proxy.metadata[META_DISCOVERED_IP] = ip
        if vendor_country:
            proxy.metadata[META_VENDOR_COUNTRY] = vendor_country
        if resolution.resolved_country and not manual:
            proxy.metadata[META_COUNTRY] = resolution.resolved_country
            proxy.metadata[META_COUNTRY_SOURCE] = (
                resolution.resolved_source.value if resolution.resolved_source else None
            )
        for key, source_key, pin, value, source in (
            (META_STATE, META_STATE_SOURCE, pinned.get("state_code"), resolution.resolved_state, resolution.resolved_state_source),
            (META_CITY, META_CITY_SOURCE, pinned.get("city"), resolution.resolved_city, resolution.resolved_city_source),
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
        proxy.metadata[META_LOCATION_CANDIDATES] = resolution.compact_candidates()
        proxy.metadata[META_LOCATION_CHECKED_AT] = utc_now().isoformat()
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
        self._observation_recorder.record(item)

    @staticmethod
    def _snapshot(proxy: Proxy) -> tuple[Any, ...]:
        return (proxy.display_host, *(repr(proxy.metadata.get(key)) for key in _TRACKED_KEYS))

    # --- admin helpers -------------------------------------------------------------

    def explain(
        self, ip: str, claimed: LocationTarget | None = None, policy: SourcePolicy | None = None
    ) -> Resolution:
        """Resolution for the admin "test an IP" box: databases plus an optional claim."""
        return self.resolve_ip(ip, policy=policy, claimed=claimed)
