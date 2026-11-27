# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Detect: a proxy's exit IP and location looked up now by requesting through it from this process, and recorded on it.

A session slot the project's location rules refuse is rotated, as any sighting of it would be; the answer still shows
what was found.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from api.geo.models import ObservationSource
from api.services.errors import NotFoundError, UpstreamError

if TYPE_CHECKING:
    from api.jobs.attribution import Attribution
    from api.models.proxy import ProxyView
    from api.services.read_model import RuntimeView


class DetectService:
    """Looks up a proxy's exit through it and records what attribution judges of it.

    Args:
        attribution: The process's attributor, with a Postgres-backed store per call (``store()``).
        views: The read model the proxy is shown from, before the lookup and after it.
    """

    def __init__(self, attribution: Attribution, *, views: RuntimeView) -> None:
        self._attribution = attribution
        self._views = views

    async def locate(self, project_id: str, proxy_id: str) -> ProxyView:
        """The proxy of the project with its exit judged now and recorded; refused when the request through it fails."""
        view = await self._views.proxy(project_id, proxy_id)
        if view is None:
            raise NotFoundError("Proxy not found")
        store = self._attribution.store()
        await store.load([proxy_id])
        judged = await self._attribution.attributor.enrich(
            store, proxy_id, source=ObservationSource.MANUAL
        )
        await store.flush()
        if judged is None:
            raise UpstreamError("Could not determine the exit location through this proxy")
        located = await self._views.proxy(project_id, proxy_id)
        if located is None:
            # Rotated: the stored row is gone, so show the judgement on the row as it was
            # (the judged copy carries its credentials filled in, never shown).
            located = replace(
                view, proxy=view.proxy.model_copy(update={"metadata": judged.metadata})
            )
        return located
