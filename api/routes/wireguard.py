# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""WireGuard endpoints: the install's server settings and each project's devices.

Reads are open to every authenticated user. Peers are project entities, so
editors manage them like credentials; the server settings shape the whole
install (and rotating its key invalidates every device config), so they need
an admin.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.exc import IntegrityError

from api.core.auth import RequireAdminDep, RequireEditorDep
from api.models.location import LOCATION_NEEDS_COUNTRY
from api.models.wireguard import (
    WireGuardPeer,
    WireGuardPeerConfigResponse,
    WireGuardPeerCreate,
    WireGuardPeerListResponse,
    WireGuardPeerResponse,
    WireGuardPeerStatus,
    WireGuardPeerUpdate,
    WireGuardServerSettings,
    WireGuardServerSettingsDoc,
    WireGuardServerSettingsResponse,
)
from api.routes.common import proxy_manager_of, wireguard_runtime_of
from api.wireguard import keys
from api.wireguard.config import client_conf_filename, render_client_conf
from api.wireguard.peers import NoFreeAddressError

if TYPE_CHECKING:
    from api.wireguard.runtime import WireGuardRuntime

logger = structlog.get_logger()

server_router = APIRouter(prefix="/wireguard")
router = APIRouter(prefix="/projects/{project_id}/wireguard/peers")


def _require_project(request: Request, project_id: str) -> None:
    if proxy_manager_of(request).get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")


def _require_peer(runtime: WireGuardRuntime, project_id: str, peer_id: str) -> WireGuardPeer:
    peer = runtime.peers.get(peer_id)
    if peer is None or peer.project_id != project_id:
        raise HTTPException(status_code=404, detail="Peer not found")
    return peer


def _peer_response(peer: WireGuardPeer, status: WireGuardPeerStatus | None) -> WireGuardPeerResponse:
    return WireGuardPeerResponse(
        id=peer.id,
        project_id=peer.project_id,
        name=peer.name,
        public_key=peer.public_key,
        has_private_key=peer.private_key is not None,
        has_preshared_key=peer.preshared_key is not None,
        address=peer.address,
        enabled=peer.enabled,
        session_id=peer.session_id,
        country=peer.country,
        state=peer.state,
        city=peer.city,
        created_at=peer.created_at,
        updated_at=peer.updated_at,
        status=status,
    )


def _name_conflict(exc: IntegrityError, name: str) -> HTTPException:
    text = str(exc.orig or exc)
    if "ix_wireguard_peers_project_name_unique" in text:
        return HTTPException(status_code=400, detail=f"A device named '{name}' already exists in this project")
    if "public_key" in text:
        return HTTPException(status_code=400, detail="A device with this public key already exists")
    if "address" in text:
        return HTTPException(status_code=409, detail="The tunnel address was taken concurrently; retry")
    raise exc


async def _settings_response(runtime: WireGuardRuntime) -> WireGuardServerSettingsResponse:
    server = runtime.settings_store.settings
    return WireGuardServerSettingsResponse(
        public_key=server.public_key,
        endpoint_host=server.endpoint_host,
        endpoint_port=server.endpoint_port,
        subnet=server.subnet,
        gateway=server.gateway,
        persistent_keepalive=server.persistent_keepalive,
        client_mtu=server.client_mtu,
        configured=server.configured,
        updated_at=server.updated_at,
        status=await runtime.status(),
    )


# --- server ------------------------------------------------------------------------------


@server_router.get("/settings", response_model=WireGuardServerSettingsResponse)
async def get_server_settings(request: Request) -> WireGuardServerSettingsResponse:
    """The install's public key, endpoint and subnet, plus what this instance is doing about the tunnel."""
    return await _settings_response(wireguard_runtime_of(request))


@server_router.put("/settings", response_model=WireGuardServerSettingsResponse)
async def update_server_settings(
    request: Request, doc: WireGuardServerSettingsDoc, admin: RequireAdminDep
) -> WireGuardServerSettingsResponse:
    """Replace the editable settings. The subnet must still contain every device's address."""
    runtime = wireguard_runtime_of(request)
    updated = runtime.settings_store.settings.model_copy(update=doc.model_dump())
    network, gateway = updated.network, updated.gateway
    outside = [p.address for p in runtime.peers.all() if ipaddress.IPv4Address(p.address) not in network or p.address == gateway]
    if outside:
        raise HTTPException(
            status_code=400,
            detail=f"{len(outside)} device(s) have addresses outside {updated.subnet} (or on its gateway {gateway}); remove them first",
        )
    await runtime.save_settings(updated, admin.username)
    return await _settings_response(runtime)


@server_router.post("/settings/rotate-key", response_model=WireGuardServerSettingsResponse)
async def rotate_server_key(request: Request, admin: RequireAdminDep) -> WireGuardServerSettingsResponse:
    """Give the install a new key pair. Every device must load a new config afterwards."""
    runtime = wireguard_runtime_of(request)
    private, public = keys.generate_keypair()
    updated = runtime.settings_store.settings.model_copy(update={"private_key": private, "public_key": public})
    await runtime.save_settings(updated, admin.username)
    logger.warning("WireGuard server key rotated; every device config is now invalid", by=admin.username)
    return await _settings_response(runtime)


# --- peers -------------------------------------------------------------------------------


@router.get("", response_model=WireGuardPeerListResponse)
async def list_peers(request: Request, project_id: str) -> WireGuardPeerListResponse:
    _require_project(request, project_id)
    runtime = wireguard_runtime_of(request)
    statuses = await runtime.peer_statuses()
    peers = runtime.peers.for_project(project_id)
    server = runtime.settings_store.settings
    return WireGuardPeerListResponse(
        total=len(peers),
        peers=[_peer_response(p, statuses.get(p.public_key)) for p in peers],
        server_configured=server.configured,
        server_public_key=server.public_key,
    )


@router.post("", response_model=WireGuardPeerResponse, status_code=201)
async def create_peer(
    request: Request, project_id: str, data: WireGuardPeerCreate, _guard: RequireEditorDep
) -> WireGuardPeerResponse:
    """Register a device. Without a public key Octoprox generates the pair and keeps it for the config."""
    _require_project(request, project_id)
    runtime = wireguard_runtime_of(request)

    private_key: str | None
    if data.public_key:
        private_key, public_key = None, data.public_key
    else:
        private_key, public_key = keys.generate_keypair()

    try:
        address = runtime.peers.allocate_address(runtime.settings_store.settings.network)
    except NoFreeAddressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    peer = WireGuardPeer(
        project_id=project_id,
        name=data.name,
        public_key=public_key,
        private_key=private_key,
        preshared_key=keys.generate_preshared_key() if data.preshared else None,
        address=address,
        enabled=data.enabled,
        session_id=data.session_id,
        country=data.country,
        state=data.state,
        city=data.city,
    )
    try:
        await runtime.add_peer(peer)
    except IntegrityError as exc:
        raise _name_conflict(exc, peer.name) from None
    return _peer_response(peer, None)


@router.get("/{peer_id}", response_model=WireGuardPeerResponse)
async def get_peer(request: Request, project_id: str, peer_id: str) -> WireGuardPeerResponse:
    _require_project(request, project_id)
    runtime = wireguard_runtime_of(request)
    peer = _require_peer(runtime, project_id, peer_id)
    statuses = await runtime.peer_statuses()
    return _peer_response(peer, statuses.get(peer.public_key))


@router.patch("/{peer_id}", response_model=WireGuardPeerResponse)
async def update_peer(
    request: Request, project_id: str, peer_id: str, data: WireGuardPeerUpdate, _guard: RequireEditorDep
) -> WireGuardPeerResponse:
    _require_project(request, project_id)
    runtime = wireguard_runtime_of(request)
    peer = _require_peer(runtime, project_id, peer_id)

    # "" clears an optional field. name and enabled cannot be cleared, so an
    # explicit null for them is ignored rather than becoming a NOT NULL error.
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
    statuses = await runtime.peer_statuses()
    return _peer_response(updated, statuses.get(updated.public_key))


@router.delete("/{peer_id}", status_code=204)
async def delete_peer(request: Request, project_id: str, peer_id: str, _guard: RequireEditorDep) -> None:
    _require_project(request, project_id)
    runtime = wireguard_runtime_of(request)
    _require_peer(runtime, project_id, peer_id)
    await runtime.remove_peer(peer_id)


@router.post("/{peer_id}/rotate-keys", response_model=WireGuardPeerResponse)
async def rotate_peer_keys(
    request: Request, project_id: str, peer_id: str, _guard: RequireEditorDep
) -> WireGuardPeerResponse:
    """New key pair (and preshared key, if it had one) for a device; the old config stops working."""
    _require_project(request, project_id)
    runtime = wireguard_runtime_of(request)
    peer = _require_peer(runtime, project_id, peer_id)
    private, public = keys.generate_keypair()
    updated = peer.model_copy(
        update={
            "private_key": private,
            "public_key": public,
            "preshared_key": keys.generate_preshared_key() if peer.preshared_key else None,
        }
    )
    await runtime.update_peer(updated)
    return _peer_response(updated, None)


@router.get("/{peer_id}/config", response_model=WireGuardPeerConfigResponse)
async def get_peer_config(request: Request, project_id: str, peer_id: str) -> WireGuardPeerConfigResponse:
    """The device's wg-quick file. Issued even before the endpoint is set, flagged as such."""
    _require_project(request, project_id)
    runtime = wireguard_runtime_of(request)
    peer = _require_peer(runtime, project_id, peer_id)
    server: WireGuardServerSettings = runtime.settings_store.settings
    return WireGuardPeerConfigResponse(
        filename=client_conf_filename(peer.name),
        config=render_client_conf(peer, server),
        complete=peer.private_key is not None and server.configured,
        server_configured=server.configured,
    )
