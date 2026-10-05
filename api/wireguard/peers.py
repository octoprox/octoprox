# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The WireGuard peers one instance knows, indexed by tunnel address.

Postgres is authoritative; this is the in-memory copy the transparent
listener authenticates against, kept current by the same change feed the
proxy manager's caches use (``wireguard_peer_changed`` over Redis Pub/Sub,
plus the periodic full reload as the safety net). The index itself is the
one every tunnel protocol uses (:class:`api.tunnel.peers.AddressDirectory`);
this adds the loading.
"""

from __future__ import annotations

import structlog

from api.db.session import SessionFactory
from api.db.wireguard_repository import WireGuardPeerRepository
from api.models.wireguard import WireGuardPeer
from api.tunnel.peers import AddressDirectory

logger = structlog.get_logger()


class PeerDirectory(AddressDirectory[WireGuardPeer]):
    def __init__(self, session_factory: SessionFactory | None) -> None:
        super().__init__()
        self._session_factory = session_factory

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
