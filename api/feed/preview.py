# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The routing previews: a router over the routing as a fresh gateway would build it from a feed compiled now.

The api builds one project's for its pages (Countries, the traffic split, exit reputation); the alerts job builds one
project's or every project's, the build off the event loop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

from api.routing.router import Router
from api.routing.snapshot import RoutingSnapshot

if TYPE_CHECKING:
    from collections.abc import Mapping

    from api.feed.compiler import FeedCompiler, PreviewRows
    from api.providers.registry import ProviderRegistry
    from api.services.read_model import RuntimeView


class RoutingPreview:
    """Routers over the routing as a fresh gateway would build it from a feed compiled now, quarantines and traffic
    blocks read from Redis: one project's for the api's pages, every project's for the alerts job.

    Args:
        off_loop: build the sections and the router in a thread, the rows read first on the event loop; the worker's
            choice, whose loop also runs the heartbeat and the other jobs.
    """

    def __init__(
        self,
        compiler: FeedCompiler,
        runtime_view: RuntimeView,
        registry: ProviderRegistry,
        *,
        default_strategy: str,
        off_loop: bool = False,
    ) -> None:
        self._compiler = compiler
        self._view = runtime_view
        self._registry = registry
        self._default_strategy = default_strategy
        self._off_loop = off_loop

    async def project(self, project_id: str) -> Router:
        """A router over one project's routing; an empty one when the project is gone."""
        quarantined, blocked = await self._runtime()
        return await self._build(await self._compiler.read_project(project_id), quarantined, blocked)

    async def every_project(self) -> Router:
        """A router over every project's routing, as :meth:`project` builds each."""
        quarantined, blocked = await self._runtime()
        return await self._build(await self._compiler.read_projects(), quarantined, blocked)

    async def _runtime(self) -> tuple[dict[str, datetime], dict[str, datetime]]:
        """The quarantines and the traffic blocks the traffic job stored, read side by side."""
        quarantined, blocked = await asyncio.gather(self._view.quarantined(), self._view.blocked())
        return quarantined, blocked

    async def _build(
        self, rows: PreviewRows, quarantined: Mapping[str, datetime], blocked: Mapping[str, datetime]
    ) -> Router:
        if self._off_loop:
            return await asyncio.to_thread(self._router, rows, quarantined, blocked)
        return self._router(rows, quarantined, blocked)

    def _router(
        self, rows: PreviewRows, quarantined: Mapping[str, datetime], blocked: Mapping[str, datetime]
    ) -> Router:
        """The sections, the snapshot and the router, all pure over the values already read."""
        sections = self._compiler.preview(rows, quarantined=quarantined, blocked_until=blocked)
        return Router(
            RoutingSnapshot.from_sections(sections, registry=self._registry), default_strategy=self._default_strategy
        )
