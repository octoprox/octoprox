# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The install-wide attribution settings row, cached per process, seeded from the config file on a fresh install.

The api and workers read it again on its change entry, the controller adopts its compile's copy and a gateway the feed's.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import ValidationError

from api.core.config import Settings
from api.db.geo_repository import GeoSettingsRepository
from api.db.session import SessionFactory
from api.geo.models import GeoSettings, SourcePolicy

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

    async def save(self, settings: GeoSettings, updated_by: str | None) -> GeoSettings:
        """Persist and adopt ``settings``; the caller announces the save, which is how other processes learn of it."""
        if self._session_factory is None:
            self._settings = settings
            return settings
        async with self._session_factory() as session:
            await GeoSettingsRepository(session).save(settings, updated_by=updated_by)
            await session.commit()
        self._settings = settings
        self._from_database = True
        logger.info("Geo settings saved", updated_by=updated_by, conflict_rule=settings.default_conflict_rule.value)
        return settings
