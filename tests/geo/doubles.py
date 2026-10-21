# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Test doubles for the seams around attribution: the batch store, and what preflight hands to the gateway.

:class:`BatchStore` is an in-memory ``ProxyStore`` and ``ProxyPages`` with the
semantics their docstrings promise, and reads as the contract a
Postgres-backed store (``api.jobs.attribution.PostgresProxyStore``) keeps:
rows are loaded as copies, saves are queued, and :meth:`BatchStore.flush`
writes each saved row once, only while it still holds the stamp it was loaded
with and never from a sighting older than that, then rotates and drops
verdicts only for the rows whose write applied. :class:`Sightings` and
:class:`Retired` stand in for the gateway's sighting publisher and local
exclusions, and :func:`logged_refusals` collects what the exit verifier logs
for each request it refuses.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from unittest.mock import MagicMock, patch

from api.geo.attributor import ProxyAttributor
from api.jobs.messages import SightingMessage
from api.models.connector import Connector
from api.models.project import Project
from api.models.proxy import Proxy


@dataclass
class _Save:
    checked_at: datetime | None
    rotate: bool
    drop_verdict: bool


class BatchStore:
    """``rows`` play Postgres; :meth:`load` and :meth:`load_page` take copies of them as the batch."""

    def __init__(self, projects: Iterable[Project] = (), connectors: Iterable[Connector] = ()) -> None:
        self.projects = {project.id: project for project in projects}
        self.connectors = {connector.id: connector for connector in connectors}
        self.rows: dict[str, Proxy] = {}
        self._batch: dict[str, Proxy] = {}
        self._loaded: dict[str, dict[str, object]] = {}
        # Each loaded row's location_checked_at as loaded: a save is written only while the row still holds it.
        self._stamps: dict[str, datetime | None] = {}
        self._pending: dict[str, _Save] = {}
        # Every save call, in order: (proxy id, checked_at, rotate, drop_verdict).
        self.saves: list[tuple[str, datetime | None, bool, bool]] = []
        # What flushes did: rows written, rows refused as older, rows deleted, verdicts dropped.
        self.written: list[str] = []
        self.refused: list[str] = []
        self.deleted: list[str] = []
        self.dropped_verdicts: list[tuple[str, str]] = []
        self.flushes = 0

    def add(self, proxy: Proxy) -> Proxy:
        """Put a row in "Postgres" and in the current batch; returns the batch's copy."""
        self.rows[proxy.id] = proxy.model_copy(deep=True)
        self.load([proxy.id])
        return self._batch[proxy.id]

    def load(self, proxy_ids: Iterable[str]) -> None:
        """Take copies of the named rows into the batch, as a sighting batch's load does."""
        for proxy_id in proxy_ids:
            row = self.rows.get(proxy_id)
            if row is not None:
                self._batch[proxy_id] = row.model_copy(deep=True)
                self._loaded[proxy_id] = dict(row.model_copy(deep=True).metadata)
                self._stamps[proxy_id] = ProxyAttributor.checked_at(row)

    async def load_page(
        self, after: str | None, limit: int, *, connector_id: str | None, project_id: str | None
    ) -> list[Proxy]:
        ids = sorted(
            proxy_id
            for proxy_id, row in self.rows.items()
            if (after is None or proxy_id > after)
            and (connector_id is None or row.connector_id == connector_id)
            and (project_id is None or self._project_id(row) == project_id)
        )[:limit]
        self._batch = {}
        self._loaded = {}
        self._stamps = {}
        self.load(ids)
        return [self._batch[proxy_id] for proxy_id in ids]

    def _project_id(self, row: Proxy) -> str | None:
        connector = self.connectors.get(row.connector_id)
        return connector.project_id if connector else None

    def get_proxy(self, proxy_id: str) -> Proxy | None:
        pending = self._pending.get(proxy_id)
        if pending is not None and pending.rotate:
            return None
        return self._batch.get(proxy_id)

    def get_connector(self, connector_id: str) -> Connector | None:
        return self.connectors.get(connector_id)

    def get_project(self, project_id: str) -> Project | None:
        return self.projects.get(project_id)

    def save(self, proxy: Proxy, *, checked_at: datetime | None, rotate: bool = False, drop_verdict: bool = False) -> None:
        self.saves.append((proxy.id, checked_at, rotate, drop_verdict))
        self._pending[proxy.id] = _Save(checked_at, rotate, drop_verdict)

    async def flush(self) -> None:
        self.flushes += 1
        won: list[str] = []
        for proxy_id in sorted(self._pending):
            save = self._pending[proxy_id]
            stored = self.rows.get(proxy_id)
            if stored is None:
                continue
            read = self._stamps[proxy_id]
            older = save.checked_at is not None and read is not None and save.checked_at < read
            if older or ProxyAttributor.checked_at(stored) != read:
                # Judged from a sighting older than the row as read, or the row
                # moved on since: the patch is not right over what is stored.
                self.refused.append(proxy_id)
                continue
            loaded = self._loaded[proxy_id]
            current = self._batch[proxy_id].metadata
            changed = {key: value for key, value in current.items() if loaded.get(key) != value}
            removed = {key for key in loaded if key not in current}
            metadata = {key: value for key, value in stored.metadata.items() if key not in removed}
            metadata.update(changed)
            stored.metadata = metadata
            self.written.append(proxy_id)
            won.append(proxy_id)
        for proxy_id in won:
            save = self._pending[proxy_id]
            project_id = self._project_id(self.rows[proxy_id]) or ""
            if save.rotate:
                del self.rows[proxy_id]
                self.deleted.append(proxy_id)
            if save.drop_verdict:
                self.dropped_verdicts.append((project_id, proxy_id))
        self._pending = {}
        self._batch = {}
        self._loaded = {}
        self._stamps = {}


class Sightings:
    """The gateway's sighting publisher as preflight sees it: what was pushed for the workers."""

    def __init__(self) -> None:
        self.messages: list[SightingMessage] = []

    def push(self, message: SightingMessage) -> None:
        self.messages.append(message)


class Retired:
    """The gateway's local exclusions as preflight sees it: the slots taken out of routing."""

    def __init__(self) -> None:
        self.ids: list[str] = []

    def retire(self, proxy_id: str) -> None:
        self.ids.append(proxy_id)


# What the exit verifier logs for every request it refuses.
REFUSED = "Request refused for its exit location"


class Refusals:
    """The refusals the exit verifier logged, each as the fields it logged."""

    def __init__(self, log: MagicMock) -> None:
        self._log = log

    @property
    def events(self) -> list[dict[str, object]]:
        return [call.kwargs for call in self._log.debug.call_args_list if call.args == (REFUSED,)]


@contextmanager
def logged_refusals() -> Iterator[Refusals]:
    """Collect the verifier's refusals while the block runs."""
    with patch("api.geo.verifier.logger") as log:
        yield Refusals(log)
