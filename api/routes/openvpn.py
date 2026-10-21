# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""OpenVPN endpoints: the install's server settings and each project's devices.

The same shape as the WireGuard routes, over the OpenVPN service
(:mod:`api.openvpn.service`): one Postgres transaction per call, writes
reaching the gateways that run the endpoint through the routing feed, and
the status showing every such gateway as its heartbeat reports it. A
device's certificate is issued here with the CA key read from the settings
row; the CA key is never shown.

Reads are open to every signed-in user, devices are for editors (a profile
carries the device's private key, so downloading one needs an editor too),
and the install-wide settings, and rotating the identity, which invalidates
every device profile, are for admins. The service's refusals answer through
the app's error handlers: 400 for a name in use or a subnet that would
strand a device, 409 for a tunnel address taken at the same moment or a
full subnet.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from api.core import utc_now
from api.core.auth import RequireAdminDep, RequireEditorDep
from api.db.repository import MetricsRepository
from api.models.openvpn import (
    OpenVpnPeerConfigResponse,
    OpenVpnPeerCreate,
    OpenVpnPeerListResponse,
    OpenVpnPeerResponse,
    OpenVpnPeerUpdate,
    OpenVpnServerSettingsDoc,
    OpenVpnServerSettingsResponse,
)
from api.models.tunnel import TunnelPeerMetricsHistoryResponse, TunnelPeerMetricsSnapshot
from api.routes.common import openvpn_service_of, require_project, session_factory_of
from api.routes.metrics import RANGE_CONFIG

server_router = APIRouter(prefix="/openvpn")
router = APIRouter(prefix="/projects/{project_id}/openvpn/peers")


def _found[T](value: T | None) -> T:
    """``value``; 404 when the device does not exist or is another project's."""
    if value is None:
        raise HTTPException(status_code=404, detail="Peer not found")
    return value


# --- server ------------------------------------------------------------------------------


@server_router.get("/settings", response_model=OpenVpnServerSettingsResponse)
async def get_server_settings(request: Request) -> OpenVpnServerSettingsResponse:
    """The install's CA fingerprint, endpoint, transport and subnet, and the endpoint on every gateway running it."""
    return await openvpn_service_of(request).settings_response()


@server_router.put("/settings", response_model=OpenVpnServerSettingsResponse)
async def update_server_settings(
    request: Request, doc: OpenVpnServerSettingsDoc, admin: RequireAdminDep
) -> OpenVpnServerSettingsResponse:
    """Replace the editable settings. The subnet must still contain every device's address."""
    service = openvpn_service_of(request)
    await service.save_settings(doc, admin.username)
    return await service.settings_response()


@server_router.post("/settings/rotate-identity", response_model=OpenVpnServerSettingsResponse)
async def rotate_identity(request: Request, admin: RequireAdminDep) -> OpenVpnServerSettingsResponse:
    """A new CA, server certificate and tls-crypt key; every device is reissued and must load its profile again."""
    service = openvpn_service_of(request)
    await service.rotate_identity(admin.username)
    return await service.settings_response()


# --- peers -------------------------------------------------------------------------------


@router.get("", response_model=OpenVpnPeerListResponse)
async def list_peers(request: Request, project_id: str) -> OpenVpnPeerListResponse:
    """A project's devices, oldest first, each with where it stands and its traffic."""
    await require_project(request, project_id)
    return await openvpn_service_of(request).peer_list(project_id)


@router.post("", response_model=OpenVpnPeerResponse, status_code=201)
async def create_peer(
    request: Request, project_id: str, data: OpenVpnPeerCreate, _guard: RequireEditorDep
) -> OpenVpnPeerResponse:
    """Register a device: a certificate from the install's CA and the next free address."""
    await require_project(request, project_id)
    service = openvpn_service_of(request)
    return await service.response(await service.add_peer(project_id, data))


@router.get("/{peer_id}", response_model=OpenVpnPeerResponse)
async def get_peer(request: Request, project_id: str, peer_id: str) -> OpenVpnPeerResponse:
    await require_project(request, project_id)
    service = openvpn_service_of(request)
    return await service.response(_found(await service.peer(project_id, peer_id)))


@router.patch("/{peer_id}", response_model=OpenVpnPeerResponse)
async def update_peer(
    request: Request, project_id: str, peer_id: str, data: OpenVpnPeerUpdate, _guard: RequireEditorDep
) -> OpenVpnPeerResponse:
    """Change a device: "" clears an optional field; a state or city needs a country."""
    await require_project(request, project_id)
    service = openvpn_service_of(request)
    return await service.response(_found(await service.update_peer(project_id, peer_id, data)))


@router.delete("/{peer_id}", status_code=204)
async def delete_peer(request: Request, project_id: str, peer_id: str, _guard: RequireEditorDep) -> None:
    await require_project(request, project_id)
    if not await openvpn_service_of(request).remove_peer(project_id, peer_id):
        raise HTTPException(status_code=404, detail="Peer not found")


@router.post("/{peer_id}/rotate-certificate", response_model=OpenVpnPeerResponse)
async def rotate_peer_certificate(
    request: Request, project_id: str, peer_id: str, _guard: RequireEditorDep
) -> OpenVpnPeerResponse:
    """A new certificate and key for a device; the profile it has stops working."""
    await require_project(request, project_id)
    service = openvpn_service_of(request)
    return await service.response(_found(await service.rotate_certificate(project_id, peer_id)))


@router.get("/{peer_id}/metrics/history", response_model=TunnelPeerMetricsHistoryResponse)
async def get_peer_metrics_history(
    request: Request,
    project_id: str,
    peer_id: str,
    range: Literal["1h", "6h", "24h", "7d", "30d"] = Query("24h", alias="range"),
) -> TunnelPeerMetricsHistoryResponse:
    """The device's own metrics history, same ranges and tiers as the WireGuard devices'."""
    await require_project(request, project_id)
    _found(await openvpn_service_of(request).peer(project_id, peer_id))
    delta, limit, bucket_seconds = RANGE_CONFIG[range]
    since = utc_now() - delta
    async with session_factory_of(request)() as session:
        repo = MetricsRepository(session)
        if bucket_seconds:
            rows = await repo.get_tunnel_peer_metrics_history_aggregated(
                project_id=project_id, peer_id=peer_id, since=since, bucket_seconds=bucket_seconds
            )
        else:
            rows = await repo.get_tunnel_peer_metrics_history(
                project_id=project_id, peer_id=peer_id, since=since, limit=limit, granularity=60
            )
    return TunnelPeerMetricsHistoryResponse(snapshots=[TunnelPeerMetricsSnapshot(**row) for row in reversed(rows)])


@router.get("/{peer_id}/config", response_model=OpenVpnPeerConfigResponse)
async def get_peer_config(
    request: Request, project_id: str, peer_id: str, _guard: RequireEditorDep
) -> OpenVpnPeerConfigResponse:
    """The device's ``.ovpn`` profile, with its private key; issued before the endpoint is set, flagged as such."""
    await require_project(request, project_id)
    return _found(await openvpn_service_of(request).client_profile(project_id, peer_id))
