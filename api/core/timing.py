# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A request's latency split into the steps it went through, so time spent here can be told from the vendor's."""

from __future__ import annotations

import time
from enum import StrEnum


class Step(StrEnum):
    """A part of a request's latency, in the order a request goes through them; a request skips the ones it has none of."""

    # Choosing the proxy: in memory, but sticky reads its Redis binding and a -cc- request may wait for an on-demand group.
    ROUTE = "route"
    # Checking where the proxy exits, only for a request preflight applies to: a cached verdict, or an echo through it.
    PREFLIGHT = "preflight"
    # Opening the connection to the vendor's gateway. SOCKS has none: python-socks does it and the handshake in one call.
    CONNECT = "connect"
    # The vendor's answer to the tunnel request: it picks an exit and reaches the target.
    HANDSHAKE = "handshake"
    # A plain HTTP request only: from sending the request upstream to its status line.
    RESPONSE = "response"


class RequestTimer:
    """One request's latency as it is spent: entering a step ends the one before, so the steps add up to the latency.

    Starts in ``Step.ROUTE``, at the moment Octoprox takes the request up.
    """

    __slots__ = ("_ended", "_entered", "started", "step", "steps")

    def __init__(self) -> None:
        self.started = time.monotonic()
        self._entered = self.started
        self._ended: float | None = None
        # The step running, or the last one once stopped: the one a failure happened in.
        self.step = Step.ROUTE
        # Milliseconds spent in each step entered so far.
        self.steps: dict[Step, float] = {}

    def enter(self, step: Step) -> None:
        """End the running step and start ``step``; nothing once stopped."""
        if self._ended is None:
            self._close(time.monotonic())
            self.step = step

    def stop(self) -> float:
        """End the running step; the latency in milliseconds, the same on every later call."""
        if self._ended is None:
            self._ended = time.monotonic()
            self._close(self._ended)
        return (self._ended - self.started) * 1000

    def _close(self, now: float) -> None:
        self.steps[self.step] = self.steps.get(self.step, 0.0) + (now - self._entered) * 1000
        self._entered = now
