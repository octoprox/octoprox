# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The listener tunnel traffic is redirected to.

A connection arrives here because nftables rewrote its destination. The
device thinks it is talking to the origin, so there is no request line and
no ``Proxy-Authorization``: the peer's tunnel address is the credential (see
:mod:`api.tunnel.peers`) and the original destination is read back from the
socket. From there it is the CONNECT path: pick an upstream for the project,
verify the exit, relay bytes.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

from api.core.event_bus import event_bus
from api.core.proxy_server import ProxyServer
from api.core.signals import tunnel_encrypted_dns_blocked, tunnel_name_unresolved
from api.models.project import MitmMode
from api.tunnel.dns import FakeIpDirectory
from api.tunnel.peers import PeerLookup
from api.tunnel.sniff import SniffResult, sniff, take_buffered

if TYPE_CHECKING:
    from api.core.mitm import MitmHandler
    from api.core.proxy_manager import ProxyManager
    from api.geo.verifier import ExitVerifier

logger = structlog.get_logger()

# netfilter's getsockopt for the pre-NAT destination of a redirected connection.
SO_ORIGINAL_DST = 80

# DNS over TLS always uses this port.
DOT_PORT = 853
# Public DNS-over-HTTPS resolvers browsers and operating systems switch to on
# their own. A device that reaches one resolves names outside the tunnel
# resolver, after which its connections carry only addresses and the name is
# known only when the stream repeats it. Closing these connections makes a
# client in automatic mode fall back to the tunnel resolver.
ENCRYPTED_DNS_HOSTS: frozenset[str] = frozenset({
    "dns.google", "dns.google.com", "dns64.dns.google",
    "cloudflare-dns.com", "mozilla.cloudflare-dns.com", "one.one.one.one",
    "security.cloudflare-dns.com", "family.cloudflare-dns.com",
    "dns.quad9.net", "dns9.quad9.net", "dns10.quad9.net", "dns11.quad9.net",
    "dns.nextdns.io", "dns.adguard-dns.com", "dns.adguard.com", "dns-family.adguard.com",
    "doh.opendns.com", "doh.familyshield.opendns.com", "doh.cleanbrowsing.org",
    "dns.alidns.com", "doh.pub", "dns.sb", "doh.dns.sb", "doh.mullvad.net", "dns.mullvad.net",
})

OriginalDestination = Callable[[asyncio.StreamWriter], "tuple[str, int] | None"]


def original_destination(writer: asyncio.StreamWriter) -> tuple[str, int] | None:
    """The address the device connected to before nftables redirected it here (IPv4)."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    try:
        raw = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
    except (OSError, AttributeError):
        return None
    if len(raw) < 8:
        return None
    # struct sockaddr_in: sa_family (host order), sin_port (network order), sin_addr.
    if int.from_bytes(raw[:2], sys.byteorder) != socket.AF_INET:
        return None
    return socket.inet_ntoa(raw[4:8]), int.from_bytes(raw[2:4], "big")


class TransparentProxyServer(ProxyServer):
    """The proxy server's relay without its HTTP front: authenticated by address, addressed by NAT."""

    def __init__(
        self,
        proxy_manager: ProxyManager,
        peers: PeerLookup,
        fake_ips: FakeIpDirectory,
        *,
        port: int,
        mitm_handler: MitmHandler | None = None,
        exit_verifier: ExitVerifier | None = None,
        destination_of: OriginalDestination = original_destination,
        sniff_timeout: float = 2.0,
        block_encrypted_dns: bool = True,
    ) -> None:
        super().__init__(proxy_manager, mitm_handler=mitm_handler, exit_verifier=exit_verifier)
        self._port = port
        # One listening socket per tunnel gateway: nftables redirects a
        # connection to the primary address of the interface it came in on.
        self._servers: dict[str, asyncio.Server] = {}
        self._peers = peers
        self._fake_ips = fake_ips
        self._destination_of = destination_of
        self._sniff_timeout = sniff_timeout
        self._block_encrypted_dns = block_encrypted_dns
        self.unknown_peers = 0

    @property
    def is_listening(self) -> bool:
        return any(server.is_serving() for server in self._servers.values())

    @property
    def hosts(self) -> list[str]:
        return list(self._servers)

    @property
    def port(self) -> int:
        for server in self._servers.values():
            if server.sockets:
                bound: int = server.sockets[0].getsockname()[1]
                return bound
        return self._port

    async def start(self) -> None:
        # The proxy server binds its configured host and port; this listener
        # must only ever be reachable from inside a tunnel.
        raise NotImplementedError("the transparent listener binds per tunnel gateway: use listen()")

    async def listen(self, host: str) -> None:
        """Accept redirected connections arriving at ``host``, a tunnel gateway address."""
        if host in self._servers:
            return
        self._servers[host] = await asyncio.start_server(self._handle_client_wrapper, host, self.port)
        logger.info("Transparent listener bound", host=host, port=self.port)

    async def unlisten(self, host: str) -> None:
        """Stop accepting at ``host``. Connections already relayed run on until they end."""
        server = self._servers.pop(host, None)
        if server is not None:
            server.close()

    async def stop(self) -> None:
        servers = list(self._servers.values())
        self._servers.clear()
        for server in servers:
            server.close()
        await self._cancel_client_tasks()
        for server in servers:
            await server.wait_closed()

    async def _send_error(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        message: str,
        body: str = "",
        extra_headers: list[str] | None = None,
    ) -> None:
        # The device is not speaking HTTP to us; an error page would corrupt
        # whatever it is speaking. The only honest answer is to close, which
        # the caller does. The rejection events still fire.
        logger.debug("Transparent connection refused", status=status, reason=body or message)

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        peername = client_writer.get_extra_info("peername")
        source = str(peername[0]) if peername else None
        try:
            peer = self._peers.by_address(source) if source else None
            if peer is None:
                self.unknown_peers += 1
                logger.debug("Connection from an address that is not a peer", source=source)
                return
            if not peer.enabled:
                logger.debug("Connection from a disabled peer", peer=peer.name)
                return
            project = self._proxy_manager.get_project(peer.project_id)
            if project is None:
                logger.warning("Peer belongs to a project this instance does not know", peer_id=peer.id)
                return

            destination = self._destination_of(client_writer)
            if destination is None:
                logger.debug("Could not read the original destination", peer=peer.name)
                return
            dst_ip, dst_port = destination
            sockname = client_writer.get_extra_info("sockname")
            if sockname and (dst_ip, dst_port) == (str(sockname[0]), int(sockname[1])):
                # Not redirected: something connected to the listener itself.
                return

            hostname = await self._fake_ips.name_for(dst_ip)
            mitm_possible = project.tls_mitm_mode != MitmMode.OFF and self._mitm_handler is not None
            if hostname is not None and not mitm_possible:
                # The name is known and nothing downstream needs to know whether
                # the stream is TLS: waiting for the first bytes would only hold
                # a server-speaks-first protocol for the whole sniff timeout.
                sniffed = SniffResult(tls=False, server_name=None)
            else:
                sniffed = await sniff(client_reader, self._sniff_timeout)
            target_host = hostname or sniffed.server_name
            if self._block_encrypted_dns and (dst_port == DOT_PORT or target_host in ENCRYPTED_DNS_HOSTS):
                # Let the device fall back to the tunnel resolver rather than
                # resolve elsewhere and connect to addresses we cannot name.
                # Counted on the device, in the same pipeline as its requests.
                await event_bus.publish(tunnel_encrypted_dns_blocked, self, peer_id=peer.id)
                logger.info(
                    "Blocked encrypted DNS from a tunnel device", peer=peer.name, target=target_host or dst_ip, port=dst_port
                )
                return
            if target_host is None:
                if self._fake_ips.contains(dst_ip):
                    # A fake address whose name has been evicted, and the
                    # stream did not repeat the name: nowhere real to send it.
                    logger.info("Fake IP without a name, dropping", peer=peer.name, address=dst_ip)
                    return
                # Relayed by address: domain filters see an address and the
                # exit may differ from the one that resolved the name.
                target_host = dst_ip
                await event_bus.publish(tunnel_name_unresolved, self, peer_id=peer.id)
            session_id = peer.session_id
            location = peer.location
            logger.debug(
                "Tunnel connection",
                peer=peer.name,
                project_id=project.id,
                target=f"{target_host}:{dst_port}",
                tls=sniffed.tls,
                session_id=session_id,
                location=location.key if location else None,
            )

            proxy = await self._get_upstream_proxy(
                project_id=project.id, session_id=session_id, target_host=target_host, location=location
            )
            if proxy is None:
                await self._reject_no_proxy(client_writer, project.id, target_host, session_id, location)
                return
            verified = await self._verify_exit(project, proxy, session_id, location, target_host, client_writer)
            if verified is None:
                return

            use_mitm = sniffed.tls and mitm_possible
            await self._relay(
                client_reader, client_writer, verified, project, target_host, dst_port,
                established=None,
                use_mitm=use_mitm,
                # The peeked ClientHello is in the reader, not the socket; the
                # TLS upgrade has to be fed it or it waits forever.
                client_head=take_buffered(client_reader) if use_mitm else b"",
                peer_id=peer.id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Error handling tunnel connection", error=str(exc), source=source)
        finally:
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except Exception:
                pass
