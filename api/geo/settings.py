# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The install-wide attribution settings row, cached per process, seeded from the config file on a fresh install, and the
admin API over it.

The api and workers read it again on its change entry, the controller adopts its compile's copy and a gateway the feed's.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError

from api.core.config import Settings
from api.db.geo_repository import GeoSettingsRepository
from api.db.repository import ProjectRepository
from api.db.session import SessionFactory
from api.db.streams import ChangeKind
from api.geo.models import GeoDatabaseKind, GeoSettings, SourcePolicy, normalize_country
from api.geo.readers import is_ip
from api.models.geo import GeoSettingsResponse, LookupCandidate, LookupRequest, LookupResponse
from api.models.location import LocationTarget
from api.services.errors import InputRejectedError, NotFoundError

if TYPE_CHECKING:
    from api.core.audit import Actor
    from api.db.streams import ChangeLog
    from api.geo.reattribution import ReattributionService
    from api.geo.service import GeoService

logger = structlog.get_logger()


def defaults_from_config(settings: Settings) -> GeoSettings:
    """What a fresh install starts with, from the config file."""
    seed: dict[str, Any] = {
        "echo_url": settings.geo_lookup_url,
        "echo_ip_path": settings.geo_lookup_ip_path,
        "echo_country_path": settings.geo_lookup_country_path or None,
        "echo_timeout_seconds": settings.geo_lookup_timeout_seconds,
    }
    seed.update(settings.geo_policy_defaults or {})
    try:
        return GeoSettings(**seed)
    except ValidationError as exc:
        logger.error("Invalid geo.defaults in config, using built-in settings", error=str(exc))
        return GeoSettings()


class GeoSettingsStore:
    """Read-through cache of the ``geo_settings`` row.

    Args:
        session_factory: Postgres, for :meth:`load` and :meth:`save`; None on a gateway.
    """

    def __init__(self, settings: Settings, session_factory: SessionFactory | None) -> None:
        self._config = settings
        self._session_factory = session_factory
        self._settings = defaults_from_config(settings)
        self._from_database = False

    @property
    def settings(self) -> GeoSettings:
        return self._settings

    @property
    def default_policy(self) -> SourcePolicy:
        return self._settings.default_policy

    @property
    def from_database(self) -> bool:
        """Whether an admin has saved the row (else these are the config defaults)."""
        return self._from_database

    def adopt(self, settings: GeoSettings, *, stored: bool) -> bool:
        """Use ``settings`` from now on, ``stored`` saying an admin saved them (else they are config defaults); True when they changed.

        Diff-only: settings equal to the ones in use change nothing and log nothing, so a caller can adopt on every sync.
        """
        if settings == self._settings and stored == self._from_database:
            return False
        self._settings = settings
        self._from_database = stored
        logger.info("Geo settings adopted", stored=stored, conflict_rule=settings.default_conflict_rule.value)
        return True

    async def load(self) -> GeoSettings:
        """Read the row and adopt it, or the config defaults when there is none; keep the current settings when Postgres fails."""
        if self._session_factory is None:
            return self._settings
        try:
            async with self._session_factory() as session:
                row = await GeoSettingsRepository(session).get()
        except Exception as exc:
            logger.warning("Could not load geo settings", error=str(exc))
            return self._settings
        if row is None:
            self.adopt(defaults_from_config(self._config), stored=False)
        else:
            self.adopt(row, stored=True)
        return self._settings

    async def save(self, settings: GeoSettings, actor: Actor) -> GeoSettings:
        """Persist and adopt ``settings``, recorded as ``actor``'s change; the caller announces the save, which is how
        other processes learn of it."""
        if self._session_factory is None:
            self._settings = settings
            return settings
        async with self._session_factory() as session:
            repository = GeoSettingsRepository(session)
            # Until an admin first saves, the install runs on the config defaults: that save changes them.
            stored = await repository.get()
            before = stored if stored is not None else defaults_from_config(self._config)
            await repository.save(settings, updated_by=actor.name)
            await actor.updated(session, before, settings)
            await session.commit()
        self._settings = settings
        self._from_database = True
        logger.info("Geo settings saved", updated_by=actor.name, conflict_rule=settings.default_conflict_rule.value)
        return settings


class GeoSettingsService:
    """The attribution settings as admins read and replace them, and a lookup of one IP under them.

    Args:
        geo: This process's attribution: its settings and database stores, and the lookups.
        echo_enabled: Whether this install answers echo requests (``geo_echo_enabled``), shown beside the settings.
        echo_trusted_proxies: The proxies echo takes a forwarded client address from (``geo_echo_trusted_proxies``).
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        geo: GeoService,
        changes: ChangeLog,
        reattribution: ReattributionService,
        echo_enabled: bool,
        echo_trusted_proxies: Sequence[str],
    ) -> None:
        self._session_factory = session_factory
        self._geo = geo
        self._changes = changes
        self._reattribution = reattribution
        self._echo_enabled = echo_enabled
        self._echo_trusted_proxies = tuple(echo_trusted_proxies)

    async def get(self) -> GeoSettingsResponse:
        """The attribution policy as stored, read and adopted first, and the databases this api process has open;
        while Postgres cannot be read, the settings in memory."""
        await self._geo.settings_store.load()
        return self._answer()

    async def replace(self, settings: GeoSettings, actor: Actor) -> GeoSettingsResponse:
        """Replace the install-wide attribution settings, which every process adopts on their change entry; a change
        to the policy projects inherit (the conflict rule and the city radius) re-judges every proxy."""
        store = self._geo.settings_store
        # Compared with the stored row, read first: this process's copy may lag a
        # save made through another api, and comparing with it could skip the
        # re-judging this save calls for.
        await store.load()
        inherited = store.settings.default_policy
        await store.save(settings, actor)
        await self._changes.record(ChangeKind.GEO_SETTINGS)
        if settings.default_policy != inherited:
            await self._reattribution.request_all("default policy changed")
        return self._answer()

    async def lookup(self, query: LookupRequest) -> LookupResponse:
        """Resolve one IP with the databases this api process has open, optionally against a vendor claim, under the
        install's policy or a project's. An IP or a claimed country that is not one is refused before the project is
        looked up."""
        ip = query.ip.strip()
        if not is_ip(ip):
            raise InputRejectedError("Not a valid IP address")
        claimed_country = normalize_country(query.claimed_country) if query.claimed_country else None
        if query.claimed_country and claimed_country is None:
            raise InputRejectedError("claimed_country must be a two-letter ISO code")
        claimed = LocationTarget.reported(claimed_country, query.claimed_state, query.claimed_city)
        policy = self._geo.default_policy
        if query.project_id:
            async with self._session_factory() as session:
                project = await ProjectRepository(session).get_by_id(query.project_id)
            if project is None:
                raise NotFoundError("Project not found")
            policy = project.source_policy(policy)
        resolution = self._geo.explain(ip, claimed, policy)
        return LookupResponse(
            ip=ip,
            resolution=resolution,
            policy=policy,
            candidates=[LookupCandidate.of(c) for c in resolution.candidates],
            # IP databases only: a GeoNames file answers no IP.
            databases_loaded=sum(1 for d in self._geo.database_store.loaded if d.kind != GeoDatabaseKind.PLACES),
        )

    def _answer(self) -> GeoSettingsResponse:
        """The settings in force here, with the echo settings and the databases this process has open."""
        store = self._geo.settings_store
        return GeoSettingsResponse(
            settings=store.settings,
            from_database=store.from_database,
            echo_enabled=self._echo_enabled,
            echo_trusted_proxies=list(self._echo_trusted_proxies),
            databases_loaded=self._geo.database_store.loaded,
        )
