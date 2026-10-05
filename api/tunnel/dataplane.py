# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What every tunnel this instance terminates shares: the resolver, the listener and the nftables table.

A tunnel protocol brings up its interface and *attaches* it here: the
fake-IP resolver and the transparent listener bind on the interface's
gateway address, and the nftables table redirects the interface's traffic
to them. A connection is matched to a device by the address it came from,
through the index every protocol registers its peers with. One data plane
per process, started once the proxy manager and its collaborators exist;
the protocols attach after that and detach before it stops.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import structlog

from api.core.config import Settings
from api.tunnel.dns import DnsServer, FakeIpDirectory, FakeIpPool, FakeIpResolver
from api.tunnel.peers import PeerIndex
from api.tunnel.system import CommandRunner, Netfilter, render_nft_ruleset
from api.tunnel.transparent import TransparentProxyServer

if TYPE_CHECKING:
    from api.core.mitm import MitmHandler
    from api.core.proxy_manager import ProxyManager
    from api.db.redis import RedisClient
    from api.geo.verifier import ExitVerifier

logger = structlog.get_logger()


class TunnelDataPlane:
    """The tunnel-side listeners of one process and the interfaces they serve."""

    def __init__(
        self,
        settings: Settings,
        redis_client: RedisClient | None,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self._config = settings
        self.pool = FakeIpPool(settings.tunnel_fake_ip_range)
        # Shared through Redis so every instance carrying a tunnel agrees on the mapping.
        self.fake_ips = FakeIpDirectory(self.pool, redis_client)
        self.peers = PeerIndex()
        self.netfilter = Netfilter(runner=runner)
        self.dns_server: DnsServer | None = None
        self.transparent_server: TransparentProxyServer | None = None
        # interface name -> gateway address, for every attached tunnel.
        self._gateways: dict[str, str] = {}
        self._lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        return self.transparent_server is not None

    @property
    def interfaces(self) -> dict[str, str]:
        """The attached interfaces and the gateway each is served on."""
        return dict(self._gateways)

    async def start(
        self,
        proxy_manager: ProxyManager,
        *,
        mitm_handler: MitmHandler | None = None,
        exit_verifier: ExitVerifier | None = None,
    ) -> None:
        """Build the listeners. Nothing is bound until a tunnel attaches."""
        self.dns_server = DnsServer(FakeIpResolver(self.fake_ips), self._config.tunnel_dns_port)
        self.transparent_server = TransparentProxyServer(
            proxy_manager,
            self.peers,
            self.fake_ips,
            port=self._config.tunnel_transparent_port,
            mitm_handler=mitm_handler,
            exit_verifier=exit_verifier,
            sniff_timeout=self._config.tunnel_sniff_timeout_seconds,
            block_encrypted_dns=self._config.tunnel_block_encrypted_dns,
        )

    async def attach(self, interface: str, gateway: str) -> None:
        """Serve a tunnel interface: listen on its gateway and redirect its traffic there.

        Raises what the bind or ``nft`` raised, with nothing of this attach
        left behind; the caller reports it and takes its interface down.
        """
        dns_server, transparent_server = self._servers()
        async with self._lock:
            previous = self._gateways.get(interface)
            if previous == gateway:
                return
            try:
                if previous is not None:
                    # Re-addressed: the old gateway's listeners go unless another interface shares them.
                    await self._release(interface, previous)
                await dns_server.listen(gateway)
                await transparent_server.listen(gateway)
                self._gateways[interface] = gateway
                await self._apply_rules()
            except Exception:
                await self._release(interface, gateway)
                if previous is not None:
                    # The interface is gone either way; the table must say so.
                    with contextlib.suppress(Exception):
                        await self._sync_rules()
                raise
        logger.info(
            "Tunnel interface attached",
            interface=interface,
            gateway=gateway,
            transparent_port=transparent_server.port,
            dns_port=dns_server.port,
        )

    async def detach(self, interface: str) -> None:
        """Stop serving an interface; the table follows the interfaces that remain."""
        async with self._lock:
            gateway = self._gateways.get(interface)
            if gateway is None:
                return
            await self._release(interface, gateway)
            await self._sync_rules()
        logger.info("Tunnel interface detached", interface=interface)

    async def stop(self) -> None:
        if self._gateways:
            # A protocol that did not detach: take its table down with the listeners.
            self._gateways.clear()
            with contextlib.suppress(Exception):
                await self.netfilter.remove()
        if self.transparent_server is not None:
            with contextlib.suppress(Exception):
                await self.transparent_server.stop()
            self.transparent_server = None
        if self.dns_server is not None:
            with contextlib.suppress(Exception):
                await self.dns_server.stop()
            self.dns_server = None

    # --- internals -------------------------------------------------------------------

    def _servers(self) -> tuple[DnsServer, TransparentProxyServer]:
        if self.dns_server is None or self.transparent_server is None:
            raise RuntimeError("the tunnel data plane is not started")
        return self.dns_server, self.transparent_server

    async def _apply_rules(self) -> None:
        dns_server, transparent_server = self._servers()
        await self.netfilter.apply(
            render_nft_ruleset(self.netfilter.table, self._gateways, transparent_server.port, dns_server.port)
        )

    async def _sync_rules(self) -> None:
        """The table follows the attached interfaces: rewritten for those left, gone with the last."""
        if self._gateways:
            await self._apply_rules()
        else:
            await self.netfilter.remove()

    async def _release(self, interface: str, gateway: str) -> None:
        """Forget an interface and, when no other interface shares its gateway, its listeners."""
        self._gateways.pop(interface, None)
        if gateway in self._gateways.values():
            return
        dns_server, transparent_server = self._servers()
        with contextlib.suppress(Exception):
            await transparent_server.unlisten(gateway)
        with contextlib.suppress(Exception):
            await dns_server.unlisten(gateway)
