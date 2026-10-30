# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A consistent-hash ring over one connector's proxies, which sticky places new sessions on.

A placement is a binary search plus a short walk past proxies that cannot serve the request, whatever the pool's size.
"""

import hashlib
import sys
from array import array
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from itertools import chain, repeat
from operator import and_, or_

from api.models.proxy import Proxy

# (pool size up to, points each proxy holds): more points spread sessions more evenly and cost more to build, so
# small pools get many and large ones few. Each proxy's points are a prefix of one hash stream, so a pool crossing a
# tier keeps its points' order; only then does a proxy joining or leaving move other proxies' sessions too.
POINT_TIERS = ((16, 1024), (64, 256))
POINTS_PER_PROXY = 64


def points_per_proxy(count: int) -> int:
    """How many points each proxy of a ring of ``count`` proxies holds."""
    return next((points for size, points in POINT_TIERS if count <= size), POINTS_PER_PROXY)


def ring_position(session_id: str) -> int:
    """Where a session sits on every ring: a 64-bit hash of its id, apart from the one that picks its connector."""
    digest = hashlib.blake2s(session_id.encode(), digest_size=8, person=b"ring").digest()
    return int.from_bytes(digest, "big")


class HashRing:
    """Points for each of a connector's rows, whatever their health, sorted into one array.

    A session goes to the first point clockwise from its position whose proxy can serve the request, so a proxy leaving
    or joining moves only the sessions it held or wins.
    """

    def __init__(self, members: Sequence[Proxy]) -> None:
        self._members = members
        self._ids = tuple(proxy.id for proxy in members)
        # The low bits of a point name its proxy, so the sorted points are the whole ring.
        self._mask = (1 << max(1, (len(self._ids) - 1).bit_length())) - 1
        points = points_per_proxy(len(self._ids))
        raw = array("Q")
        for proxy_id in self._ids:
            raw.frombytes(hashlib.shake_128(proxy_id.encode()).digest(8 * points))
        if sys.byteorder == "big":
            # Every gateway must read the same points from the same bytes.
            raw.byteswap()
        owners = chain.from_iterable(repeat(index, points) for index in range(len(self._ids)))
        self._points = array("Q", sorted(map(or_, map(and_, raw, repeat(~self._mask)), owners)))

    def matches(self, members: Sequence[Proxy]) -> bool:
        """Whether the ring was built from these rows, by identity or by their ids in order; a match is remembered by identity."""
        if members is self._members:
            return True
        if len(members) != len(self._ids) or any(proxy.id != proxy_id for proxy, proxy_id in zip(members, self._ids, strict=True)):
            return False
        self._members = members
        return True

    def place(self, position: int, eligible: Mapping[str, Proxy]) -> Proxy | None:
        """The first proxy clockwise from ``position`` that is in ``eligible``; None when no proxy of the ring is."""
        points, ids, mask = self._points, self._ids, self._mask
        count = len(points)
        start = bisect_left(points, position)
        for step in range(count):
            proxy = eligible.get(ids[points[(start + step) % count] & mask])
            if proxy is not None:
                return proxy
        return None
