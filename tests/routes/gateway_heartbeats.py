# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Fake gateway heartbeats for route tests: what a live gateway publishes, written into the app's Redis.

Route tests run role ``api`` with no gateway, and the api reads every gateway from its heartbeat
(``ApiRuntime.instance_snapshots``), so they advertise gateways the way one would and withdraw them when done.
"""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from api.models.system import InstanceSnapshot
from tests.conftest import api_runtime, on_app_loop

# Every field a gateway's section of its heartbeat requires, idle.
_IDLE_GATEWAY: dict[str, Any] = {
    "listener_port": 8080,
    "listening": True,
    "active_connections": 0,
    "retired_slots": 0,
    "local_quarantines": 0,
    "in_flight": 0,
    "requests": 0,
    "successes": 0,
    "failures": 0,
    "bytes_sent": 0,
    "bytes_received": 0,
    "failures_by_kind": {},
    "pending_metric_deltas": 0,
    "metric_flush_failures": 0,
    "rate_limit_errors": 0,
    "preflight_checks": 0,
    "preflight_rejections": 0,
    "geo_databases_loaded": [],
    "geo_database_errors": {},
    "geo_databases_missing": [],
    "observations_pending": 0,
    "observations_dropped": 0,
    "sightings_pending": 0,
    "sightings_dropped": 0,
    "failure_log_dropped": 0,
    "mitm_captures_dropped": 0,
    "tunnels": [],
}


def gateway_heartbeat(instance_id: str, **gateway: Any) -> str:
    """The heartbeat a gateway publishes, idle but for ``gateway``'s fields (validated as the api reads them)."""
    snapshot = InstanceSnapshot.model_validate(
        {
            "runtime": {"version": "6.0.0", "role": "gateway", "instance_id": instance_id, "pid": 1},
            "ready": True,
            "gateway": _IDLE_GATEWAY | gateway,
        }
    )
    return snapshot.model_dump_json()


def advertise_gateway(client: TestClient, instance_id: str, **gateway: Any) -> None:
    """Publish one heartbeat of a live gateway in the client's app's Redis."""
    redis = api_runtime(client).redis_client
    on_app_loop(client, redis.advertise_instance, instance_id, "gateway", gateway_heartbeat(instance_id, **gateway))


def withdraw_gateway(client: TestClient, instance_id: str) -> None:
    """Take a gateway out of the app's Redis, as one stopping does."""
    on_app_loop(client, api_runtime(client).redis_client.withdraw_instance, instance_id)
