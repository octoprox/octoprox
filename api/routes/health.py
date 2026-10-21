# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The ops endpoints every role serves on port 8000: ``/live``, ``/ready``, ``/health`` and ``/metrics``.

All four are public and read memory only, so a probe costs no Redis or Postgres round trip, and uvicorn's access lines
for them are dropped below DEBUG (``PROBE_PATHS``).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from api import __version__
from api.core.roles import Role
from api.core.telemetry import CONTENT_TYPE
from api.routes.common import node_of

router = APIRouter()

# The status /ready and /health answer with while the process cannot serve.
UNAVAILABLE = 503


class LiveResponse(BaseModel):
    """The process serves."""

    status: Literal["alive"] = "alive"


class ReadyResponse(BaseModel):
    """Whether the process takes traffic, and if not, why."""

    status: Literal["ready", "unready"]
    role: Role
    # One per runtime that is not ready, prefixed with its name; empty when ready.
    reasons: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """The readiness check with the process's identity; ``reasons`` only when it is not ready."""

    status: Literal["healthy", "unhealthy"]
    role: Role
    instance: str
    version: str
    reasons: list[str] | None = None


@router.get("/live", response_model=LiveResponse)
async def liveness_check() -> LiveResponse:
    """Liveness probe: 200 while the process answers at all."""
    return LiveResponse()


@router.get("/ready", response_model=ReadyResponse, responses={UNAVAILABLE: {"model": ReadyResponse}})
async def readiness_check(request: Request) -> Response:
    """Readiness probe: 200 while every runtime of the process is ready, else 503 with the reasons."""
    node = node_of(request)
    reasons = node.readiness()
    body = ReadyResponse(status="unready" if reasons else "ready", role=node.role, reasons=reasons)
    return JSONResponse(status_code=UNAVAILABLE if reasons else 200, content=body.model_dump(mode="json"))


@router.get("/health", response_model=HealthResponse, responses={UNAVAILABLE: {"model": HealthResponse}})
async def health_check(request: Request) -> Response:
    """The readiness check for health checks that read a body: who answers, and why not when it cannot serve."""
    node = node_of(request)
    reasons = node.readiness()
    body = HealthResponse(
        status="unhealthy" if reasons else "healthy",
        role=node.role,
        instance=node.instance_id,
        version=__version__,
        reasons=reasons or None,
    )
    return JSONResponse(
        status_code=UNAVAILABLE if reasons else 200, content=body.model_dump(mode="json", exclude_none=True)
    )


@router.get("/metrics", response_class=Response)
async def metrics(request: Request) -> Response:
    """Prometheus text exposition of this process, from its snapshot now."""
    return Response(content=node_of(request).telemetry.render(), media_type=CONTENT_TYPE)
