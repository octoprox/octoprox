# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The install-wide attribution settings row, cached per instance.

The config file seeds a fresh install (``geo.defaults`` plus the older
``proxy.geo_lookup`` echo settings, so existing installs keep their endpoint).
After that the admin panel owns the row: a save writes it and publishes
``geo_settings_changed``; every instance reloads it.

Per-project judgement (which source wins, when a vendor is contradicted) is
not here: it is on the project row, with :attr:`GeoSettings.default_policy`
as what a project inherits.
"""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import ValidationError

from api.core.config import Settings
from api.db.geo_repository import GeoSettingsRepository
from api.geo.models import GeoSettings, SourcePolicy
from api.geo.store import SessionFactory

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
    """Read-through cache of the ``geo_settings`` row."""

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

    async def load(self) -> GeoSettings:
        """Read the row, falling back to the config defaults when absent."""
        if self._session_factory is None:
            return self._settings
        try:
            async with self._session_factory() as session:
                stored = await GeoSettingsRepository(session).get()
        except Exception as exc:
            logger.warning("Could not load geo settings", error=str(exc))
            return self._settings
        if stored is None:
            self._settings = defaults_from_config(self._config)
            self._from_database = False
        else:
            self._settings = stored
            self._from_database = True
        return self._settings

    async def save(self, settings: GeoSettings, updated_by: str | None) -> GeoSettings:
        """Persist and adopt ``settings``. The caller publishes the change to peers."""
        if self._session_factory is None:
            self._settings = settings
            return settings
        async with self._session_factory() as session:
            await GeoSettingsRepository(session).save(settings, updated_by=updated_by)
            await session.commit()
        self._settings = settings
        self._from_database = True
        logger.info("Geo settings saved", updated_by=updated_by, sources=[s.value for s in settings.default_sources])
        return settings
