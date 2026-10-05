# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Devices indexed by tunnel address, whatever tunnel they arrive through.

A tunnel protocol only lets a device send from the address it was given
(WireGuard's cryptokey routing does this; so does OpenVPN's per-client
source check), so the source address of a connection off a tunnel interface
identifies the device, and through it the project and the routing it was
given. Each protocol keeps its own directory, a subclass of
:class:`AddressDirectory` that knows how to load its rows; the data plane
asks them all through one :class:`PeerIndex`.
"""

from __future__ import annotations

import ipaddress
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


class NoFreeAddressError(RuntimeError):
    """Every host address of the subnet is taken."""


class AddressDirectory[P: TunnelPeer]:
    """The peers of one tunnel protocol this instance knows, indexed by id and by tunnel address.

    The in-memory copy the transparent listener authenticates against. A
    subclass loads it from its own table and keeps it current through the
    protocol's change feed.
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

    def for_project(self, project_id: str) -> list[P]:
        return [p for p in self.all() if p.project_id == project_id]

    def __len__(self) -> int:
        return len(self._peers)

    @property
    def enabled_count(self) -> int:
        return sum(1 for p in self._peers.values() if p.enabled)

    def used_addresses(self) -> set[str]:
        return set(self._by_address)

    def allocate_address(self, network: ipaddress.IPv4Network) -> str:
        """The lowest free host address, skipping the gateway (the first host)."""
        hosts = network.hosts()
        next(hosts, None)
        for candidate in hosts:
            if str(candidate) not in self._by_address:
                return str(candidate)
        raise NoFreeAddressError(f"no free address left in {network}")

    # --- writes to the cache --------------------------------------------------------

    def put(self, peer: P) -> None:
        existing = self._peers.get(peer.id)
        if existing is not None:
            self._by_address.pop(existing.address, None)
        self._peers[peer.id] = peer
        self._by_address[peer.address] = peer

    def remove(self, peer_id: str) -> P | None:
        peer = self._peers.pop(peer_id, None)
        if peer is not None:
            self._by_address.pop(peer.address, None)
        return peer

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
