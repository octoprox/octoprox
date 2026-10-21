# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Devices indexed by tunnel address, whatever tunnel they arrive through.

A tunnel protocol only lets a device send from the address it was given, so a
connection's source address identifies the device; each protocol's endpoint
keeps an in-memory :class:`AddressDirectory` of the devices in the last feed it
applied, and the data plane asks them all through one :class:`PeerIndex`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from api.models.location import LocationTarget


class TunnelPeer(Protocol):
    """What the data plane needs of a device: who it is, whose it is, and how to route it."""

    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def project_id(self) -> str: ...

    @property
    def address(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def session_id(self) -> str | None: ...

    @property
    def location(self) -> LocationTarget | None: ...

    @property
    def created_at(self) -> datetime: ...


class PeerLookup(Protocol):
    """Anything that can turn a tunnel address into a peer."""

    def by_address(self, address: str) -> TunnelPeer | None: ...


class AddressDirectory[P: TunnelPeer]:
    """The peers of one tunnel protocol a gateway knows, indexed by id and by tunnel address.

    The in-memory copy the transparent listener authenticates against; the
    protocol's endpoint replaces it with the devices of each feed it applies.
    """

    def __init__(self) -> None:
        self._peers: dict[str, P] = {}
        self._by_address: dict[str, P] = {}

    # --- reads ----------------------------------------------------------------------

    def get(self, peer_id: str) -> P | None:
        return self._peers.get(peer_id)

    def by_address(self, address: str) -> P | None:
        return self._by_address.get(address)

    def all(self) -> list[P]:
        return sorted(self._peers.values(), key=lambda p: (p.created_at, p.id))

    def __len__(self) -> int:
        return len(self._peers)

    # --- writes to the cache --------------------------------------------------------

    def put(self, peer: P) -> None:
        existing = self._peers.get(peer.id)
        if existing is not None:
            self._by_address.pop(existing.address, None)
        self._peers[peer.id] = peer
        self._by_address[peer.address] = peer

    def replace_all(self, peers: Iterable[P]) -> None:
        self._peers = {}
        self._by_address = {}
        for peer in peers:
            self.put(peer)


class PeerIndex:
    """Every protocol's directory, asked in turn: the lookup the data plane routes with."""

    def __init__(self) -> None:
        self._directories: list[PeerLookup] = []

    def add(self, directory: PeerLookup) -> None:
        if directory not in self._directories:
            self._directories.append(directory)

    def by_address(self, address: str) -> TunnelPeer | None:
        for directory in self._directories:
            peer = directory.by_address(address)
            if peer is not None:
                return peer
        return None
