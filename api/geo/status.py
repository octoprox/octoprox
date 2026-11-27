# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The attribution pipeline's health for the settings page: this api process, the observations stream, and every live
gateway as its last heartbeat reports it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.db.geo_repository import GeoDatabaseRepository, ObservationRepository
from api.models.geo import GatewayGeoStatus, GeoStatusResponse

if TYPE_CHECKING:
    from api.db.session import SessionFactory
    from api.geo.service import GeoService
    from api.models.system import GatewaySnapshots


class GeoStatusService:
    """Reports what this api process has open and buffered, what Postgres holds, and each live gateway's attribution."""

    def __init__(
        self, session_factory: SessionFactory, *, geo: GeoService, gateways: GatewaySnapshots
    ) -> None:
        self._session_factory = session_factory
        self._geo = geo
        self._gateways = gateways

    async def get(self) -> GeoStatusResponse:
        """Pipeline health for the settings page: this api process, the observations stream, and every live gateway."""
        store = self._geo.database_store
        async with self._session_factory() as session:
            records = await GeoDatabaseRepository(session).get_all()
            stored = await ObservationRepository(session).estimated_count()
        recorder = self._geo.observation_recorder
        gateways = [
            GatewayGeoStatus.of(instance_id, gateway)
            for instance_id, gateway in (await self._gateways()).items()
        ]
        return GeoStatusResponse(
            databases_loaded=len(store.loaded),
            databases_total=len(records) + len(store.config_only({r.id for r in records})),
            load_errors=store.load_errors,
            policy_from_database=self._geo.settings_store.from_database,
            pending_observations=recorder.pending,
            published_observations=recorder.published,
            dropped_observations=recorder.dropped,
            queued_observations=await recorder.queued(),
            stored_observations=stored,
            preflight_checks=sum(g.preflight_checks for g in gateways),
            preflight_rejections=sum(g.preflight_rejections for g in gateways),
            gateways=gateways,
        )
