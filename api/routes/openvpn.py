# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""OpenVPN endpoints: the install's server settings and each project's devices.

The same shape and permissions as the WireGuard routes: reads for every
authenticated user, devices for editors, the install-wide settings (and
rotating the identity, which invalidates every device profile) for admins.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Literal

import structlog
from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError

from api.core import utc_now
from api.core.auth import RequireAdminDep, RequireEditorDep
from api.db.repository import MetricsRepository
from api.models.location import LOCATION_NEEDS_COUNTRY
from api.models.openvpn import (
    OpenVpnPeer,
    OpenVpnPeerConfigResponse,
    OpenVpnPeerCreate,
    OpenVpnPeerListResponse,
    OpenVpnPeerResponse,
    OpenVpnPeerStatus,
    OpenVpnPeerUpdate,
    OpenVpnServerSettingsDoc,
    OpenVpnServerSettingsResponse,
)
from api.models.tunnel import (
    TunnelPeerMetrics,
    TunnelPeerMetricsHistoryResponse,
    TunnelPeerMetricsSnapshot,
)
from api.openvpn import pki
from api.openvpn.config import client_profile_filename, render_client_profile
from api.routes.common import openvpn_runtime_of, proxy_manager_of, wireguard_runtime_of
from api.routes.metrics import RANGE_CONFIG
from api.tunnel.peers import NoFreeAddressError

if TYPE_CHECKING:
    from api.openvpn.runtime import OpenVpnRuntime

logger = structlog.get_logger()

server_router = APIRouter(prefix="/openvpn")
router = APIRouter(prefix="/projects/{project_id}/openvpn/peers")


def _require_project(request: Request, project_id: str) -> None:
    if proxy_manager_of(request).get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")


def _require_peer(runtime: OpenVpnRuntime, project_id: str, peer_id: str) -> OpenVpnPeer:
    peer = runtime.peers.get(peer_id)
    if peer is None or peer.project_id != project_id:
        raise HTTPException(status_code=404, detail="Peer not found")
    return peer


def _peer_response(runtime: OpenVpnRuntime, peer: OpenVpnPeer, live: dict[str, OpenVpnPeerStatus]) -> OpenVpnPeerResponse:
    return OpenVpnPeerResponse(
        id=peer.id,
        project_id=peer.project_id,
        name=peer.name,
        serial=peer.serial,
        certificate_expires_at=peer.certificate_expires_at,
        address=peer.address,
        enabled=peer.enabled,
        session_id=peer.session_id,
        country=peer.country,
        state=peer.state,
        city=peer.city,
        created_at=peer.created_at,
        updated_at=peer.updated_at,
        status=runtime.peer_status(peer, live.get(peer.id)),
        metrics=TunnelPeerMetrics.from_delta(runtime.metrics_of(peer.id)),
    )


def _name_conflict(exc: IntegrityError, name: str) -> HTTPException:
    text = str(exc.orig or exc)
    if "ix_openvpn_peers_project_name_unique" in text:
        return HTTPException(status_code=400, detail=f"A device named '{name}' already exists in this project")
    if "address" in text:
        return HTTPException(status_code=409, detail="The tunnel address was taken concurrently; retry")
    raise exc


async def _settings_response(runtime: OpenVpnRuntime) -> OpenVpnServerSettingsResponse:
    server = runtime.settings_store.settings
    return OpenVpnServerSettingsResponse(
        ca_fingerprint=pki.fingerprint_of(server.ca_cert),
        ca_expires_at=pki.not_after_of(server.ca_cert),
        endpoint_host=server.endpoint_host,
        endpoint_port=server.endpoint_port,
        protocol=server.protocol,
        subnet=server.subnet,
        gateway=server.gateway,
        keepalive_interval=server.keepalive_interval,
        keepalive_timeout=server.keepalive_timeout,
        client_mtu=server.client_mtu,
        configured=server.configured,
        updated_at=server.updated_at,
        status=await runtime.status(),
    )


# --- server ------------------------------------------------------------------------------


@server_router.get("/settings", response_model=OpenVpnServerSettingsResponse)
async def get_server_settings(request: Request) -> OpenVpnServerSettingsResponse:
    """The install's CA fingerprint, endpoint, transport and subnet, plus what this instance is doing about the daemon."""
    return await _settings_response(openvpn_runtime_of(request))


@server_router.put("/settings", response_model=OpenVpnServerSettingsResponse)
async def update_server_settings(
    request: Request, doc: OpenVpnServerSettingsDoc, admin: RequireAdminDep
) -> OpenVpnServerSettingsResponse:
    """Replace the editable settings. The subnet must still contain every device's address."""
    runtime = openvpn_runtime_of(request)
    updated = runtime.settings_store.settings.model_copy(update=doc.model_dump())
    network, gateway = updated.network, updated.gateway
    # The data plane tells devices apart by tunnel address across every
    # protocol, so the two subnets must not share any.
    other = wireguard_runtime_of(request).settings_store.settings.network
    if network.overlaps(other):
        raise HTTPException(status_code=400, detail=f"subnet {updated.subnet} overlaps the WireGuard subnet {other}")
    outside = [p.address for p in runtime.peers.all() if ipaddress.IPv4Address(p.address) not in network or p.address == gateway]
    if outside:
        raise HTTPException(
            status_code=400,
            detail=f"{len(outside)} device(s) have addresses outside {updated.subnet} (or on its gateway {gateway}); remove them first",
        )
    await runtime.save_settings(updated, admin.username)
    return await _settings_response(runtime)


@server_router.post("/settings/rotate-identity", response_model=OpenVpnServerSettingsResponse)
async def rotate_identity(request: Request, admin: RequireAdminDep) -> OpenVpnServerSettingsResponse:
    """A new CA, server certificate and tls-crypt key; every device is reissued and must load its profile again."""
    runtime = openvpn_runtime_of(request)
    await runtime.rotate_identity(admin.username)
    return await _settings_response(runtime)


# --- peers -------------------------------------------------------------------------------


@router.get("", response_model=OpenVpnPeerListResponse)
async def list_peers(request: Request, project_id: str) -> OpenVpnPeerListResponse:
    _require_project(request, project_id)
    runtime = openvpn_runtime_of(request)
    live = await runtime.peer_statuses()
    peers = runtime.peers.for_project(project_id)
    server = runtime.settings_store.settings
    return OpenVpnPeerListResponse(
        total=len(peers),
        peers=[_peer_response(runtime, p, live) for p in peers],
        server_configured=server.configured,
        ca_fingerprint=pki.fingerprint_of(server.ca_cert),
    )


@router.post("", response_model=OpenVpnPeerResponse, status_code=201)
async def create_peer(
    request: Request, project_id: str, data: OpenVpnPeerCreate, _guard: RequireEditorDep
) -> OpenVpnPeerResponse:
    """Register a device: a certificate from the install's CA and the next free address."""
    _require_project(request, project_id)
    runtime = openvpn_runtime_of(request)
    try:
        peer = runtime.issue_peer(
            project_id=project_id,
            name=data.name,
            enabled=data.enabled,
            session_id=data.session_id,
            country=data.country,
            state=data.state,
            city=data.city,
        )
    except NoFreeAddressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    try:
        await runtime.add_peer(peer)
    except IntegrityError as exc:
        raise _name_conflict(exc, peer.name) from None
    return _peer_response(runtime, peer, {})


@router.get("/{peer_id}", response_model=OpenVpnPeerResponse)
async def get_peer(request: Request, project_id: str, peer_id: str) -> OpenVpnPeerResponse:
    _require_project(request, project_id)
    runtime = openvpn_runtime_of(request)
    peer = _require_peer(runtime, project_id, peer_id)
    return _peer_response(runtime, peer, await runtime.peer_statuses())


@router.patch("/{peer_id}", response_model=OpenVpnPeerResponse)
async def update_peer(
    request: Request, project_id: str, peer_id: str, data: OpenVpnPeerUpdate, _guard: RequireEditorDep
) -> OpenVpnPeerResponse:
    _require_project(request, project_id)
    runtime = openvpn_runtime_of(request)
    peer = _require_peer(runtime, project_id, peer_id)
    changes = {
        key: (None if value == "" else value)
        for key, value in data.model_dump(exclude_unset=True).items()
        if not (value is None and key in ("name", "enabled"))
    }
    updated = peer.model_copy(update=changes)
    if (updated.state or updated.city) and not updated.country:
        raise HTTPException(status_code=400, detail=LOCATION_NEEDS_COUNTRY)
    try:
        await runtime.update_peer(updated)
    except IntegrityError as exc:
        raise _name_conflict(exc, updated.name) from None
    return _peer_response(runtime, updated, await runtime.peer_statuses())


@router.delete("/{peer_id}", status_code=204)
async def delete_peer(request: Request, project_id: str, peer_id: str, _guard: RequireEditorDep) -> None:
    _require_project(request, project_id)
    runtime = openvpn_runtime_of(request)
    _require_peer(runtime, project_id, peer_id)
    await runtime.remove_peer(peer_id)


@router.post("/{peer_id}/rotate-certificate", response_model=OpenVpnPeerResponse)
async def rotate_peer_certificate(
    request: Request, project_id: str, peer_id: str, _guard: RequireEditorDep
) -> OpenVpnPeerResponse:
    """A new certificate and key for a device; the profile it has stops working."""
    _require_project(request, project_id)
    runtime = openvpn_runtime_of(request)
    peer = _require_peer(runtime, project_id, peer_id)
    updated = runtime.reissue_certificate(peer)
    await runtime.update_peer(updated)
    return _peer_response(runtime, updated, {})


@router.get("/{peer_id}/metrics/history", response_model=TunnelPeerMetricsHistoryResponse)
async def get_peer_metrics_history(
    request: Request,
    project_id: str,
    peer_id: str,
    range: Literal["1h", "6h", "24h", "7d", "30d"] = Query("24h", alias="range"),
) -> TunnelPeerMetricsHistoryResponse:
    """The device's own metrics history, same ranges and tiers as the WireGuard devices'."""
    _require_project(request, project_id)
    _require_peer(openvpn_runtime_of(request), project_id, peer_id)
    delta, limit, bucket_seconds = RANGE_CONFIG[range]
    since = utc_now() - delta
    async with proxy_manager_of(request)._session_factory() as session:
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
async def get_peer_config(request: Request, project_id: str, peer_id: str) -> OpenVpnPeerConfigResponse:
    """The device's ``.ovpn`` profile. Issued even before the endpoint is set, flagged as such."""
    _require_project(request, project_id)
    runtime = openvpn_runtime_of(request)
    peer = _require_peer(runtime, project_id, peer_id)
    server = runtime.settings_store.settings
    return OpenVpnPeerConfigResponse(
        filename=client_profile_filename(peer.name),
        config=render_client_profile(peer, server),
        complete=server.configured,
        server_configured=server.configured,
    )
