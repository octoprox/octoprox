# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The OpenVPN peers one instance knows, indexed by tunnel address and by id (their common name).

Postgres is authoritative; this is the in-memory copy the transparent
listener authenticates against and the management interface admits devices
from, kept current by ``openvpn_peer_changed`` over Redis Pub/Sub plus the
periodic full reload. The index is the one every tunnel protocol uses
(:class:`api.tunnel.peers.AddressDirectory`); this adds the loading.
"""

from __future__ import annotations

import structlog

from api.db.openvpn_repository import OpenVpnPeerRepository
from api.db.session import SessionFactory
from api.models.openvpn import OpenVpnPeer
from api.tunnel.peers import AddressDirectory

logger = structlog.get_logger()


class PeerDirectory(AddressDirectory[OpenVpnPeer]):
    def __init__(self, session_factory: SessionFactory | None) -> None:
        super().__init__()
        self._session_factory = session_factory

    async def load(self) -> None:
        if self._session_factory is None:
            return
        async with self._session_factory() as session:
            peers = await OpenVpnPeerRepository(session).get_all()
        self.replace_all(peers)
        logger.debug("Loaded OpenVPN peers", count=len(peers))

    async def reload_one(self, peer_id: str, op: str | None) -> OpenVpnPeer | None:
        """Cross-instance handler for ``openvpn_peer_changed``; the peer as it now is, None when gone."""
        if op == "removed" or self._session_factory is None:
            self.remove(peer_id)
            return None
        async with self._session_factory() as session:
            peer = await OpenVpnPeerRepository(session).get_by_id(peer_id)
        if peer is None:
            self.remove(peer_id)
        else:
            self.put(peer)
        return peer
