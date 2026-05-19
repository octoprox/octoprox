# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Event bus that wraps blinker signals behind pluggable transports.

The bus has two transport slots: a local transport (always present) that
forwards to in-process blinker subscribers, and an optional distributed
transport (Redis Pub/Sub) for the subset of signals classified as
cross-instance. The bus itself knows nothing about specific entity types —
when a publisher wants the event to reach other instances, it passes
``entity_id=<id>`` explicitly; the distributed transport forwards just
``(signal_name, instance_id, entity_id, op)``.

Receivers continue to subscribe via ``signal.connect(handler)`` for
in-process events. Cross-instance receivers run a dedicated subscriber
loop (see ``ProxyManager._cross_instance_subscriber_loop``) that bridges
pub/sub messages directly into per-entity reload methods — they do NOT
re-emit into blinker (that would cause infinite echoes between
instances).

There is exactly one ``EventBus`` instance per process, exposed via the
``@lru_cache``-d ``get_event_bus()`` factory — same pattern as
``get_settings()`` / ``get_redis_client(...)`` elsewhere in the
project. Constructing a second ``EventBus()`` directly is a mistake:
local subscribers would fire twice and distributed publishes would
duplicate. The bus is configured exactly once in
``ProxyManager.start()`` and reset in ``ProxyManager.stop()``.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import structlog
from blinker import Signal

if TYPE_CHECKING:
    from api.db.redis import RedisClient

logger = structlog.get_logger()

# Redis Pub/Sub channel name used for cross-instance event fanout.
EVENT_CHANNEL = "octoprox:events"


class Transport(ABC):
    """Abstract event transport."""

    @abstractmethod
    async def publish(
        self,
        signal: Signal,
        sender: Any,
        *,
        entity_id: str | None,
        op: str | None,
        **kwargs: Any,
    ) -> None:
        ...


class LocalTransport(Transport):
    """In-process transport: forwards directly to the blinker signal.

    Local subscribers receive ``entity_id`` and ``op`` as keyword args
    alongside whatever else the publisher attached, so existing
    in-process handlers do not need to change to subscribe.
    """

    async def publish(
        self,
        signal: Signal,
        sender: Any,
        *,
        entity_id: str | None,
        op: str | None,
        **kwargs: Any,
    ) -> None:
        forward = dict(kwargs)
        if entity_id is not None:
            forward["entity_id"] = entity_id
        if op is not None:
            forward["op"] = op
        await signal.send_async(sender, **forward)


class RedisPubSubTransport(Transport):
    """Cross-instance transport: JSON message on a Redis Pub/Sub channel.

    Only ``(signal_name, instance_id, entity_id, op)`` is sent. Receivers
    re-read the entity from Postgres / Redis (idempotent reload) rather
    than trying to deserialize complex payloads. Self-echoes are dropped
    on the receiver side by ``instance_id``.
    """

    def __init__(
        self,
        redis_client: RedisClient,
        instance_id: str,
        channel: str = EVENT_CHANNEL,
    ) -> None:
        self._redis_client = redis_client
        self._instance_id = instance_id
        self._channel = channel

    async def publish(
        self,
        signal: Signal,
        sender: Any,
        *,
        entity_id: str | None,
        op: str | None,
        **kwargs: Any,
    ) -> None:
        # The distributed transport ignores unrelated kwargs by design —
        # callers attach domain payload for local subscribers, but only
        # the minimal envelope crosses the wire.
        name = getattr(signal, "name", "")
        if not entity_id:
            logger.warning(
                "Cross-instance signal published without entity_id; dropping",
                signal=name,
            )
            return
        payload = json.dumps(
            {
                "signal": name,
                "instance_id": self._instance_id,
                "entity_id": entity_id,
                "op": op,
            }
        )
        try:
            await self._redis_client.client.publish(self._channel, payload)
        except Exception:
            logger.warning("Cross-instance publish failed", signal=name, exc_info=True)


class EventBus:
    """Routes published events through configured transports.

    Local transport is always wired (forwards to blinker). The distributed
    transport is opt-in: call ``configure_distributed(...)`` once Redis is
    connected and the instance id is known. There should be exactly one
    ``EventBus`` instance per process (the module-level ``event_bus``
    singleton).

    Args:
        local: Transport used for in-process delivery. Defaults to
            ``LocalTransport``.
    """

    def __init__(self, local: Transport | None = None) -> None:
        self._local = local or LocalTransport()
        self._distributed: Transport | None = None
        self._cross_instance_names: set[str] = set()

    def configure_distributed(
        self,
        transport: Transport,
        cross_instance_signals: list[Signal],
    ) -> None:
        """Wire the distributed transport and the set of signals it carries.

        Idempotent — calling again replaces the prior configuration.
        """
        self._distributed = transport
        self._cross_instance_names = {
            getattr(s, "name", "") for s in cross_instance_signals
        }
        logger.info(
            "EventBus distributed transport configured",
            cross_instance_signals=sorted(self._cross_instance_names),
        )

    def reset_distributed(self) -> None:
        """Drop the distributed transport (used in tests / shutdown)."""
        self._distributed = None
        self._cross_instance_names.clear()

    @property
    def cross_instance_signals(self) -> frozenset[str]:
        return frozenset(self._cross_instance_names)

    async def publish(
        self,
        signal: Signal,
        sender: Any,
        *,
        entity_id: str | None = None,
        op: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Publish an event.

        Args:
            signal: blinker signal to fire locally and (if classified
                cross-instance) forward over Redis.
            sender: blinker convention for the publishing object.
            entity_id: REQUIRED for cross-instance signals so peers can
                identify what to reload. For local-only signals it is
                still useful (and passed through to subscribers) but may
                be omitted.
            op: Optional verb (e.g. ``"added"``, ``"updated"``,
                ``"removed"``, ``"quarantined"``, ``"released"``) so
                subscribers can branch without re-reading state.
            **kwargs: Additional payload attached to the local fan-out.
                The distributed transport does not forward these.
        """
        await self._local.publish(signal, sender, entity_id=entity_id, op=op, **kwargs)
        name = getattr(signal, "name", "")
        if self._distributed is not None and name in self._cross_instance_names:
            await self._distributed.publish(
                signal, sender, entity_id=entity_id, op=op, **kwargs
            )


@lru_cache
def get_event_bus() -> EventBus:
    """Return the process-wide ``EventBus`` instance.

    Cached (same pattern as ``get_settings`` / ``get_redis_client``)
    so every caller sees the same object. Reset between tests via
    ``EventBus.reset_distributed()`` rather than rebuilding.
    """
    return EventBus()


# Module-level convenience alias to the cached singleton — mirrors how
# ``api.core.config`` exposes both ``get_settings()`` and ``settings``.
event_bus = get_event_bus()
