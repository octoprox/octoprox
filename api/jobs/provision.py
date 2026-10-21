# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""On-demand country slot groups: a worker adds the group a gateway asked for, keyed ``<connector_id>:<country>``.

A run holds the connector's row lock around its check and its insert, so runs for one connector on several workers
take turns and a group is added once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import structlog
from sqlalchemy import select

from api.core.roles import JobKind
from api.db.models import ConnectorModel
from api.db.repository import ConnectorRepository, ProxyRepository
from api.jobs.job import Job
from api.models.location import normalize_country_code
from api.providers.sdk.strategies import META_GEO_COUNTRY

if TYPE_CHECKING:
    from api.db.session import SessionFactory
    from api.jobs.job import JobBatch
    from api.models.proxy import Proxy
    from api.providers.registry import ProviderRegistry
    from api.services.proxies import ProxyService

logger = structlog.get_logger()

# Groups one worker provisions at once. A run is short (no vendor round
# trip), and runs for one connector wait for each other on its row lock.
PROVISION_CONCURRENCY = 4


class ProvisionTarget(NamedTuple):
    """The country slot group a gateway asks a connector for."""

    connector_id: str
    country: str

    @property
    def key(self) -> str:
        """The ``provision`` key: ``<connector_id>:<country>``, the country in upper case."""
        return f"{self.connector_id}:{self.country.strip().upper()}"

    @classmethod
    def parse(cls, key: str) -> ProvisionTarget | None:
        """The group a ``provision`` key asks for; None unless its country is a two-letter code."""
        connector_id, _, raw = key.rpartition(":")
        try:
            country = normalize_country_code(raw)
        except ValueError:
            return None
        return cls(connector_id, country) if connector_id and country else None


class ProvisionJob(Job):
    """Adds the country slot group a gateway asked for, unless the connector holds one by then: the ``provision`` kind's job.

    A key whose country is not a two-letter code is logged and skipped, so whatever writes the stream can add at most
    one group per code to a pool.
    """

    kind = JobKind.PROVISION
    concurrency = PROVISION_CONCURRENCY

    def __init__(self, session_factory: SessionFactory, registry: ProviderRegistry, proxies: ProxyService) -> None:
        self._session_factory = session_factory
        self._registry = registry
        self._proxies = proxies

    async def run(self, batch: JobBatch) -> None:
        """Provision the group each key of the batch asks for."""
        for key in batch.keys:
            target = ProvisionTarget.parse(key)
            if target is None:
                logger.warning("Unreadable provision key dropped", key=key)
                continue
            await self.provision(target.connector_id, target.country)

    async def provision(self, connector_id: str, country: str) -> list[Proxy]:
        """Store the connector's slot group for ``country`` (an upper-case code) unless it has one; the proxies added.

        Nothing is added for a connector that is gone, disabled, pending deletion, or not an "All countries" session pool.
        """
        async with self._session_factory() as session:
            locked = await session.execute(
                select(ConnectorModel.id).where(ConnectorModel.id == connector_id).with_for_update(key_share=True)
            )
            if locked.scalar_one_or_none() is None:
                return []
            found = await ConnectorRepository(session).get_with_credential(connector_id)
            if found is None:
                return []
            connector, credential = found
            if not connector.enabled or connector.pending_deletion:
                return []
            existing = await ProxyRepository(session).get_by_connector(connector_id)
            if any(proxy.metadata.get(META_GEO_COUNTRY) == country for proxy in existing):
                logger.debug("Country slot group already provisioned", connector_id=connector_id, country=country)
                return []
            provider = self._registry.descriptor_provider(connector, credential)
            if provider is None or not provider.accepts_request_country():
                logger.warning(
                    "Country slot group asked of a connector that provisions none",
                    connector_id=connector_id,
                    country=country,
                )
                return []
            group = await provider.provision_country(existing, country)
            # Stored in its own transaction while this one holds the connector,
            # so a second run for the connector reads the group once it has the lock.
            added = await self._proxies.bulk_add(group)
            await session.commit()
        if added:
            logger.info(
                "Provisioned country slot group on demand", connector_id=connector_id, country=country, slots=len(added)
            )
        return added
