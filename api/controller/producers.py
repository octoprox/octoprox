# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The controller's job producers: each periodic kind's keys published once a period, each at the second its hash picks.

Every producer takes its keys from the feed builder's last compile (:class:`JobKeys`), so no producer reads Postgres.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from api.core.loops import PeriodicLoop
from api.core.roles import JobKind
from api.core.workers import job_producer_name
from api.jobs.health import HealthCheckJob

if TYPE_CHECKING:
    from api.controller.feed_builder import FeedBuilder
    from api.core.leadership import Lease
    from api.feed.models import ConnectorSection
    from api.jobs.queue import JobQueue
    from api.providers.registry import ProviderRegistry

# The most seconds of slots one cycle publishes. After a stall the older
# seconds wait a period, so a controller that stalled sends no burst that
# would scale the workers up for nothing.
CATCH_UP_SECONDS = 5

# The one key an install-wide kind is published with: its job runs for the
# whole install.
INSTALL_WIDE_KEYS: tuple[str, ...] = ("all",)


def stable_jitter(key: str) -> float:
    """Where in a period ``key`` falls, in [0, 1): a digest, so the same on every process and every run."""
    digest = hashlib.blake2b(key.encode(), digest_size=8).digest()
    return int.from_bytes(digest) / 2**64


@dataclass(frozen=True)
class JobKeys:
    """The keys each periodic kind is published for, made anew by each compile: none of a compile's before the first."""

    # Proxies to health-check: those the health job wants to check.
    health: frozenset[str] = frozenset()
    # Enabled connectors whose provider syncs with its vendor.
    sync: frozenset[str] = frozenset()
    # Every cloud connector, enabled or not: a disabled or deleted one is
    # drained and emptied by its scale job.
    scale: frozenset[str] = frozenset()

    @classmethod
    def of(cls, connectors: Mapping[str, ConnectorSection], registry: ProviderRegistry) -> JobKeys:
        """The keys a compile's connectors call for, by ID; the registry says which connector types sync and which scale."""
        health: list[str] = []
        sync: list[str] = []
        scale: list[str] = []
        for connector_id, section in connectors.items():
            connector = section.connector
            if registry.is_cloud(connector.credential_type):
                scale.append(connector_id)
            if not connector.enabled:
                continue
            if registry.is_syncable(connector.credential_type):
                sync.append(connector_id)
            health.extend(proxy.id for proxy in section.proxies if HealthCheckJob.wants_check(proxy, connector))
        return cls(health=frozenset(health), sync=frozenset(sync), scale=frozenset(scale))

    def of_kind(self, kind: JobKind) -> Collection[str]:
        """The keys ``kind`` is published for: the compile's for health, sync and scale, the install's one for any other."""
        if kind == JobKind.HEALTH:
            return self.health
        if kind == JobKind.SYNC:
            return self.sync
        if kind == JobKind.SCALE:
            return self.scale
        return INSTALL_WIDE_KEYS


class JobProducer(PeriodicLoop):
    """Publishes a periodic kind's keys each once a period, at the second each key's stable hash picks; the leader runs it.

    Args:
        period: seconds between two publishes of a key, rounded to whole seconds, at least 1.
    """

    # A cycle a second, the first at once, of the loop and of each new lead.
    first_at_once: ClassVar[bool] = True

    def __init__(
        self,
        jobs: JobQueue,
        kind: JobKind,
        builder: FeedBuilder,
        *,
        period: float,
        lease: Lease,
    ) -> None:
        super().__init__(name=job_producer_name(kind), interval=1.0, lease=lease)
        self.kind = kind
        self.period = max(1, round(period))
        self._jobs = jobs
        self._builder = builder
        # The last second (epoch) this lead published the slice of, empty or
        # not; None until its first, which starts at the current second.
        self._last: int | None = None
        # The compile's keys the slot map was built from, and the map: second
        # of the period -> the keys published at it.
        self._source: JobKeys | None = None
        self._slots: dict[int, list[str]] = {}

    def slot(self, key: str) -> int:
        """The second of the period ``key`` is published at."""
        return int(stable_jitter(f"{self.kind}:{key}") * self.period)

    async def _on_term(self, term: int) -> None:
        """A new lead starts at the current second: what the lead before left waits a period, with no burst."""
        await super()._on_term(term)
        self._last = None

    async def _cycle(self) -> bool:
        """Publish the keys of every second after the last one published, up to now, at most ``CATCH_UP_SECONDS`` of them.

        The last second moves only once the publish returned, so a publish Redis refused is sent by the next cycle.
        """
        now = int(time.time())
        keys = self._builder.job_keys
        # Each compile makes its own keys: the map is built once per compile, not each second.
        if keys is not self._source:
            self._slots = self._slot_map(keys.of_kind(self.kind))
            self._source = keys
        first = now if self._last is None else max(self._last + 1, now - CATCH_UP_SECONDS + 1)
        due = [key for second in range(first, now + 1) for key in self._slots.get(second % self.period, ())]
        if due:
            await self._jobs.publish(self.kind, due)
        self._last = now
        return bool(due)

    def _slot_map(self, keys: Collection[str]) -> dict[int, list[str]]:
        """Each second of the period with the keys published at it, in key order."""
        slots: dict[int, list[str]] = {}
        for key in sorted(keys):
            slots.setdefault(self.slot(key), []).append(key)
        return slots
