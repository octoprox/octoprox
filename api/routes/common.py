# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Helpers shared by the entity routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import HTTPException, Request
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from api.core.proxy_manager import ProxyManager
    from api.geo.runtime import GeoRuntime
    from api.wireguard.runtime import WireGuardRuntime


def proxy_manager_of(request: Request) -> ProxyManager:
    """The process's proxy manager, as the lifespan left it on ``app.state``."""
    return cast("ProxyManager", request.app.state.proxy_manager)


def geo_runtime_of(request: Request) -> GeoRuntime:
    return cast("GeoRuntime", request.app.state.geo_runtime)


def wireguard_runtime_of(request: Request) -> WireGuardRuntime:
    return cast("WireGuardRuntime", request.app.state.wireguard_runtime)


def unique_name_violation(exc: IntegrityError, kind: str, name: str) -> HTTPException:
    """Translate a hit on the per-project unique name index into a 400.

    The database is the single source of truth for name uniqueness (see
    migration 022); routes call this from an ``except IntegrityError`` so the
    user gets a readable message. Any other integrity error is re-raised
    unchanged, so it still surfaces as a server error rather than being
    mislabelled.
    """
    if f"ix_{kind}s_project_name_unique" not in str(exc.orig or exc):
        raise exc
    return HTTPException(status_code=400, detail=f"A {kind} named '{name}' already exists in this project")
