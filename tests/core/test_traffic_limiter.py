# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the traffic helpers every role shares, and for the per-transfer meter.

The usage report and the block rule are pure, given a connector's window and the bytes counted in it; the meter reports
to a recording request metering whose interrupt answer the test sets.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from api.core import traffic_limiter as tl
from api.core.traffic_limiter import (
    PROGRESS_REPORT_BYTES,
    ConnectorTraffic,
    TrafficMeter,
)
from api.models.connector import Connector
from api.models.credential import CredentialType
from tests.gateway.doubles import RecordingMetering

MB = 1_000_000
# A Wednesday afternoon, well inside its day, week and month.
NOW = datetime(2026, 10, 14, 15, 30, 0)


def make_connector(traffic_config: dict[str, Any] | None = None, **overrides: Any) -> Connector:
    return Connector(
        name="vendor",
        credential_id="cred",
        credential_type=CredentialType.STATIC_PROXY_PROVIDER,
        project_id="project",
        traffic_config=traffic_config or {},
        **overrides,
    )


def traffic(connector: Connector, sent: int = 0, received: int = 0) -> ConnectorTraffic:
    """The connector's usage window at ``NOW`` with ``sent`` and ``received`` counted in it."""
    return replace(ConnectorTraffic.of(connector, NOW), sent=sent, received=received)


class TestConnectorTraffic:
    def test_both_directions_count_against_the_limit(self) -> None:
        usage = traffic(make_connector({"limit_bytes": 100}), 60, 50).usage(blocked=False, pending=False)
        assert (usage.bytes_sent, usage.bytes_received, usage.total_bytes) == (60, 50, 110)
        assert (usage.status, usage.percent, usage.action.value) == ("exceeded", 110.0, "alert")

    def test_the_warning_line(self) -> None:
        connector = make_connector({"limit_bytes": 100, "warn_percent": 50})
        assert traffic(connector, 49).usage(blocked=False, pending=False).status == "ok"
        assert traffic(connector, 50).usage(blocked=False, pending=False).status == "warning"
        assert traffic(connector, 99, 1).usage(blocked=False, pending=False).status == "exceeded"

    def test_without_a_limit_a_connector_is_only_metered_and_priced(self) -> None:
        usage = traffic(make_connector({"price_per_gb": 3}), 10**12, 20).usage(blocked=False, pending=False)
        assert (usage.limit_bytes, usage.percent, usage.status) == (None, None, "ok")
        assert usage.cost == pytest.approx((10**12 + 20) / 1e9 * 3, rel=1e-6)
        assert usage.currency == "USD"

    def test_period_and_price(self) -> None:
        connector = make_connector({
            "limit_bytes": 10 * MB, "period": "week", "reset_day": 1, "price_per_gb": 2.5, "currency": "EUR",
        })
        usage = traffic(connector, MB, MB).usage(blocked=False, pending=False)
        assert usage.period.value == "week"
        # The week starts on its reset day, a Monday.
        assert (usage.period_start, usage.period_end) == (datetime(2026, 10, 12), datetime(2026, 10, 19))
        assert usage.percent == 20.0
        assert usage.cost == pytest.approx(0.005)
        assert usage.currency == "EUR"

    def test_a_manual_reset_inside_the_period_starts_the_window_there(self) -> None:
        reset_at = NOW - timedelta(hours=1)
        connector = make_connector({"limit_bytes": 100, "action": "block"}, traffic_reset_at=reset_at)
        window = ConnectorTraffic.of(connector, NOW)
        usage = window.usage(blocked=False, pending=False)
        assert window.start == usage.period_start == usage.reset_at == reset_at
        assert usage.total_bytes == 0

    def test_a_reset_from_an_earlier_period_is_not_reported(self) -> None:
        connector = make_connector({"limit_bytes": 100, "period": "day"}, traffic_reset_at=NOW - timedelta(days=2))
        usage = traffic(connector, 10).usage(blocked=False, pending=False)
        assert usage.period_start == datetime(2026, 10, 14)
        assert usage.reset_at is None

    def test_blocked_is_the_callers_word_never_the_totals(self) -> None:
        """The api reports the block the traffic job stored, which a reset or a raised limit leaves until it decides."""
        connector = make_connector({"limit_bytes": 100, "action": "block"})
        over, under = traffic(connector, 100, 100), traffic(connector, 10)
        assert over.block_until() is not None and under.block_until() is None
        assert (over.usage(blocked=False, pending=False).blocked, under.usage(blocked=True, pending=False).blocked) == (False, True)

    def test_pending_is_the_callers_word_beside_the_stored_block(self) -> None:
        """A reset's answer: usage counts from the reset, the stored block stands, and the decision is pending."""
        connector = make_connector({"limit_bytes": 100, "action": "block"}, traffic_reset_at=NOW - timedelta(minutes=1))
        usage = ConnectorTraffic.of(connector, NOW).usage(blocked=True, pending=True)
        assert (usage.total_bytes, usage.status, usage.blocked, usage.pending) == (0, "ok", True, True)
        assert traffic(connector, 10).usage(blocked=True, pending=False).pending is False

    def test_the_traffic_jobs_rule_blocks_to_the_windows_end(self) -> None:
        window = traffic(make_connector({"limit_bytes": 100, "action": "block", "period": "day"}), 100)
        assert window.block_until() == window.end == datetime(2026, 10, 15)
        assert traffic(make_connector({"limit_bytes": 100}), 100).block_until() is None


class TestStatus:
    def test_a_total_against_the_warning_line_and_the_limit(self) -> None:
        connector = make_connector({"limit_bytes": 100, "warn_percent": 50})
        assert [traffic(connector, total).status for total in (49, 50, 99, 100, 101)] == [
            "ok", "warning", "warning", "exceeded", "exceeded",
        ]

    def test_without_a_limit_every_total_is_ok(self) -> None:
        assert traffic(make_connector(), 10**15).status == "ok"


class TestBlockUntil:
    # The end of the monthly window ``NOW`` is in.
    END = datetime(2026, 11, 1)

    def test_no_limit_never_blocks(self) -> None:
        assert traffic(make_connector(), 10**15).block_until() is None

    def test_under_the_limit_nothing_is_blocked(self) -> None:
        assert traffic(make_connector({"limit_bytes": 100, "action": "block"}), 99).block_until() is None

    def test_reaching_the_limit_blocks_to_the_end_of_the_period(self) -> None:
        assert traffic(make_connector({"limit_bytes": 100, "action": "block"}), 100).block_until() == self.END
        assert traffic(make_connector({"limit_bytes": 100, "action": "interrupt"}), 250).block_until() == self.END

    def test_the_alert_action_only_flags(self) -> None:
        assert traffic(make_connector({"limit_bytes": 100}), 250).block_until() is None

    def test_raising_the_limit_releases(self) -> None:
        assert traffic(make_connector({"limit_bytes": 100, "action": "block"}), 200).block_until() == self.END
        assert traffic(make_connector({"limit_bytes": 1000, "action": "block"}), 200).block_until() is None

    def test_switching_to_alert_releases(self) -> None:
        assert traffic(make_connector({"limit_bytes": 100, "action": "alert"}), 200).block_until() is None


class TestTrafficMeter:
    def test_reports_after_a_megabyte(self) -> None:
        owner = RecordingMetering()
        meter = TrafficMeter(owner, "proxy", "project", "connector")
        assert meter.add_sent(PROGRESS_REPORT_BYTES - 1) is True
        assert owner.reports == []
        meter.add_received(1)
        assert owner.reports == [("proxy", "project", "connector", None, PROGRESS_REPORT_BYTES - 1, 1, None)]
        # The reported bytes are counted once: finish returns only what came after.
        meter.add_received(10)
        assert meter.finish() == (0, 10)
        assert meter.finish() == (0, 0)

    def test_reports_after_the_time_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tl, "PROGRESS_REPORT_SECONDS", 0.0)
        owner = RecordingMetering()
        # On behalf of a tunnel device, for a known destination: the owner learns both.
        meter = TrafficMeter(owner, "proxy", "project", "connector", "device-1", "shop.example.com")
        meter.add_sent(5)
        assert owner.reports == [("proxy", "project", "connector", "device-1", 5, 0, "shop.example.com")]

    def test_a_long_transfer_is_reported_while_it_runs(self) -> None:
        owner = RecordingMetering()
        meter = TrafficMeter(owner, "proxy", "project", "connector")
        # Two megabytes down in 64 KiB chunks and a little up: a keep-alive tunnel still open.
        for _ in range(2 * PROGRESS_REPORT_BYTES // 65536):
            assert meter.add_received(65536) is True
        meter.add_sent(5)
        assert owner.reported()[1] >= PROGRESS_REPORT_BYTES
        # Whatever was not reported while it ran comes out of finish, no more.
        rest_sent, rest_received = meter.finish()
        assert owner.reported()[0] + rest_sent == 5
        assert owner.reported()[1] + rest_received == 2 * PROGRESS_REPORT_BYTES
        assert (meter.sent, meter.received) == (5, 2 * PROGRESS_REPORT_BYTES)

    def test_stops_once_its_connector_is_interrupted(self) -> None:
        owner = RecordingMetering()
        owner.status = 429
        meter = TrafficMeter(owner, "proxy", "project", "connector")
        assert meter.add_received(PROGRESS_REPORT_BYTES) is True
        owner.interrupted = True
        assert meter.allowed is False
        # The chunk in hand is still counted; the transfer stops after it.
        assert meter.add_received(1) is False
        assert meter.add_sent(1) is False
        assert meter.limit_status == 429
        assert meter.finish() == (1, 1)

    def test_finishing_ends_the_meter_once(self) -> None:
        ended: list[str] = []
        meter = TrafficMeter(RecordingMetering(), "proxy", "project", "connector", on_finish=lambda: ended.append("x"))
        meter.add_sent(3)
        assert ended == []
        assert meter.finish() == (3, 0)
        assert meter.finish() == (0, 0)
        assert ended == ["x"]

    def test_a_transfer_nothing_will_complete_reports_then_finishes(self) -> None:
        """A client gone mid-tunnel publishes no completion: its bytes go out as progress, then the meter ends."""
        owner = RecordingMetering()
        ended: list[bool] = []
        meter = TrafficMeter(owner, "proxy", "project", "connector", on_finish=lambda: ended.append(True))
        meter.add_received(7)
        meter.report()
        assert owner.reports == [("proxy", "project", "connector", None, 0, 7, None)]
        assert meter.finish() == (0, 0)
        assert ended == [True]
