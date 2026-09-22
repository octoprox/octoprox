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
    META_COUNTRY_SOURCE,
    META_LOCATION,
    META_LOCATION_CANDIDATES,
    META_LOCATION_CHECKED_AT,
    META_LOCATION_CONFLICT,
    META_VENDOR_COUNTRY,
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
from api.models.proxy import Proxy
from api.providers.sdk.descriptor import IpDiscoverySpec
from api.providers.sdk.sources import IpDiscoverer
from api.providers.sdk.strategies import META_COUNTRY, META_DISCOVERED_IP, META_GEO

logger = structlog.get_logger()

MANUAL_SOURCE = "manual"

# Metadata keys apply_observation may change, compared to decide whether the caller must persist.
_TRACKED_KEYS = (
    META_COUNTRY,
    META_COUNTRY_SOURCE,
    META_VENDOR_COUNTRY,
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
        claimed_country: str | None = None,
        endpoint_country: str | None = None,
    ) -> Resolution:
        """Resolve one IP from the databases plus whatever the caller learned.

        ``policy`` defaults to the install default; callers attributing a
        proxy pass its project's policy.
        """
        candidates = self.attribute(ip)
        vendor = vendor_candidate(claimed_country)
        if vendor is not None:
            candidates.append(vendor)
        endpoint = endpoint_candidate(endpoint_country)
        if endpoint is not None:
            candidates.append(endpoint)
        return resolve(candidates, policy or self.default_policy)

    # --- applying to proxies -------------------------------------------------------

    @staticmethod
    def claimed_country_of(proxy: Proxy) -> str | None:
        """The vendor's claim for a proxy: its recorded claim, else the geo it was provisioned for."""
        for key in (META_VENDOR_COUNTRY, META_GEO):
            code = normalize_country(proxy.metadata.get(key))
            if code:
                return code
        return None

    def apply_observation(
        self,
        proxy: Proxy,
        ip: str,
        *,
        source: ObservationSource,
        policy: SourcePolicy | None = None,
        endpoint_country: str | None = None,
        project_id: str | None = None,
    ) -> tuple[Resolution, bool]:
        """Resolve ``ip`` for ``proxy``, write the result to its metadata and record the observation.

        The vendor's claim is read off the proxy (its listed country or the geo
        it was provisioned for). ``policy`` is the owning project's source
        policy (default: the install's). Returns the resolution and whether any
        persisted field changed, so the caller knows whether to write the
        proxy. A country set by hand (``country_source == "manual"``) is kept
        as the routing country and treated as the claim to verify when the
        vendor made none.
        """
        claimed = self.claimed_country_of(proxy)
        manual = proxy.metadata.get(META_COUNTRY_SOURCE) == MANUAL_SOURCE
        manual_country = normalize_country(proxy.metadata.get(META_COUNTRY)) if manual else None
        resolution = self.resolve_ip(
            ip,
            policy=policy,
            claimed_country=claimed or manual_country,
            endpoint_country=endpoint_country,
        )

        before = self._snapshot(proxy)
        # A hand-out is a new IP, or the first attribution of a proxy that
        # predates attribution; everything else re-judges an exit it already had.
        new_exit = (
            proxy.metadata.get(META_DISCOVERED_IP) != ip
            or META_LOCATION_CONFLICT not in proxy.metadata
        )
        proxy.display_host = ip
        proxy.metadata[META_DISCOVERED_IP] = ip
        if claimed:
            proxy.metadata[META_VENDOR_COUNTRY] = claimed
        if resolution.country and not manual:
            proxy.metadata[META_COUNTRY] = resolution.country
            proxy.metadata[META_COUNTRY_SOURCE] = (
                resolution.source.value if resolution.source else None
            )
        if resolution.location is not None:
            proxy.metadata[META_LOCATION] = resolution.location.model_dump(exclude_none=True)
        else:
            proxy.metadata.pop(META_LOCATION, None)
        proxy.metadata[META_LOCATION_CONFLICT] = resolution.conflict
        proxy.metadata[META_LOCATION_CANDIDATES] = resolution.compact_candidates()
        proxy.metadata[META_LOCATION_CHECKED_AT] = utc_now().isoformat()
        changed = self._snapshot(proxy) != before

        self.record(
            IpObservation(
                proxy_id=proxy.id,
                connector_id=proxy.connector_id,
                project_id=project_id,
                source=source,
                ip=ip,
                claimed_country=resolution.claimed_country,
                endpoint_country=normalize_country(endpoint_country),
                resolved_country=resolution.country,
                resolved_source=resolution.source,
                conflict=resolution.conflict,
                disagreement=resolution.disagreement,
                candidates=resolution.compact_candidates(),
                instance_id=self._settings.instance_id,
                new_exit=new_exit,
            )
        )
        if resolution.conflict:
            logger.info(
                "Vendor location contradicted",
                proxy_id=proxy.id,
                ip=ip,
                claimed=resolution.claimed_country,
                resolved=resolution.country,
                source=source.value,
            )
        return resolution, changed

    def flag_preflight_mismatch(
        self, proxy: Proxy, ip: str, expected: str, observed: str | None
    ) -> bool:
        """Record on a fixed-exit proxy that preflight saw it somewhere other than expected.

        Sets the same conflict flag attribution uses, so ``strict`` projects
        skip the proxy without another echo request. Returns whether anything changed.
        """
        before = self._snapshot(proxy)
        proxy.display_host = ip
        proxy.metadata[META_DISCOVERED_IP] = ip
        proxy.metadata[META_LOCATION_CONFLICT] = True
        proxy.metadata[META_LOCATION_CHECKED_AT] = utc_now().isoformat()
        if observed:
            candidates = [
                c
                for c in proxy.metadata.get(META_LOCATION_CANDIDATES) or []
                if c.get("source") != "preflight"
            ]
            candidates.append({"source": "preflight", "origin": "endpoint", "country": observed})
            proxy.metadata[META_LOCATION_CANDIDATES] = candidates
        return self._snapshot(proxy) != before

    def record(self, observation: IpObservation) -> None:
        self._observation_recorder.record(observation)

    @staticmethod
    def _snapshot(proxy: Proxy) -> tuple[Any, ...]:
        return (proxy.display_host, *(repr(proxy.metadata.get(key)) for key in _TRACKED_KEYS))

    # --- admin helpers -------------------------------------------------------------

    def explain(
        self, ip: str, claimed_country: str | None = None, policy: SourcePolicy | None = None
    ) -> Resolution:
        """Resolution for the admin "test an IP" box: databases plus an optional claim."""
        return self.resolve_ip(ip, policy=policy, claimed_country=claimed_country)
