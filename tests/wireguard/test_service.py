# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The api's WireGuard service against Postgres and Redis: settings, devices, addresses, status.

What a write announces is read off the changes stream, through a :class:`~api.db.streams.ChangeLog` on the test Redis
unless a test gives the service a change log of its own.
"""

import asyncio
import json
import time
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.config import Settings
from api.core.stats import TunnelPeerMetricDelta
from api.db.openvpn_repository import OpenVpnSettingsRepository
from api.db.redis import CHANGES_STREAM_KEY, TUNNEL_PEER_METRICS_KEY, RedisClient
from api.db.repository import ProjectRepository
from api.db.streams import ChangeKind, ChangeLog, ref_of
from api.db.wireguard_repository import WireGuardPeerRepository, WireGuardSettingsRepository
from api.models.project import Project
from api.models.tunnel import TunnelAddressError, TunnelEndpointState, TunnelValidationError
from api.models.wireguard import (
    WireGuardPeer,
    WireGuardPeerCreate,
    WireGuardPeerStatus,
    WireGuardPeerUpdate,
    WireGuardServerSettingsDoc,
)
from api.wireguard import keys
from api.wireguard.service import WireGuardService, defaults_from_config, seed_tunnel_settings

Factory = async_sessionmaker[AsyncSession]


class Recorded(ChangeLog):
    """A change log that keeps what it is told instead of appending it, for a test that reads entries per writer."""

    def __init__(self, redis: Any) -> None:
        super().__init__(redis)
        self.entries: list[tuple[ChangeKind, str]] = []

    async def record(self, kind: ChangeKind, *ids: str) -> None:
        self.entries.append((kind, ref_of(ids)))


class Totals:
    """The api's read model as the service sees it: device totals by id, and what it was asked."""

    def __init__(self) -> None:
        self.by_id: dict[str, TunnelPeerMetricDelta] = {}
        self.asked: list[list[str]] = []

    async def __call__(self, ids: Sequence[str]) -> dict[str, TunnelPeerMetricDelta]:
        self.asked.append(list(ids))
        return {peer_id: self.by_id[peer_id] for peer_id in ids if peer_id in self.by_id}


def _gateway(instance_id: str, protocol: str = "wireguard", **fields: Any) -> TunnelEndpointState:
    return TunnelEndpointState(protocol=protocol, instance_id=instance_id, state="running", **fields)  # type: ignore[arg-type]


def _service(
    factory: Factory,
    redis: Any,
    *,
    gateways: Sequence[TunnelEndpointState] = (),
    totals: Totals | None = None,
    defaults: dict[str, Any] | None = None,
    changes: ChangeLog | None = None,
) -> WireGuardService:
    async def endpoint_states() -> list[TunnelEndpointState]:
        return list(gateways)

    return WireGuardService(
        factory,
        redis,
        endpoint_states=endpoint_states,
        device_totals=totals or Totals(),
        defaults=defaults or {},
        changes=changes or ChangeLog(redis),
    )


async def _project(session: AsyncSession, name: str = "Living room") -> Project:
    project = Project(name=name, username=name.lower().replace(" ", "-"), password="pw")
    await ProjectRepository(session).create(project)
    await session.commit()
    return project


async def announced(redis: RedisClient) -> list[tuple[str, str]]:
    """Every entry of the changes stream as (kind, ref), oldest first."""
    return [(fields["kind"], fields["ref"]) for _, fields in await redis.client.xrange(CHANGES_STREAM_KEY)]


async def forget_announcements(redis: RedisClient) -> None:
    """Empty the changes stream, so a test sees only what its own writes announce."""
    await redis.client.delete(CHANGES_STREAM_KEY)


@pytest.fixture
async def project(db_session: AsyncSession) -> Project:
    return await _project(db_session)


def test_defaults_seed_from_config() -> None:
    seeded = defaults_from_config({"endpoint_host": "vpn.example.net", "subnet": "10.77.0.0/24"})
    assert seeded.endpoint_host == "vpn.example.net"
    assert seeded.gateway == "10.77.0.1"
    assert len(seeded.private_key) == 44 and seeded.public_key == keys.public_key_of(seeded.private_key)
    assert defaults_from_config({"subnet": "nonsense"}).subnet == "10.66.0.0/16"
    assert defaults_from_config({"private_key": "theirs"}).private_key != "theirs"


class TestSettings:
    async def test_seeding_creates_both_rows_once(
        self, db_session: AsyncSession, db_session_factory: Factory, redis_client: RedisClient, change_log: ChangeLog
    ) -> None:
        settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            wireguard_defaults={"endpoint_host": "vpn.example.net", "subnet": "10.77.0.0/24"},
            openvpn_defaults={"protocol": "tcp"},
        )
        await seed_tunnel_settings(db_session_factory, settings, change_log)
        wireguard = await WireGuardSettingsRepository(db_session).get()
        openvpn = await OpenVpnSettingsRepository(db_session).get()
        assert wireguard is not None and wireguard.endpoint_host == "vpn.example.net" and wireguard.subnet == "10.77.0.0/24"
        assert openvpn is not None and openvpn.protocol == "tcp"
        # The rows are feed tables: creating them is announced, once each.
        assert await announced(redis_client) == [("wireguard", ""), ("openvpn", "")]

        # Every later start finds them: nothing is regenerated, nothing is announced.
        await seed_tunnel_settings(db_session_factory, settings, change_log)
        db_session.expire_all()
        again = await WireGuardSettingsRepository(db_session).get()
        assert again is not None and again.private_key == wireguard.private_key
        assert (await OpenVpnSettingsRepository(db_session).get()).ca_cert == openvpn.ca_cert  # type: ignore[union-attr]
        assert await announced(redis_client) == [("wireguard", ""), ("openvpn", "")]

    async def test_processes_seeding_at_once_announce_the_rows_once(
        self, db_session: AsyncSession, db_session_factory: Factory, redis_client: RedisClient
    ) -> None:
        """Each finds no row and generates its own; the one whose insert won announces, the others go on quietly."""
        recorders = [Recorded(redis_client) for _ in range(3)]
        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        await asyncio.gather(*(seed_tunnel_settings(db_session_factory, settings, r) for r in recorders))

        entries = sorted(entry for recorder in recorders for entry in recorder.entries)
        assert entries == [(ChangeKind.OPENVPN, ""), (ChangeKind.WIREGUARD, "")]
        assert await WireGuardSettingsRepository(db_session).get() is not None

    async def test_a_read_creates_a_missing_row(
        self, db_session: AsyncSession, db_session_factory: Factory, redis_client: RedisClient
    ) -> None:
        service = _service(db_session_factory, redis_client, defaults={"endpoint_port": 51821})
        server = await service.settings()
        assert server.endpoint_port == 51821
        stored = await WireGuardSettingsRepository(db_session).get()
        assert stored is not None and stored.public_key == server.public_key
        # The read that created the row announced it; reads that find it announce nothing.
        await service.settings()
        assert await announced(redis_client) == [("wireguard", "")]

    async def test_save_keeps_every_device_inside_the_subnet(
        self, db_session: AsyncSession, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        service = _service(db_session_factory, redis_client)
        await seed_tunnel_settings(db_session_factory, Settings(_env_file=None), Recorded(redis_client))  # type: ignore[call-arg]
        peer = await service.add_peer(project.id, WireGuardPeerCreate(name="tv"))
        assert peer.address == "10.66.0.2"

        doc = WireGuardServerSettingsDoc(endpoint_host="vpn.example.net", subnet="10.66.0.0/24", persistent_keepalive=15)
        await forget_announcements(redis_client)
        saved = await service.save_settings(doc, "admin")
        assert (saved.endpoint_host, saved.subnet, saved.persistent_keepalive) == ("vpn.example.net", "10.66.0.0/24", 15)
        assert await announced(redis_client) == [("wireguard", "")]

        with pytest.raises(TunnelValidationError, match="outside 10.99.0.0/24"):
            await service.save_settings(WireGuardServerSettingsDoc(subnet="10.99.0.0/24"), "admin")
        # A device on the new subnet's gateway (its first host) could not be told from the gateway.
        await WireGuardPeerRepository(db_session).update(peer.model_copy(update={"address": "10.66.1.1"}))
        await db_session.commit()
        with pytest.raises(TunnelValidationError, match="on its gateway 10.66.1.1"):
            await service.save_settings(WireGuardServerSettingsDoc(subnet="10.66.1.0/24"), "admin")
        # The data plane tells devices apart by address across both protocols.
        with pytest.raises(TunnelValidationError, match="overlaps the OpenVPN subnet"):
            await service.save_settings(WireGuardServerSettingsDoc(subnet="10.64.0.0/14"), "admin")
        assert (await service.settings()).subnet == "10.66.0.0/24"
        # A refused save announces nothing.
        assert await announced(redis_client) == [("wireguard", "")]

    async def test_rotating_the_key(self, db_session_factory: Factory, redis_client: RedisClient) -> None:
        service = _service(db_session_factory, redis_client)
        before = await service.settings()
        rotated = await service.rotate_key("admin")
        assert rotated.public_key != before.public_key and rotated.public_key == keys.public_key_of(rotated.private_key)
        assert (await service.settings()).private_key == rotated.private_key


class TestDevices:
    async def test_keys_are_generated_or_brought(self, db_session_factory: Factory, redis_client: RedisClient, project: Project) -> None:
        service = _service(db_session_factory, redis_client)
        generated = await service.add_peer(project.id, WireGuardPeerCreate(name="tv"))
        assert generated.private_key is not None and generated.public_key == keys.public_key_of(generated.private_key)
        assert generated.preshared_key is not None
        brought_key = keys.generate_keypair()[1]
        brought = await service.add_peer(project.id, WireGuardPeerCreate(name="laptop", public_key=brought_key, preshared=False))
        assert brought.private_key is None and brought.public_key == brought_key and brought.preshared_key is None
        assert [p.id for p in await service.peers(project.id)] == [generated.id, brought.id]

    async def test_addresses_come_from_postgres(self, db_session_factory: Factory, redis_client: RedisClient, project: Project) -> None:
        service = _service(db_session_factory, redis_client, defaults={"subnet": "10.66.0.0/29"})
        first = await service.add_peer(project.id, WireGuardPeerCreate(name="a"))
        second = await service.add_peer(project.id, WireGuardPeerCreate(name="b"))
        # The gateway (.1) is skipped; each device takes the lowest free host.
        assert (first.address, second.address) == ("10.66.0.2", "10.66.0.3")
        assert await service.remove_peer(project.id, first.id)
        assert (await service.add_peer(project.id, WireGuardPeerCreate(name="c"))).address == "10.66.0.2"
        for name in ("d", "e", "f"):
            await service.add_peer(project.id, WireGuardPeerCreate(name=name))
        with pytest.raises(TunnelAddressError, match="no free address left in 10.66.0.0/29"):
            await service.add_peer(project.id, WireGuardPeerCreate(name="g"))

    async def test_a_device_created_at_the_same_moment_is_a_conflict_to_retry(
        self, db_session_factory: Factory, redis_client: RedisClient, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = _service(db_session_factory, redis_client)
        await service.add_peer(project.id, WireGuardPeerCreate(name="first"))
        # Another api replica read the used addresses before the first insert committed.
        monkeypatch.setattr(WireGuardPeerRepository, "used_addresses", AsyncMock(return_value=set()))
        with pytest.raises(TunnelAddressError, match="taken concurrently; retry"):
            await service.add_peer(project.id, WireGuardPeerCreate(name="second"))
        monkeypatch.undo()
        assert (await service.add_peer(project.id, WireGuardPeerCreate(name="second"))).address == "10.66.0.3"

    async def test_names_and_keys_are_unique(self, db_session: AsyncSession, db_session_factory: Factory, redis_client: RedisClient, project: Project) -> None:
        service = _service(db_session_factory, redis_client)
        tv = await service.add_peer(project.id, WireGuardPeerCreate(name="TV"))
        with pytest.raises(TunnelValidationError, match="named 'tv' already exists"):
            await service.add_peer(project.id, WireGuardPeerCreate(name="tv"))
        with pytest.raises(TunnelValidationError, match="this public key already exists"):
            await service.add_peer(project.id, WireGuardPeerCreate(name="other", public_key=tv.public_key))
        elsewhere = await _project(db_session, "Kitchen")
        assert (await service.add_peer(elsewhere.id, WireGuardPeerCreate(name="TV"))).project_id == elsewhere.id

    async def test_updates_are_partial_and_scoped_to_the_project(
        self, db_session: AsyncSession, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        service = _service(db_session_factory, redis_client)
        tv = await service.add_peer(project.id, WireGuardPeerCreate(name="tv", country="de", session_id="s1"))
        await service.add_peer(project.id, WireGuardPeerCreate(name="console"))
        await forget_announcements(redis_client)

        # A null name or enabled is ignored; "" clears an optional field.
        updated = await service.update_peer(project.id, tv.id, WireGuardPeerUpdate(name=None, enabled=False, session_id=""))
        assert updated is not None and (updated.name, updated.enabled, updated.session_id, updated.country) == ("tv", False, None, "DE")
        assert await announced(redis_client) == [("wireguard", tv.id)]
        assert (await service.peer(project.id, tv.id)).enabled is False  # type: ignore[union-attr]

        with pytest.raises(TunnelValidationError, match="country"):
            await service.update_peer(project.id, tv.id, WireGuardPeerUpdate(country="", city="Berlin"))
        with pytest.raises(TunnelValidationError, match="named 'console' already exists"):
            await service.update_peer(project.id, tv.id, WireGuardPeerUpdate(name="console"))
        elsewhere = await _project(db_session, "Kitchen")
        assert await service.update_peer(elsewhere.id, tv.id, WireGuardPeerUpdate(enabled=True)) is None
        assert await service.peer(elsewhere.id, tv.id) is None
        assert await service.rotate_peer_keys(elsewhere.id, tv.id) is None
        assert await service.remove_peer(elsewhere.id, tv.id) is False
        assert await service.client_config(elsewhere.id, tv.id) is None
        # Refused or not the project's: nothing more was announced.
        assert await announced(redis_client) == [("wireguard", tv.id)]

    async def test_rotating_a_devices_keys(self, db_session_factory: Factory, redis_client: RedisClient, project: Project) -> None:
        service = _service(db_session_factory, redis_client)
        with_psk = await service.add_peer(project.id, WireGuardPeerCreate(name="tv"))
        without = await service.add_peer(project.id, WireGuardPeerCreate(name="laptop", preshared=False))
        rotated = await service.rotate_peer_keys(project.id, with_psk.id)
        assert rotated is not None and rotated.public_key != with_psk.public_key
        assert rotated.preshared_key is not None and rotated.preshared_key != with_psk.preshared_key
        assert (await service.rotate_peer_keys(project.id, without.id)).preshared_key is None  # type: ignore[union-attr]
        assert (await service.peer(project.id, with_psk.id)).public_key == rotated.public_key  # type: ignore[union-attr]

    async def test_removing_drops_the_row_and_the_unflushed_window(
        self, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        service = _service(db_session_factory, redis_client)
        tv = await service.add_peer(project.id, WireGuardPeerCreate(name="tv"))
        key = TUNNEL_PEER_METRICS_KEY.format(peer_id=tv.id)
        await redis_client.client.hset(key, mapping={"request_count": "3"})
        assert await service.remove_peer(project.id, tv.id) is True
        assert await service.peer(project.id, tv.id) is None
        assert not await redis_client.client.exists(key)

    async def test_a_removal_stands_when_the_window_cannot_be_dropped(
        self, db_session_factory: Factory, redis_client: RedisClient, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = _service(db_session_factory, redis_client)
        tv = await service.add_peer(project.id, WireGuardPeerCreate(name="tv"))

        async def down(peer_id: str) -> None:
            raise ConnectionError("Redis is down")

        monkeypatch.setattr(redis_client, "delete_tunnel_peer_window", down)
        with structlog.testing.capture_logs() as logs:
            assert await service.remove_peer(project.id, tv.id) is True
        assert await service.peer(project.id, tv.id) is None
        assert [log["step"] for log in logs if log["event"] == "Redis step after a write failed"] == [
            "drop a deleted device's metric window"
        ]

    async def test_the_device_config(self, db_session_factory: Factory, redis_client: RedisClient, project: Project) -> None:
        service = _service(db_session_factory, redis_client)
        tv = await service.add_peer(project.id, WireGuardPeerCreate(name="Living room TV"))
        config = await service.client_config(project.id, tv.id)
        assert config is not None and config.filename == "Living-room-TV.conf"
        assert f"Address = {tv.address}/32" in config.config and "DNS = 10.66.0.1" in config.config
        # Issued before the endpoint is set, and flagged as such.
        assert (config.complete, config.server_configured) == (False, False)
        await service.save_settings(WireGuardServerSettingsDoc(endpoint_host="vpn.example.net"), "admin")
        config = await service.client_config(project.id, tv.id)
        assert config is not None and config.complete and "Endpoint = vpn.example.net:51820" in config.config


class TestPeerStatus:
    """The live reading when a gateway has one; the sighting on record otherwise, or when it is newer."""

    def test_nothing_carrying_falls_back_to_the_row(self) -> None:
        seen = utc_now() - timedelta(seconds=30)
        peer = WireGuardPeer(project_id="p", name="tv", public_key="k", address="10.66.0.2", last_handshake_at=seen, last_endpoint="9.9.9.9:1")
        status = WireGuardService.peer_status(peer, None)
        assert status.online is True and status.live is False
        assert status.last_handshake_at == seen and status.endpoint == "9.9.9.9:1"
        assert (status.rx_bytes, status.tx_bytes) == (0, 0)
        never = WireGuardService.peer_status(WireGuardPeer(project_id="p", name="tv", public_key="k", address="10.66.0.3"), None)
        assert never.online is False and never.last_handshake_at is None and never.endpoint is None

    def test_restarted_gateway_without_a_handshake_keeps_the_last_sighting(self) -> None:
        seen = utc_now() - timedelta(hours=2)
        peer = WireGuardPeer(project_id="p", name="tv", public_key="k", address="10.66.0.2", last_handshake_at=seen, last_endpoint="9.9.9.9:1")
        live = WireGuardPeerStatus(online=False, last_handshake_at=None, live=True, rx_bytes=12, tx_bytes=34)
        status = WireGuardService.peer_status(peer, live)
        assert status.live is False and status.last_handshake_at == seen and status.online is False
        # The interface counters are still the gateway's.
        assert (status.rx_bytes, status.tx_bytes) == (12, 34)

    def test_live_reading_wins_when_it_is_newer(self) -> None:
        seen = utc_now() - timedelta(minutes=10)
        peer = WireGuardPeer(project_id="p", name="tv", public_key="k", address="10.66.0.2", last_handshake_at=seen)
        live = WireGuardPeerStatus(online=True, last_handshake_at=seen + timedelta(minutes=9), endpoint="1.1.1.1:5", live=True)
        assert WireGuardService.peer_status(peer, live) is live
        older = WireGuardPeerStatus(online=False, last_handshake_at=seen - timedelta(days=1), endpoint="old", live=True)
        assert WireGuardService.peer_status(peer, older).last_handshake_at == seen


class TestStatus:
    async def test_readings_merge_across_gateways(self, db_session_factory: Factory, redis_client: RedisClient) -> None:
        now = int(time.time())
        # Two gateways behind a UDP load balancer: every device appears in both
        # dumps, and the newer handshake is where the device's traffic lands.
        await redis_client.set_tunnel_peer_status("wireguard", "gw-a", {
            "online": json.dumps({"latest_handshake": now - 30, "rx_bytes": 5, "tx_bytes": 7, "endpoint": "1.2.3.4:1"}),
            "stale": json.dumps({"latest_handshake": now - 3600, "rx_bytes": 0, "tx_bytes": 0, "endpoint": None}),
            "never": json.dumps({"latest_handshake": 0}),
            "junk": "{not json",
        })
        await redis_client.set_tunnel_peer_status("wireguard", "gw-b", {
            "online": json.dumps({"latest_handshake": now - 900, "rx_bytes": 99, "tx_bytes": 99, "endpoint": "1.2.3.4:2"}),
            "stale": json.dumps({"latest_handshake": now - 60, "rx_bytes": 3, "tx_bytes": 4, "endpoint": "5.6.7.8:9"}),
        })
        statuses = await _service(db_session_factory, redis_client).peer_statuses()
        assert statuses["online"].online is True and statuses["online"].rx_bytes == 5 and statuses["online"].live is True
        assert statuses["stale"].online is True and statuses["stale"].endpoint == "5.6.7.8:9"
        assert statuses["never"].online is False and statuses["never"].last_handshake_at is None
        assert "junk" not in statuses

    async def test_status_lists_the_gateways_and_sums_the_devices(
        self, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        totals = Totals()
        gateways = [_gateway("gw-b", backend="kernel"), _gateway("gw-ovpn", protocol="openvpn"), _gateway("gw-a", backend="userspace")]
        service = _service(db_session_factory, redis_client, gateways=gateways, totals=totals)
        a = await service.add_peer(project.id, WireGuardPeerCreate(name="a"))
        b = await service.add_peer(project.id, WireGuardPeerCreate(name="b", enabled=False))
        totals.by_id = {a.id: TunnelPeerMetricDelta(by_address=2, encrypted_dns_blocked=1), b.id: TunnelPeerMetricDelta(by_address=3)}
        await redis_client.set_tunnel_peer_status("wireguard", "gw-a", {
            a.public_key: json.dumps({"latest_handshake": int(time.time()) - 5, "rx_bytes": 1, "tx_bytes": 2, "endpoint": "1.1.1.1:1"}),
        })

        status = await service.status()
        assert [g.instance_id for g in status.gateways] == ["gw-a", "gw-b"]
        assert (status.peers_total, status.peers_enabled, status.peers_online) == (2, 1, 1)
        assert (status.connections_by_address, status.encrypted_dns_blocked) == (5, 1)

        response = await service.settings_response()
        assert response.status == status and response.gateway == "10.66.0.1" and response.configured is False

        listed = await service.peer_list(project.id)
        assert listed.total == 2 and listed.server_public_key == response.public_key
        by_name = {p.name: p for p in listed.peers}
        assert by_name["a"].status.live is True and by_name["a"].status.online is True
        assert by_name["a"].metrics.connections_by_address == 2 and by_name["a"].has_private_key
        assert by_name["b"].status.live is False and by_name["b"].metrics.connections_by_address == 3
        assert totals.asked[-1] == [a.id, b.id]

    async def test_status_without_redis_or_heartbeats(self, db_session_factory: Factory, project: Project) -> None:
        redis = MagicMock()
        redis.get_tunnel_peer_status = AsyncMock(side_effect=ConnectionError("Redis is gone"))

        async def no_heartbeats() -> list[TunnelEndpointState]:
            raise ConnectionError("Redis is gone")

        service = WireGuardService(
            db_session_factory,
            redis,
            endpoint_states=no_heartbeats,
            device_totals=Totals(),
            defaults={},
            changes=Recorded(redis),
        )
        peer = await service.add_peer(project.id, WireGuardPeerCreate(name="tv"))
        status = await service.status()
        assert status.gateways == [] and status.peers_total == 1 and status.peers_online == 0
        assert (await service.response(peer)).status.live is False


class TestChangeEntries:
    async def test_each_write_appends_one_wireguard_entry(
        self, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        service = _service(db_session_factory, redis_client)
        await service.settings()
        await forget_announcements(redis_client)

        await service.save_settings(WireGuardServerSettingsDoc(endpoint_host="vpn.example.net"), "admin")
        await service.rotate_key("admin")
        tv = await service.add_peer(project.id, WireGuardPeerCreate(name="tv"))
        await service.update_peer(project.id, tv.id, WireGuardPeerUpdate(enabled=False))
        await service.rotate_peer_keys(project.id, tv.id)
        await service.remove_peer(project.id, tv.id)

        assert await announced(redis_client) == [
            ("wireguard", ""),
            ("wireguard", ""),
            ("wireguard", tv.id),
            ("wireguard", tv.id),
            ("wireguard", tv.id),
            ("wireguard", tv.id),
        ]

    async def test_a_read_announces_only_the_row_it_had_to_create(
        self, db_session: AsyncSession, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        """A backup restored without the row: the read that writes it again tells the gateways."""
        service = _service(db_session_factory, redis_client)
        tv = await service.add_peer(project.id, WireGuardPeerCreate(name="tv"))
        await forget_announcements(redis_client)

        await service.client_config(project.id, tv.id)
        await service.peer_list(project.id)
        assert await announced(redis_client) == []

        await db_session.execute(text("DELETE FROM wireguard_settings"))
        await db_session.commit()
        await service.peer_list(project.id)
        await service.client_config(project.id, tv.id)

        assert await announced(redis_client) == [("wireguard", "")]

    async def test_each_entry_follows_its_commit(
        self, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        """A controller woken by an entry compiles from Postgres: the write must be there by then."""
        service: WireGuardService

        async def look(ref: str) -> object:
            return await service.peer(project.id, ref) if ref else (await service.settings()).public_key

        seen: list[object] = []

        class Witness(ChangeLog):
            async def record(self, kind: ChangeKind, *ids: str) -> None:
                seen.append(await look(ref_of(ids)))

        service = _service(db_session_factory, redis_client, changes=Witness(redis_client))
        await service.settings()
        seen.clear()

        rotated = await service.rotate_key("admin")
        tv = await service.add_peer(project.id, WireGuardPeerCreate(name="tv"))
        await service.remove_peer(project.id, tv.id)

        assert seen == [rotated.public_key, tv, None]
