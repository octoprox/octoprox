# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""WireGuard endpoints: the install's server settings and each project's devices.

Served by the api role (and ``all``), over the WireGuard service
(:mod:`api.wireguard.service`): each call is one Postgres transaction, and a
write reaches the gateways carrying the tunnel through the routing feed,
never from here. The status shows every gateway carrying the tunnel, as
their heartbeats report it, and each device where the gateways last saw it.

Reads are open to every signed-in user. Devices are project entities, so
editors manage them like credentials; a device's config carries its private
key when Octoprox generated it, so downloading one needs an editor too. The
server settings shape the whole install (and rotating its key invalidates
every device config), so they need an admin.

The service's refusals answer through the app's error handlers: a name or
key in use, or a subnet that would strand a device, is a 400; a tunnel
address taken at the same moment, or a full subnet, a 409.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from api.core import utc_now
from api.core.auth import RequireAdminDep, RequireEditorDep
from api.db.repository import MetricsRepository
from api.models.tunnel import TunnelPeerMetricsHistoryResponse, TunnelPeerMetricsSnapshot
from api.models.wireguard import (
    WireGuardPeerConfigResponse,
    WireGuardPeerCreate,
    WireGuardPeerListResponse,
    WireGuardPeerResponse,
    WireGuardPeerUpdate,
    WireGuardServerSettingsDoc,
    WireGuardServerSettingsResponse,
)
from api.routes.common import require_project, session_factory_of, wireguard_service_of
from api.routes.metrics import RANGE_CONFIG

server_router = APIRouter(prefix="/wireguard")
router = APIRouter(prefix="/projects/{project_id}/wireguard/peers")


def _found[T](value: T | None) -> T:
    """``value``; 404 when the device does not exist or is another project's."""
    if value is None:
        raise HTTPException(status_code=404, detail="Peer not found")
    return value


# --- server ------------------------------------------------------------------------------


@server_router.get("/settings", response_model=WireGuardServerSettingsResponse)
async def get_server_settings(request: Request) -> WireGuardServerSettingsResponse:
    """The install's public key, endpoint and subnet, and the tunnel on every gateway carrying it."""
    return await wireguard_service_of(request).settings_response()


@server_router.put("/settings", response_model=WireGuardServerSettingsResponse)
async def update_server_settings(
    request: Request, doc: WireGuardServerSettingsDoc, admin: RequireAdminDep
) -> WireGuardServerSettingsResponse:
    """Replace the editable settings. The subnet must still contain every device's address."""
    service = wireguard_service_of(request)
    await service.save_settings(doc, admin.username)
    return await service.settings_response()


@server_router.post("/settings/rotate-key", response_model=WireGuardServerSettingsResponse)
async def rotate_server_key(request: Request, admin: RequireAdminDep) -> WireGuardServerSettingsResponse:
    """Give the install a new key pair. Every device must load a new config afterwards."""
    service = wireguard_service_of(request)
    await service.rotate_key(admin.username)
    return await service.settings_response()


# --- peers -------------------------------------------------------------------------------


@router.get("", response_model=WireGuardPeerListResponse)
async def list_peers(request: Request, project_id: str) -> WireGuardPeerListResponse:
    """A project's devices, oldest first, each with where it stands and its traffic."""
    await require_project(request, project_id)
    return await wireguard_service_of(request).peer_list(project_id)


@router.post("", response_model=WireGuardPeerResponse, status_code=201)
async def create_peer(
    request: Request, project_id: str, data: WireGuardPeerCreate, _guard: RequireEditorDep
) -> WireGuardPeerResponse:
    """Register a device. Without a public key Octoprox generates the pair and keeps it for the config."""
    await require_project(request, project_id)
    service = wireguard_service_of(request)
    return await service.response(await service.add_peer(project_id, data))


@router.get("/{peer_id}", response_model=WireGuardPeerResponse)
async def get_peer(request: Request, project_id: str, peer_id: str) -> WireGuardPeerResponse:
    await require_project(request, project_id)
    service = wireguard_service_of(request)
    return await service.response(_found(await service.peer(project_id, peer_id)))


@router.patch("/{peer_id}", response_model=WireGuardPeerResponse)
async def update_peer(
    request: Request, project_id: str, peer_id: str, data: WireGuardPeerUpdate, _guard: RequireEditorDep
) -> WireGuardPeerResponse:
    """Change a device: "" clears an optional field; a state or city needs a country."""
    await require_project(request, project_id)
    service = wireguard_service_of(request)
    return await service.response(_found(await service.update_peer(project_id, peer_id, data)))


@router.delete("/{peer_id}", status_code=204)
async def delete_peer(request: Request, project_id: str, peer_id: str, _guard: RequireEditorDep) -> None:
    await require_project(request, project_id)
    if not await wireguard_service_of(request).remove_peer(project_id, peer_id):
        raise HTTPException(status_code=404, detail="Peer not found")


@router.post("/{peer_id}/rotate-keys", response_model=WireGuardPeerResponse)
async def rotate_peer_keys(
    request: Request, project_id: str, peer_id: str, _guard: RequireEditorDep
) -> WireGuardPeerResponse:
    """New key pair (and preshared key, if it had one) for a device; the old config stops working."""
    await require_project(request, project_id)
    service = wireguard_service_of(request)
    return await service.response(_found(await service.rotate_peer_keys(project_id, peer_id)))


@router.get("/{peer_id}/metrics/history", response_model=TunnelPeerMetricsHistoryResponse)
async def get_peer_metrics_history(
    request: Request,
    project_id: str,
    peer_id: str,
    range: Literal["1h", "6h", "24h", "7d", "30d"] = Query("24h", alias="range"),
) -> TunnelPeerMetricsHistoryResponse:
    """The device's own metrics history: requests, bytes and name-resolution signals per interval.

    Same ranges and compaction tiers as project and connector history. Bytes
    are what the proxy path relayed for the device, not the tunnel
    interface's counters.
    """
    await require_project(request, project_id)
    _found(await wireguard_service_of(request).peer(project_id, peer_id))
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


@router.get("/{peer_id}/config", response_model=WireGuardPeerConfigResponse)
async def get_peer_config(
    request: Request, project_id: str, peer_id: str, _guard: RequireEditorDep
) -> WireGuardPeerConfigResponse:
    """The device's wg-quick file, with the private key Octoprox generated; issued before the endpoint is set, flagged."""
    await require_project(request, project_id)
    return _found(await wireguard_service_of(request).client_config(project_id, peer_id))
