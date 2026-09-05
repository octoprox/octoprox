# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Bridges admin-authored descriptors in Postgres with the in-memory registry."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.repository import ProviderDescriptorRepository
from api.models.provider import ProviderRecord
from api.providers.registry import ProviderRegistry
from api.providers.sdk.loader import DescriptorLoadError, descriptor_from_dict

logger = structlog.get_logger()

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class ProviderStore:
    """Loads custom descriptors from the database into a registry."""

    def __init__(self, registry: ProviderRegistry, session_factory: SessionFactory) -> None:
        self._registry = registry
        self._session_factory = session_factory

    async def sync_all(self) -> int:
        """Replace the registry's custom providers with the enabled rows in Postgres."""
        async with self._session_factory() as session:
            records = await ProviderDescriptorRepository(session).get_all()
        descriptors = []
        for record in records:
            if not record.enabled:
                continue
            try:
                descriptors.append(descriptor_from_dict(record.spec))
            except DescriptorLoadError as exc:
                logger.error("Stored provider descriptor is invalid", provider_id=record.id, error=str(exc))
        self._registry.replace_custom(descriptors)
        logger.info("Loaded custom provider descriptors", count=len(descriptors))
        return len(descriptors)

    async def reload_one(self, provider_id: str, op: str | None) -> None:
        """Apply a cross-instance change notification for one descriptor."""
        if op == "removed":
            self._registry.unregister(provider_id)
            return
        async with self._session_factory() as session:
            record = await ProviderDescriptorRepository(session).get_by_id(provider_id)
        self.apply_record(record, provider_id)

    def apply_record(self, record: ProviderRecord | None, provider_id: str) -> None:
        """Register/unregister from a record without touching the database."""
        if record is None or not record.enabled:
            self._registry.unregister(provider_id)
            return
        try:
            descriptor = descriptor_from_dict(record.spec)
        except DescriptorLoadError as exc:
            logger.error("Stored provider descriptor is invalid", provider_id=provider_id, error=str(exc))
            self._registry.unregister(provider_id)
            return
        existing = self._registry.get(descriptor.id)
        if existing is not None and existing.source != "custom":
            logger.warning("Custom provider shadows a built-in id and was skipped", provider_id=descriptor.id)
            return
        self._registry.register_descriptor(descriptor, "custom", origin="database")
