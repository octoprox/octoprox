# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The peers one instance knows, indexed by tunnel address.

Postgres is authoritative; this is the in-memory copy the transparent
listener authenticates against, kept current by the same change feed the
proxy manager's caches use (``wireguard_peer_changed`` over Redis Pub/Sub,
plus the periodic full reload as the safety net).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

import structlog

from api.db.session import SessionFactory
from api.db.wireguard_repository import WireGuardPeerRepository
from api.models.wireguard import WireGuardPeer

logger = structlog.get_logger()


class NoFreeAddressError(RuntimeError):
    """Every host address of the subnet is taken."""


class PeerDirectory:
    def __init__(self, session_factory: SessionFactory | None) -> None:
        self._session_factory = session_factory
        self._peers: dict[str, WireGuardPeer] = {}
        self._by_address: dict[str, WireGuardPeer] = {}

    # --- reads ----------------------------------------------------------------------

    def get(self, peer_id: str) -> WireGuardPeer | None:
        return self._peers.get(peer_id)

    def by_address(self, address: str) -> WireGuardPeer | None:
        return self._by_address.get(address)

    def all(self) -> list[WireGuardPeer]:
        return sorted(self._peers.values(), key=lambda p: (p.created_at, p.id))

    def for_project(self, project_id: str) -> list[WireGuardPeer]:
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

    def put(self, peer: WireGuardPeer) -> None:
        existing = self._peers.get(peer.id)
        if existing is not None:
            self._by_address.pop(existing.address, None)
        self._peers[peer.id] = peer
        self._by_address[peer.address] = peer

    def remove(self, peer_id: str) -> WireGuardPeer | None:
        peer = self._peers.pop(peer_id, None)
        if peer is not None:
            self._by_address.pop(peer.address, None)
        return peer

    def replace_all(self, peers: Iterable[WireGuardPeer]) -> None:
        self._peers = {}
        self._by_address = {}
        for peer in peers:
            self.put(peer)

    # --- Postgres -------------------------------------------------------------------

    async def load(self) -> None:
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            peers = await WireGuardPeerRepository(session).get_all()
        self.replace_all(peers)
        logger.debug("Loaded WireGuard peers", count=len(peers))

    async def reload_one(self, peer_id: str, op: str | None) -> None:
        """Cross-instance handler for ``wireguard_peer_changed``."""
        if op == "removed" or self._session_factory is None:
            self.remove(peer_id)
            return
        async with self._session_factory() as session:
            peer = await WireGuardPeerRepository(session).get_by_id(peer_id)
        if peer is None:
            self.remove(peer_id)
        else:
            self.put(peer)
