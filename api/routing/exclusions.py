# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What a gateway keeps out of routing before the feed says so: the slots preflight retired, and its own quarantines.

Two small maps, local to one gateway and persisted nowhere, each entry expiring on its own, so a feed update that never
comes keeps nothing out for long.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta

from api.core import utc_now

# How long a slot judged out by preflight stays out of this gateway's
# routing, in seconds. The sighting reaches the worker, the delete reaches
# Postgres and the compile reaches the gateway in a few seconds; 30 s covers
# that with room, and a row the worker decides to keep comes back on its own.
RETIRED_TTL_SECONDS = 30.0
# How long a quarantine this gateway tripped keeps the proxy out on the trip's
# word alone, in seconds. The feed's runtime section carries the trip to every
# gateway once the controller has rebuilt and published it, usually within a
# fraction of a second, after which the section decides; this covers a
# controller held up by a long compile, or a lease moving to another one.
LOCAL_QUARANTINE_HANDOVER_SECONDS = 10.0


class LocalExclusions:
    """Retired slots and local quarantines, each until its own expiry (naive UTC)."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        # Proxy id -> when the retirement ends.
        self._retired: dict[str, datetime] = {}
        # Proxy id -> when the quarantine this gateway tripped ends.
        self._quarantined: dict[str, datetime] = {}

    def retire(self, proxy_id: str, ttl: float = RETIRED_TTL_SECONDS) -> None:
        """Keep a slot preflight judged out away from routing for ``ttl`` seconds, until the feed drops it."""
        self._retired[proxy_id] = self._clock() + timedelta(seconds=ttl)

    def quarantine(self, proxy_id: str, until: datetime) -> None:
        """Keep a proxy this gateway's rate limiter tripped out until ``until``, or the handover's end if sooner; a later end wins.

        Past the handover the feed decides alone, whether it carries the quarantine or not.
        """
        until = min(until, self._clock() + timedelta(seconds=LOCAL_QUARANTINE_HANDOVER_SECONDS))
        current = self._quarantined.get(proxy_id)
        if current is None or until > current:
            self._quarantined[proxy_id] = until

    def is_retired(self, proxy_id: str) -> bool:
        """Whether the proxy is retired on this gateway now."""
        expires = self._retired.get(proxy_id)
        if expires is None:
            return False
        if expires > self._clock():
            return True
        del self._retired[proxy_id]
        return False

    def quarantined_until(self, proxy_id: str) -> datetime | None:
        """When the quarantine this gateway tripped on the proxy ends; None when there is none in force."""
        until = self._quarantined.get(proxy_id)
        if until is None:
            return None
        if until > self._clock():
            return until
        del self._quarantined[proxy_id]
        return None

    def reconcile(self, section_quarantines: Mapping[str, datetime]) -> None:
        """Hand local quarantines over to the feed's ``section_quarantines`` after each apply, and forget what expired.

        An ended entry the feed still carries hands nothing over, so a fresh trip stays in force here.
        """
        now = self._clock()
        self._quarantined = {
            proxy_id: until
            for proxy_id, until in self._quarantined.items()
            if until > now and not _active(section_quarantines.get(proxy_id), now)
        }
        self._retired = {proxy_id: expires for proxy_id, expires in self._retired.items() if expires > now}

    def retired_ids(self) -> set[str]:
        """The slots retired on this gateway now."""
        now = self._clock()
        return {proxy_id for proxy_id, expires in self._retired.items() if expires > now}

    def quarantined_ids(self) -> set[str]:
        """The proxies under a quarantine this gateway tripped now."""
        now = self._clock()
        return {proxy_id for proxy_id, until in self._quarantined.items() if until > now}

    @property
    def retired_count(self) -> int:
        """Slots retired on this gateway now, for its snapshot."""
        now = self._clock()
        return sum(1 for expires in self._retired.values() if expires > now)

    @property
    def quarantined_count(self) -> int:
        """Local quarantines in force on this gateway now, for its snapshot."""
        now = self._clock()
        return sum(1 for until in self._quarantined.values() if until > now)


def _active(until: datetime | None, now: datetime) -> bool:
    return until is not None and until > now
