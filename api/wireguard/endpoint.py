# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The WireGuard tunnel one gateway terminates, following the routing feed.

Runs on gateways with ``wireguard.enabled`` (and in a process of role
``all`` with it set). The endpoint owns the interface
(:class:`~api.wireguard.system.WireGuardInterface`) and attaches it to the
tunnel data plane the process shares with every tunnel protocol, where the
resolver, the nftables redirect and the transparent listener live. It never
touches Postgres. The feed's global section carries the server settings and
every device (keys and address; never a device's private key or its last
sighting), and the endpoint follows it:

- the devices are swapped into the in-memory directory the data plane finds
  a connection's device in, at once;
- the interface follows in the background, so the feed's apply never waits
  on ``ip`` or ``wg``: it comes up with the first feed, moves in place when
  the subnet does, and takes ``wg syncconf`` (which leaves unchanged peers'
  sessions alone) whenever what ``wg`` is told changes: a device added,
  removed, enabled, disabled or rotated, the server key, the port;
- every :data:`RECONCILE_INTERVAL_SECONDS` the gateway's
  :class:`TunnelReconciler` has it compare the peers the interface carries
  with the devices and fix what differs, which also retries whatever failed
  to apply and brings back an interface that vanished.

Every few seconds it publishes what the interface reports per device to
Redis (``wireguard:peer_status:<instance>``); the api shows it and the
controller records last sightings from it. Revoking a device needs the
feed: with Redis or the controller gone, a gateway keeps carrying the
devices it last applied.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from api.core.job_stats import job_stats
from api.core.tasks import TaskSet, spawn_held
from api.core.workers import WorkerName
from api.db.redis import TUNNEL_STATUS_INTERVAL
from api.models.tunnel import TunnelEndpointPhase, TunnelEndpointState, TunnelProtocol
from api.models.wireguard import WireGuardPeerReading
from api.tunnel.peers import AddressDirectory
from api.tunnel.system import CommandError, CommandRunner, explain
from api.wireguard.config import render_server_conf
from api.wireguard.system import WireGuardInterface

if TYPE_CHECKING:
    from api.core.config import Settings
    from api.db.redis import RedisClient
    from api.feed.models import WireGuardFeed, WireGuardPeerFeed, WireGuardServerFeed
    from api.tunnel.dataplane import TunnelDataPlane

logger = structlog.get_logger()

# The protocol's name in the Redis status keys and in endpoint states.
PROTOCOL: TunnelProtocol = "wireguard"
# How often every tunnel endpoint of a gateway compares what it carries with
# the feed, in seconds. A device revoked by a change that failed to apply
# keeps its session at most this long after the feed reached the gateway;
# a sweep is one ``wg show`` or ``status`` per endpoint, so it costs nothing.
RECONCILE_INTERVAL_SECONDS = 30.0


class WireGuardEndpoint:
    """The interface one gateway runs for WireGuard, kept in line with the feed.

    Args:
        settings: This gateway's tunnel configuration: interface name, port
            override, MTU, the resolver's range, encrypted-DNS blocking.
        plane: The process's tunnel data plane, started before the first
            feed is applied; the interface attaches to it.
        redis_client: Where the device readings are published.
        runner: Runs ``ip`` and ``wg``; tests pass a recording one.
        status_interval: Seconds between two publications of the readings.
    """

    def __init__(
        self,
        settings: Settings,
        plane: TunnelDataPlane,
        redis_client: RedisClient,
        *,
        runner: CommandRunner | None = None,
        status_interval: float = TUNNEL_STATUS_INTERVAL,
    ) -> None:
        self._instance_id = settings.instance_id
        self._listen_port = settings.wireguard_listen_port
        self._mtu = settings.wireguard_mtu
        self._fake_ip_range = settings.tunnel_fake_ip_range
        self._block_encrypted_dns = settings.tunnel_block_encrypted_dns
        self._plane = plane
        self._redis = redis_client
        self._status_interval = status_interval
        self.interface = WireGuardInterface(settings.wireguard_interface, runner)
        # The devices of the feed applied last. The data plane tells a
        # connection's device by its source address through this directory
        # (registered with its index), so admission follows the feed at once.
        self.directory: AddressDirectory[WireGuardPeerFeed] = AddressDirectory()
        plane.peers.add(self.directory)
        self.tasks = TaskSet()
        self._feed: WireGuardFeed | None = None
        # What the interface runs with while it is up: the subnet it was
        # addressed on and the configuration wg last took. None while down.
        self._subnet: str | None = None
        self._conf: str | None = None
        self._phase: TunnelEndpointPhase = "starting"
        self._error: str | None = None
        self._stopping = False
        # One change to the interface at a time: applies and the reconcile
        # sweep queue on it, and each converges on the feed applied last.
        self._lock = asyncio.Lock()
        self._held: set[asyncio.Task[Any]] = set()

    @property
    def running(self) -> bool:
        """Whether the interface is up and attached."""
        return self._phase == "running"

    async def start(self) -> None:
        """Start publishing the readings; the interface itself comes up with the first feed."""
        self.tasks.spawn(WorkerName.WIREGUARD_STATUS_PUBLISHER, self._publish_loop())

    async def stop(self) -> None:
        """Stop publishing, drop any change under way and take the interface down."""
        self._stopping = True
        await self.tasks.stop()
        for task in list(self._held):
            task.cancel()
        await asyncio.gather(*self._held, return_exceptions=True)
        async with self._lock:
            await self._tear_down()
        self._phase = "stopped"

    # --- following the feed --------------------------------------------------------------

    def apply(self, feed: WireGuardFeed | None) -> None:
        """Follow the feed's WireGuard settings and devices; None while the install has no settings row.

        The directory changes at once. The interface follows in the
        background (:meth:`settle` waits for it): a failure is reported in
        :meth:`state` and retried by :meth:`reconcile`, never raised into the
        feed's apply.
        """
        self._feed = feed
        self.directory.replace_all(feed.peers if feed is not None else [])
        spawn_held(self._held, self._follow(), name="wireguard_apply")

    async def settle(self) -> None:
        """Wait until the feeds applied so far have reached the interface."""
        while self._held:
            await asyncio.gather(*list(self._held), return_exceptions=True)

    async def reconcile(self) -> int:
        """Compare the peers the interface carries with the devices and fix what differs; how many fixes it made.

        First retries whatever the last apply could not do (a bring-up, a
        subnet move, a sync), which counts as one fix when it worked. Then a
        peer on the interface that is not an enabled device of the feed is a
        stray, and an enabled device the interface lacks is missing: one
        ``wg syncconf`` fixes both, each counted. An interface that vanished
        (wireguard-go exiting takes its interface with it) is brought up
        again.
        """
        async with self._lock:
            applied = (self._subnet, self._conf)
            await self._converge()
            retried = int((self._subnet, self._conf) != applied)
            feed = self._feed
            if feed is None or self._subnet is None:
                return retried
            try:
                carried = {dump.public_key for dump in await self.interface.dump()}
            except Exception as exc:
                if await self.interface.exists():
                    logger.warning("WireGuard interface unreadable", interface=self.interface.name, error=str(exc))
                    return retried
                logger.warning("WireGuard interface vanished, bringing it up again", interface=self.interface.name)
                await self._tear_down()
                await self._converge()
                return retried + 1
            wanted = {peer.public_key for peer in feed.peers if peer.enabled}
            strays, missing = carried - wanted, wanted - carried
            if not strays and not missing:
                return retried
            logger.warning("WireGuard peers out of step with the feed", strays=len(strays), missing=len(missing))
            self._conf = None
            await self._converge()
            return retried + len(strays) + len(missing)

    async def _follow(self) -> None:
        """One apply's background half: converge once the interface is free."""
        async with self._lock:
            await self._converge()

    async def _converge(self) -> None:
        """Bring the interface in line with the feed applied last: up, moved, synced, or down. Lock held."""
        if self._stopping:
            return
        feed = self._feed
        if feed is None:
            if self._subnet is not None:
                await self._tear_down()
                logger.info("WireGuard endpoint down: the feed carries no WireGuard settings")
            self._phase, self._error = "starting", None
            return
        server = feed.server
        port = self._port(server)
        conf = render_server_conf(server.private_key, port, feed.peers)
        if self._subnet is None:
            await self._bring_up(feed, port, conf)
            return
        if self._subnet != server.subnet and not await self._readdress(server):
            return
        if self._conf != conf:
            await self._sync(feed, port, conf)

    async def _bring_up(self, feed: WireGuardFeed, port: int, conf: str) -> None:
        """Create and configure the interface and attach it; on a failure nothing of it is left up."""
        server = feed.server
        self._phase = "starting"
        try:
            await self.interface.up(
                private_key=server.private_key,
                listen_port=port,
                address=f"{server.gateway}/{server.network.prefixlen}",
                mtu=self._mtu,
                peers=feed.peers,
            )
            await self._plane.attach(self.interface.name, server.gateway)
        except Exception as exc:
            await self._tear_down()
            self._fail(exc, "WireGuard endpoint failed to start")
            return
        self._subnet, self._conf = server.subnet, conf
        self._phase, self._error = "running", None
        logger.info(
            "WireGuard endpoint running",
            interface=self.interface.name,
            backend=self.interface.backend,
            gateway=server.gateway,
            listen_port=port,
            peers=len(feed.peers),
        )

    async def _readdress(self, server: WireGuardServerFeed) -> bool:
        """Move the interface and its listeners to the new subnet's gateway; whether it worked.

        A half-moved interface is worse than none: on a failure it is taken
        down, and the next reconcile brings it up afresh.
        """
        try:
            await self.interface.readdress(f"{server.gateway}/{server.network.prefixlen}")
            await self._plane.attach(self.interface.name, server.gateway)
        except Exception as exc:
            await self._tear_down()
            self._fail(exc, "WireGuard subnet change failed")
            return False
        self._subnet = server.subnet
        logger.info("WireGuard subnet changed live", gateway=server.gateway, subnet=server.subnet)
        return True

    async def _sync(self, feed: WireGuardFeed, port: int, conf: str) -> None:
        """``wg syncconf`` the feed's key, port and devices; on a failure the previous set stays, retried later."""
        try:
            await self.interface.sync(private_key=feed.server.private_key, listen_port=port, peers=feed.peers)
        except Exception as exc:
            self._report(exc, "WireGuard peer sync failed")
            return
        self._conf, self._error = conf, None
        logger.debug("WireGuard peers synced", peers=len(feed.peers))

    async def _tear_down(self) -> None:
        """Detach the interface and remove it, best effort: what remains is gone with the next bring-up."""
        with contextlib.suppress(Exception):
            await self._plane.detach(self.interface.name)
        with contextlib.suppress(Exception):
            await self.interface.down()
        self._subnet = self._conf = None

    def _fail(self, exc: Exception, event: str) -> None:
        """The tunnel is down for ``exc``: reported, and retried by the next reconcile."""
        self._report(exc, event)
        self._phase = "failed"

    def _report(self, exc: Exception, event: str) -> None:
        """Record what went wrong for the endpoint's state.

        Retried every reconcile, so a failure is logged as an error when it
        first happens or its reason changes, and at debug while it repeats.
        """
        error = explain(exc) if isinstance(exc, CommandError) else str(exc)
        if error != self._error:
            logger.error(event, error=error)
        else:
            logger.debug(event, error=error)
        self._error = error

    def _port(self, server: WireGuardServerFeed) -> int:
        """The UDP port this gateway listens on: its own override, else the install's endpoint port."""
        return self._listen_port or server.endpoint_port

    # --- status ----------------------------------------------------------------------------

    def state(self) -> TunnelEndpointState:
        """What this endpoint is doing, for the gateway's heartbeat snapshot."""
        transparent = self._plane.transparent_server
        return TunnelEndpointState(
            protocol=PROTOCOL,
            instance_id=self._instance_id,
            state=self._phase,
            error=self._error,
            backend=self.interface.backend,
            listen_port=self._port(self._feed.server) if self._feed is not None else self._listen_port,
            active_connections=transparent.active_connections if transparent is not None else 0,
            interface=self.interface.name,
            fake_ip_range=self._fake_ip_range,
            block_encrypted_dns=self._block_encrypted_dns,
        )

    async def publish_status(self) -> bool:
        """Publish what the interface reports per device; False when it is not up or carries no peer.

        Every peer of the interface is published under its public key, the
        only name wg has for it: readers keep the ones they know. The hash
        expires soon after this gateway stops writing it.
        """
        if self._subnet is None:
            return False
        statuses = {
            dump.public_key: WireGuardPeerReading(
                latest_handshake=dump.latest_handshake,
                rx_bytes=dump.rx_bytes,
                tx_bytes=dump.tx_bytes,
                endpoint=dump.endpoint,
            ).model_dump_json()
            for dump in await self.interface.dump()
        }
        await self._redis.set_tunnel_peer_status(PROTOCOL, self._instance_id, statuses)
        return bool(statuses)

    async def _publish_loop(self) -> None:
        """Publisher loop: every gateway with WireGuard enabled runs one."""
        job_stats.declare_interval(WorkerName.WIREGUARD_STATUS_PUBLISHER, self._status_interval)
        while True:
            try:
                await asyncio.sleep(self._status_interval)
                with job_stats.track(WorkerName.WIREGUARD_STATUS_PUBLISHER) as run:
                    if not await self.publish_status():
                        run.idle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("WireGuard status publish failed", error=str(exc))


class ReconcilingEndpoint(Protocol):
    """What the reconcile loop needs of a tunnel endpoint."""

    async def reconcile(self) -> int: ...


class TunnelReconciler:
    """The gateway's one reconcile loop over every tunnel endpoint it runs.

    Each sweep has every endpoint compare what it carries with the feed and
    fix what differs (:meth:`WireGuardEndpoint.reconcile`,
    :meth:`api.openvpn.endpoint.OpenVpnEndpoint.reconcile`). It lives with
    the first tunnel protocol because it serves them all: a gateway runs one,
    as ``tunnel_reconciler``, whichever protocols it terminates.
    """

    def __init__(
        self, endpoints: Sequence[ReconcilingEndpoint], *, interval: float = RECONCILE_INTERVAL_SECONDS
    ) -> None:
        self._endpoints = list(endpoints)
        self._interval = interval

    async def run(self) -> None:
        """Reconcile loop: a gateway with any tunnel enabled runs one."""
        job_stats.declare_interval(WorkerName.TUNNEL_RECONCILER, self._interval)
        while True:
            try:
                await asyncio.sleep(self._interval)
                with job_stats.track(WorkerName.TUNNEL_RECONCILER) as run:
                    fixed, failed = await self.reconcile()
                    if failed:
                        run.fail(f"{failed} tunnel endpoint(s) failed to reconcile")
                    elif not fixed:
                        run.idle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Tunnel reconcile failed", error=str(exc))

    async def reconcile(self) -> tuple[int, int]:
        """One sweep: how many things the endpoints fixed, and how many endpoints failed.

        One endpoint failing does not keep the others from their sweep.
        """
        fixed = failed = 0
        for endpoint in self._endpoints:
            try:
                fixed += await endpoint.reconcile()
            except Exception as exc:
                failed += 1
                logger.error("Tunnel endpoint reconcile failed", endpoint=type(endpoint).__name__, error=str(exc))
        return fixed, failed
