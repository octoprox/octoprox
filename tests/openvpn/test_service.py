# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The api's OpenVPN service against Postgres and Redis: CA, certificates, devices, status.

What a write announces is read off the changes stream, as in the WireGuard
service's tests.
"""

import json
import time
from datetime import timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.core import utc_now
from api.core.config import Settings
from api.core.stats import TunnelPeerMetricDelta
from api.db.openvpn_repository import OpenVpnPeerRepository
from api.db.redis import RedisClient
from api.db.streams import ChangeKind, ChangeLog, ref_of
from api.models.openvpn import OpenVpnPeerCreate, OpenVpnPeerUpdate, OpenVpnServerSettingsDoc
from api.models.project import Project
from api.models.tunnel import TunnelEndpointState, TunnelValidationError
from api.openvpn import pki
from api.openvpn.service import OpenVpnService, defaults_from_config
from api.wireguard.service import seed_tunnel_settings
from tests.wireguard.test_service import (
    Recorded,
    Totals,
    _gateway,
    _project,
    announced,
    forget_announcements,
)

Factory = async_sessionmaker[AsyncSession]


def _service(
    factory: Factory,
    redis: Any,
    *,
    gateways: list[TunnelEndpointState] | None = None,
    totals: Totals | None = None,
    changes: ChangeLog | None = None,
) -> OpenVpnService:
    async def endpoint_states() -> list[TunnelEndpointState]:
        return list(gateways or [])

    return OpenVpnService(
        factory,
        redis,
        endpoint_states=endpoint_states,
        device_totals=totals or Totals(),
        defaults={},
        changes=changes or ChangeLog(redis),
    )


@pytest.fixture
async def project(db_session: AsyncSession) -> Project:
    return await _project(db_session)


def test_defaults_seed_from_config() -> None:
    seeded = defaults_from_config({"endpoint_host": "vpn.example.net", "protocol": "tcp", "subnet": "10.77.0.0/24"})
    assert seeded.endpoint_host == "vpn.example.net" and seeded.protocol == "tcp"
    assert pki.is_issued_by(seeded.server_cert, seeded.ca_cert) and pki.is_tls_crypt_key(seeded.tls_crypt_key)
    assert defaults_from_config({"subnet": "nonsense"}).subnet == "10.67.0.0/16"


class TestCertificates:
    async def test_devices_are_issued_by_the_ca_in_the_row(self, db_session_factory: Factory, redis_client: RedisClient, project: Project) -> None:
        service = _service(db_session_factory, redis_client)
        peer = await service.add_peer(project.id, OpenVpnPeerCreate(name="tv", country="DE"))
        server = await service.settings()
        assert peer.address == "10.67.0.2" and pki.common_name_of(peer.certificate) == peer.id
        assert pki.is_issued_by(peer.certificate, server.ca_cert) and peer.serial == pki.serial_of(peer.certificate)
        assert peer.certificate_expires_at == pki.not_after_of(peer.certificate)

        await forget_announcements(redis_client)
        rotated = await service.rotate_certificate(project.id, peer.id)
        assert rotated is not None and rotated.serial != peer.serial and rotated.address == peer.address
        assert pki.is_issued_by(rotated.certificate, server.ca_cert)
        assert (await service.peer(project.id, peer.id)).serial == rotated.serial  # type: ignore[union-attr]
        assert await announced(redis_client) == [("openvpn", peer.id)]

    async def test_rotating_the_identity_reissues_every_device_with_it(
        self, db_session: AsyncSession, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        service = _service(db_session_factory, redis_client)
        devices = [await service.add_peer(project.id, OpenVpnPeerCreate(name=name)) for name in ("tv", "console")]
        old = await service.settings()
        await forget_announcements(redis_client)
        rotated = await service.rotate_identity("admin")
        assert rotated.ca_cert != old.ca_cert and pki.is_issued_by(rotated.server_cert, rotated.ca_cert)
        reissued = await OpenVpnPeerRepository(db_session).get_all()
        assert {p.id for p in reissued} == {d.id for d in devices}
        for device in reissued:
            assert pki.is_issued_by(device.certificate, rotated.ca_cert)
            assert device.serial not in {d.serial for d in devices}
        # The row and every device changed in one transaction: one entry.
        assert await announced(redis_client) == [("openvpn", "")]

    async def test_the_device_profile(self, db_session_factory: Factory, redis_client: RedisClient, project: Project) -> None:
        service = _service(db_session_factory, redis_client)
        tv = await service.add_peer(project.id, OpenVpnPeerCreate(name="Living room TV"))
        profile = await service.client_profile(project.id, tv.id)
        assert profile is not None and profile.filename == "Living-room-TV.ovpn"
        assert "<cert>" in profile.config and "remote <set the public endpoint" in profile.config
        assert (profile.complete, profile.server_configured) == (False, False)
        await service.save_settings(OpenVpnServerSettingsDoc(endpoint_host="vpn.example.net"), "admin")
        profile = await service.client_profile(project.id, tv.id)
        assert profile is not None and profile.complete and "remote vpn.example.net 1194" in profile.config


class TestSettingsAndDevices:
    async def test_the_subnet_must_hold_every_device_and_stay_clear_of_wireguard(
        self, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        await seed_tunnel_settings(db_session_factory, Settings(_env_file=None), Recorded(redis_client))  # type: ignore[call-arg]
        service = _service(db_session_factory, redis_client)
        await service.add_peer(project.id, OpenVpnPeerCreate(name="tv"))
        saved = await service.save_settings(OpenVpnServerSettingsDoc(subnet="10.67.0.0/24", protocol="tcp"), "admin")
        assert (saved.subnet, saved.protocol) == ("10.67.0.0/24", "tcp")
        with pytest.raises(TunnelValidationError, match="outside 10.68.0.0/24"):
            await service.save_settings(OpenVpnServerSettingsDoc(subnet="10.68.0.0/24"), "admin")
        with pytest.raises(TunnelValidationError, match="overlaps the WireGuard subnet"):
            await service.save_settings(OpenVpnServerSettingsDoc(subnet="10.64.0.0/14"), "admin")

    async def test_updates_and_removal(self, db_session: AsyncSession, db_session_factory: Factory, redis_client: RedisClient, project: Project) -> None:
        service = _service(db_session_factory, redis_client)
        tv = await service.add_peer(project.id, OpenVpnPeerCreate(name="tv"))
        await service.add_peer(project.id, OpenVpnPeerCreate(name="console"))
        updated = await service.update_peer(project.id, tv.id, OpenVpnPeerUpdate(enabled=False, country="de", state="BE"))
        assert updated is not None and (updated.enabled, updated.country, updated.state) == (False, "DE", "BE")
        assert updated.serial == tv.serial
        with pytest.raises(TunnelValidationError, match="named 'Console' already exists"):
            await service.update_peer(project.id, tv.id, OpenVpnPeerUpdate(name="Console"))
        elsewhere = await _project(db_session, "Kitchen")
        assert await service.update_peer(elsewhere.id, tv.id, OpenVpnPeerUpdate(enabled=True)) is None
        assert await service.rotate_certificate(elsewhere.id, tv.id) is None
        assert await service.remove_peer(elsewhere.id, tv.id) is False
        assert await service.remove_peer(project.id, tv.id) is True
        assert [p.name for p in await service.peers(project.id)] == ["console"]

    async def test_a_removal_stands_when_the_window_cannot_be_dropped(
        self, db_session_factory: Factory, redis_client: RedisClient, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = _service(db_session_factory, redis_client)
        tv = await service.add_peer(project.id, OpenVpnPeerCreate(name="tv"))

        async def down(peer_id: str) -> None:
            raise ConnectionError("Redis is down")

        monkeypatch.setattr(redis_client, "delete_tunnel_peer_window", down)
        with structlog.testing.capture_logs() as logs:
            assert await service.remove_peer(project.id, tv.id) is True
        assert await service.peer(project.id, tv.id) is None
        assert [log["step"] for log in logs if log["event"] == "Redis step after a write failed"] == [
            "drop a deleted device's metric window"
        ]


class TestStatus:
    async def test_the_newer_session_wins_and_the_row_fills_in(
        self, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        totals = Totals()
        service = _service(
            db_session_factory, redis_client, totals=totals,
            gateways=[_gateway("gw-2", protocol="openvpn", daemon_version="OpenVPN 2.6.3"), _gateway("gw-wg")],
        )
        tv = await service.add_peer(project.id, OpenVpnPeerCreate(name="tv"))
        console = await service.add_peer(project.id, OpenVpnPeerCreate(name="console"))
        totals.by_id = {tv.id: TunnelPeerMetricDelta(request_count=4, encrypted_dns_blocked=2)}
        now = int(time.time())
        # Two gateways: the newer session is where the device is now.
        await redis_client.set_tunnel_peer_status("openvpn", "gw-1", {
            tv.id: json.dumps({"connected_since": now - 300, "rx_bytes": 1, "tx_bytes": 1, "endpoint": "old:1"}),
            "junk": "{",
        })
        await redis_client.set_tunnel_peer_status("openvpn", "gw-2", {
            tv.id: json.dumps({"connected_since": now - 30, "rx_bytes": 5, "tx_bytes": 7, "endpoint": "203.0.113.9:1"}),
        })
        live = await service.peer_statuses()
        assert set(live) == {tv.id}
        assert live[tv.id].online and live[tv.id].endpoint == "203.0.113.9:1" and live[tv.id].rx_bytes == 5

        # The last connection on record, as the tunnel last-seen job writes it, shows while nothing carries the device.
        seen = utc_now() - timedelta(hours=1)
        async with db_session_factory() as session:
            assert await OpenVpnPeerRepository(session).record_last_seen([(console.id, seen, "198.51.100.1:2")]) == 1
            await session.commit()
        responses = {r.name: r for r in (await service.peer_list(project.id)).peers}
        assert responses["tv"].status.live is True and responses["tv"].metrics.request_count == 4
        offline = responses["console"].status
        assert offline.online is False and offline.live is False
        assert offline.last_seen_at == seen and offline.endpoint == "198.51.100.1:2"

        status = await service.status()
        assert [g.instance_id for g in status.gateways] == ["gw-2"]
        assert (status.peers_total, status.peers_enabled, status.peers_online, status.encrypted_dns_blocked) == (2, 2, 1, 2)
        response = await service.settings_response()
        assert response.ca_fingerprint == pki.fingerprint_of((await service.settings()).ca_cert)
        assert response.status.gateways[0].daemon_version == "OpenVPN 2.6.3"


class TestLastSeen:
    """The tunnel last-seen job records sightings through the repository: forward only, never a feed change."""

    async def test_last_connections_only_move_forward(self, db_session_factory: Factory, redis_client: RedisClient, project: Project) -> None:
        service = _service(db_session_factory, redis_client)
        tv = await service.add_peer(project.id, OpenVpnPeerCreate(name="tv"))
        stored = await service.peer(project.id, tv.id)
        assert stored is not None
        newer, older = utc_now(), utc_now() - timedelta(minutes=5)
        async with db_session_factory() as session:
            repo = OpenVpnPeerRepository(session)
            assert await repo.record_last_seen([(tv.id, newer, "203.0.113.9:1"), ("gone", newer, None)]) == 1
            # Another gateway reports an older session: the row keeps the newer one.
            assert await repo.record_last_seen([(tv.id, older, "198.51.100.1:2")]) == 0
            await session.commit()
        seen = await service.peer(project.id, tv.id)
        assert seen is not None and seen.last_connected_at == newer and seen.last_endpoint == "203.0.113.9:1"
        # A sighting is not an edit: the definition and its updated_at stay as they were.
        assert seen.updated_at == stored.updated_at


class TestChangeEntries:
    async def test_each_write_appends_one_openvpn_entry(
        self, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        service = _service(db_session_factory, redis_client)
        await service.settings()
        await forget_announcements(redis_client)

        await service.save_settings(OpenVpnServerSettingsDoc(endpoint_host="vpn.example.net"), "admin")
        tv = await service.add_peer(project.id, OpenVpnPeerCreate(name="tv"))
        await service.update_peer(project.id, tv.id, OpenVpnPeerUpdate(enabled=False))
        await service.rotate_certificate(project.id, tv.id)
        await service.rotate_identity("admin")
        await service.remove_peer(project.id, tv.id)

        assert await announced(redis_client) == [
            ("openvpn", ""),
            ("openvpn", tv.id),
            ("openvpn", tv.id),
            ("openvpn", tv.id),
            ("openvpn", ""),
            ("openvpn", tv.id),
        ]

    async def test_a_refused_write_announces_nothing(
        self, db_session: AsyncSession, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        service = _service(db_session_factory, redis_client)
        tv = await service.add_peer(project.id, OpenVpnPeerCreate(name="tv"))
        await service.add_peer(project.id, OpenVpnPeerCreate(name="console"))
        elsewhere = await _project(db_session, "Kitchen")
        await forget_announcements(redis_client)

        with pytest.raises(TunnelValidationError):
            await service.update_peer(project.id, tv.id, OpenVpnPeerUpdate(name="Console"))
        with pytest.raises(TunnelValidationError, match="outside 10.68.0.0/24"):
            await service.save_settings(OpenVpnServerSettingsDoc(subnet="10.68.0.0/24"), "admin")
        assert await service.update_peer(elsewhere.id, tv.id, OpenVpnPeerUpdate(enabled=True)) is None
        assert await service.rotate_certificate(elsewhere.id, tv.id) is None
        assert await service.remove_peer(elsewhere.id, tv.id) is False

        assert await announced(redis_client) == []

    async def test_a_read_announces_only_the_row_it_had_to_create(
        self, db_session: AsyncSession, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        """A backup restored without the row: the read that writes it again tells the gateways."""
        service = _service(db_session_factory, redis_client)
        tv = await service.add_peer(project.id, OpenVpnPeerCreate(name="tv"))
        await forget_announcements(redis_client)

        await service.client_profile(project.id, tv.id)
        await service.peer_list(project.id)
        await service.settings()
        assert await announced(redis_client) == []

        await db_session.execute(text("DELETE FROM openvpn_settings"))
        await db_session.commit()
        await service.peer_list(project.id)
        await service.client_profile(project.id, tv.id)

        assert await announced(redis_client) == [("openvpn", "")]

    async def test_each_entry_follows_its_commit(
        self, db_session_factory: Factory, redis_client: RedisClient, project: Project
    ) -> None:
        """A controller woken by an entry compiles from Postgres: the write must be there by then."""
        seen: list[object] = []
        service: OpenVpnService

        class Witness(ChangeLog):
            async def record(self, kind: ChangeKind, *ids: str) -> None:
                ref = ref_of(ids)
                seen.append(await service.peer(project.id, ref) if ref else (await service.settings()).ca_cert)

        service = _service(db_session_factory, redis_client, changes=Witness(redis_client))
        await service.settings()
        seen.clear()

        rotated = await service.rotate_identity("admin")
        tv = await service.add_peer(project.id, OpenVpnPeerCreate(name="tv"))
        await service.remove_peer(project.id, tv.id)

        assert seen == [rotated.ca_cert, tv, None]
