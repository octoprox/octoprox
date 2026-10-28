# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for a request's latency split into steps."""

import pytest

from api.core import timing
from api.core.timing import RequestTimer, Step


class Clock:
    """``time.monotonic`` under the test's control, in seconds."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(timing.time, "monotonic", clock)
    return clock


class TestRequestTimer:
    def test_the_steps_add_up_to_the_latency(self, clock: Clock) -> None:
        timer = RequestTimer()
        clock.now += 0.002
        timer.enter(Step.PREFLIGHT)
        clock.now += 0.5
        timer.enter(Step.CONNECT)
        clock.now += 0.04
        timer.enter(Step.HANDSHAKE)
        clock.now += 0.7

        assert timer.stop() == pytest.approx(1242.0)
        assert timer.steps == pytest.approx({Step.ROUTE: 2.0, Step.PREFLIGHT: 500.0, Step.CONNECT: 40.0, Step.HANDSHAKE: 700.0})
        assert sum(timer.steps.values()) == pytest.approx(timer.stop())

    def test_a_step_never_entered_is_absent(self, clock: Clock) -> None:
        """A request without preflight has no preflight step, rather than one of 0 ms."""
        timer = RequestTimer()
        clock.now += 0.001
        timer.enter(Step.HANDSHAKE)
        clock.now += 0.3
        timer.stop()

        assert set(timer.steps) == {Step.ROUTE, Step.HANDSHAKE}

    def test_stopping_names_the_step_it_stopped_in_and_freezes_the_timer(self, clock: Clock) -> None:
        """A failure is in the step running when it happened, and nothing after it moves the numbers."""
        timer = RequestTimer()
        timer.enter(Step.CONNECT)
        clock.now += 30.0
        latency = timer.stop()

        clock.now += 5.0
        timer.enter(Step.HANDSHAKE)

        assert timer.step is Step.CONNECT
        assert timer.stop() == latency == pytest.approx(30000.0)
        assert set(timer.steps) == {Step.ROUTE, Step.CONNECT}
