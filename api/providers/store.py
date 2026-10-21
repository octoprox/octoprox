# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Applies the enabled admin-authored descriptors in Postgres (``provider_descriptors``) to a process's registry, diff-only."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import structlog

from api.core.logging import error_text
from api.db.repository import ProviderDescriptorRepository
from api.db.session import SessionFactory
from api.providers.registry import ProviderRegistry
from api.providers.sdk.loader import DescriptorLoadError, descriptor_from_dict

logger = structlog.get_logger()


class ProviderStore:
    """Applies the custom descriptors to one registry.

    Args:
        session_factory: Postgres, for :meth:`sync_all`; None on a gateway.
    """

    def __init__(self, registry: ProviderRegistry, session_factory: SessionFactory | None) -> None:
        self._registry = registry
        self._session_factory = session_factory
        # The documents last applied: the same ones again are not even parsed.
        self._applied: list[dict[str, Any]] | None = None

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    def apply_specs(self, specs: Sequence[Mapping[str, Any]]) -> bool:
        """Make the registry's custom providers exactly ``specs``, the enabled descriptors' documents; True when it changed.

        A document that does not load is logged and skipped, and one that
        would shadow a built-in id is skipped with a warning; both are said
        once, when the documents change, not on every call.
        """
        wanted = [dict(spec) for spec in specs]
        if wanted == self._applied:
            return False
        descriptors = []
        for spec in wanted:
            try:
                descriptors.append(descriptor_from_dict(spec))
            except DescriptorLoadError as exc:
                logger.error("Stored provider descriptor is invalid", provider_id=spec.get("id"), error=error_text(exc))
        added, updated, removed = self._registry.replace_custom(descriptors)
        self._applied = wanted
        if not (added or updated or removed):
            return False
        logger.info("Custom provider descriptors synced", added=added, updated=updated, removed=removed)
        return True

    async def sync_all(self) -> bool:
        """Read the enabled descriptors from Postgres and apply them (:meth:`apply_specs`); False without Postgres."""
        if self._session_factory is None:
            return False
        async with self._session_factory() as session:
            records = await ProviderDescriptorRepository(session).get_all()
        return self.apply_specs([record.spec for record in records if record.enabled])

