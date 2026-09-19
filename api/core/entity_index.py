# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Indexed in-memory caches for the proxy manager.

The manager keeps every proxy and connector in memory and used to answer
"which proxies belong to this project" by scanning all of them. With
thousands of proxies across many projects that scan ran on every request.
These mappings behave like the plain ``dict`` they replace (``get``,
``pop``, ``update``, ``in``, iteration) and additionally keep a secondary
index so per-connector and per-project lookups touch only the rows involved.

The index is maintained on assignment and deletion. Objects mutated in
place (``Proxy.merge_definition_from`` may change ``connector_id``) must be
re-indexed with :meth:`reindex` afterwards.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, MutableMapping
from typing import Generic, TypeVar

from api.models.connector import Connector
from api.models.proxy import Proxy

T = TypeVar("T")


class _GroupedIndex(MutableMapping[str, T], Generic[T]):
    """``id -> entity`` mapping grouped by one key of the entity."""

    def __init__(self, items: Iterable[tuple[str, T]] | None = None) -> None:
        self._by_id: dict[str, T] = {}
        self._groups: dict[str, dict[str, T]] = {}
        self._group_of: dict[str, str] = {}
        if items is not None:
            for key, value in items:
                self[key] = value

    def _group_key(self, value: T) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    # --- MutableMapping ---------------------------------------------------------------

    def __getitem__(self, key: str) -> T:
        return self._by_id[key]

    def __setitem__(self, key: str, value: T) -> None:
        if key in self._by_id:
            self._unindex(key)
        self._by_id[key] = value
        group = self._group_key(value)
        self._groups.setdefault(group, {})[key] = value
        self._group_of[key] = group

    def __delitem__(self, key: str) -> None:
        self._unindex(key)
        del self._by_id[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._by_id)

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, key: object) -> bool:
        return key in self._by_id

    # --- index maintenance ------------------------------------------------------------

    def _unindex(self, key: str) -> None:
        group = self._group_of.pop(key)
        members = self._groups[group]
        del members[key]
        if not members:
            del self._groups[group]

    def reindex(self, key: str) -> None:
        """Re-file an entity whose group key was changed in place."""
        value = self._by_id[key]
        if self._group_of.get(key) != self._group_key(value):
            self[key] = value

    def in_group(self, group: str) -> list[T]:
        return list(self._groups.get(group, {}).values())

    def in_groups(self, groups: Iterable[str]) -> list[T]:
        result: list[T] = []
        for group in groups:
            result.extend(self._groups.get(group, {}).values())
        return result

    def remove_groups(self, groups: Iterable[str]) -> list[str]:
        """Drop every entity in the given groups; returns the removed ids."""
        removed: list[str] = []
        for group in list(groups):
            for key in list(self._groups.get(group, {})):
                del self[key]
                removed.append(key)
        return removed


class ProxyIndex(_GroupedIndex[Proxy]):
    """Proxies by id, grouped by ``connector_id``."""

    def _group_key(self, value: Proxy) -> str:
        return value.connector_id

    def for_connector(self, connector_id: str) -> list[Proxy]:
        return self.in_group(connector_id)

    def for_connectors(self, connector_ids: Iterable[str]) -> list[Proxy]:
        return self.in_groups(connector_ids)


class ConnectorIndex(_GroupedIndex[Connector]):
    """Connectors by id, grouped by ``project_id``."""

    def _group_key(self, value: Connector) -> str:
        return value.project_id

    def for_project(self, project_id: str) -> list[Connector]:
        return self.in_group(project_id)
