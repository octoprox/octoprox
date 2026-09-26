# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for TrafficConfig, validate_traffic_config and the period arithmetic."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from api.core.traffic_limiter import period_bounds, usage_window
from api.models.connector import (
    BYTES_PER_GB,
    Connector,
    TrafficConfig,
    TrafficLimitAction,
    TrafficPeriod,
    validate_traffic_config,
)


class TestTrafficConfig:
    def test_defaults_meter_without_limiting(self) -> None:
        config = TrafficConfig()
        assert config.limit_bytes is None
        assert config.limit_enabled is False
        assert config.period == TrafficPeriod.MONTH
        assert config.reset_day == 1
        assert config.action == TrafficLimitAction.ALERT
        assert config.warn_percent == 80
        assert config.limit_status == 509
        assert config.price_per_gb is None
        assert config.currency == "USD"

    def test_warn_bytes_is_a_share_of_the_limit(self) -> None:
        config = TrafficConfig(limit_bytes=1000, warn_percent=75)
        assert config.warn_bytes == 750
        assert TrafficConfig().warn_bytes is None

    def test_cost_at_decimal_gigabytes(self) -> None:
        config = TrafficConfig(price_per_gb=8.0)
        assert config.cost_of(BYTES_PER_GB) == 8.0
        assert config.cost_of(BYTES_PER_GB // 2) == 4.0
        assert TrafficConfig().cost_of(BYTES_PER_GB) is None

    def test_limit_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            TrafficConfig(limit_bytes=0)

    def test_reset_day_capped_at_28_for_months(self) -> None:
        with pytest.raises(ValidationError):
            TrafficConfig(reset_day=29)
        assert TrafficConfig(reset_day=28).reset_day == 28

    def test_weekly_reset_day_is_a_weekday(self) -> None:
        with pytest.raises(ValidationError, match="Monday"):
            TrafficConfig(period="week", reset_day=8)
        assert TrafficConfig(period="week", reset_day=7).reset_day == 7

    def test_limit_status_is_429_or_509(self) -> None:
        with pytest.raises(ValidationError, match="limit_status"):
            TrafficConfig(limit_status=402)
        assert TrafficConfig(limit_status=429).limit_status == 429

    def test_currency_normalised_and_checked(self) -> None:
        assert TrafficConfig(currency=" eur ").currency == "EUR"
        with pytest.raises(ValidationError, match="ISO 4217"):
            TrafficConfig(currency="euro")

    def test_price_cannot_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            TrafficConfig(price_per_gb=-1)


class TestValidateTrafficConfig:
    def test_empty_stays_empty(self) -> None:
        assert validate_traffic_config({}) == {}

    def test_defaults_are_not_stored(self) -> None:
        assert validate_traffic_config({"period": "month", "reset_day": 1, "action": "alert"}) == {}

    def test_explicit_choices_are_kept_as_json_values(self) -> None:
        stored = validate_traffic_config({
            "limit_bytes": 5 * BYTES_PER_GB, "period": "day", "action": "block",
            "price_per_gb": 2.5, "currency": "gbp",
        })
        assert stored == {
            "limit_bytes": 5 * BYTES_PER_GB, "period": "day", "action": "block",
            "price_per_gb": 2.5, "currency": "GBP",
        }

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValidationError):
            validate_traffic_config({"limit_bytes": -5})


class TestParsedTrafficConfig:
    def _connector(self, traffic_config: dict[str, object]) -> Connector:
        return Connector(
            name="c", credential_id="cred", credential_type="static_proxy_provider",
            project_id="p", traffic_config=traffic_config,
        )

    def test_unset_reads_as_defaults(self) -> None:
        assert self._connector({}).parsed_traffic_config == TrafficConfig()

    def test_stored_choices_are_typed(self) -> None:
        config = self._connector({"limit_bytes": 10, "action": "interrupt"}).parsed_traffic_config
        assert config.limit_bytes == 10
        assert config.action == TrafficLimitAction.INTERRUPT

    def test_broken_stored_json_reads_as_defaults(self) -> None:
        assert self._connector({"limit_bytes": "lots"}).parsed_traffic_config == TrafficConfig()


class TestPeriodBounds:
    def test_day(self) -> None:
        start, end = period_bounds(TrafficConfig(period="day"), datetime(2026, 9, 26, 15, 30))
        assert (start, end) == (datetime(2026, 9, 26), datetime(2026, 9, 27))

    def test_week_starts_on_reset_weekday(self) -> None:
        # 2026-09-26 is a Saturday; weeks reset on Monday by default.
        start, end = period_bounds(TrafficConfig(period="week"), datetime(2026, 9, 26, 15, 30))
        assert (start, end) == (datetime(2026, 9, 21), datetime(2026, 9, 28))
        # Reset on Saturday: today is the first day.
        start, end = period_bounds(TrafficConfig(period="week", reset_day=6), datetime(2026, 9, 26, 15, 30))
        assert (start, end) == (datetime(2026, 9, 26), datetime(2026, 10, 3))
        # Reset on Sunday: the period started six days ago.
        start, end = period_bounds(TrafficConfig(period="week", reset_day=7), datetime(2026, 9, 26))
        assert (start, end) == (datetime(2026, 9, 20), datetime(2026, 9, 27))

    def test_month_from_reset_day(self) -> None:
        config = TrafficConfig(period="month", reset_day=15)
        assert period_bounds(config, datetime(2026, 9, 26)) == (datetime(2026, 9, 15), datetime(2026, 10, 15))
        assert period_bounds(config, datetime(2026, 9, 3)) == (datetime(2026, 8, 15), datetime(2026, 9, 15))
        assert period_bounds(config, datetime(2026, 9, 15)) == (datetime(2026, 9, 15), datetime(2026, 10, 15))

    def test_month_across_year_end(self) -> None:
        config = TrafficConfig(period="month", reset_day=1)
        assert period_bounds(config, datetime(2026, 12, 31)) == (datetime(2026, 12, 1), datetime(2027, 1, 1))
        assert period_bounds(config, datetime(2027, 1, 1)) == (datetime(2027, 1, 1), datetime(2027, 2, 1))

    def test_month_reset_day_28_survives_february(self) -> None:
        config = TrafficConfig(period="month", reset_day=28)
        assert period_bounds(config, datetime(2027, 2, 10)) == (datetime(2027, 1, 28), datetime(2027, 2, 28))
        assert period_bounds(config, datetime(2027, 2, 28)) == (datetime(2027, 2, 28), datetime(2027, 3, 28))


class TestUsageWindow:
    def test_reset_inside_the_period_moves_the_start(self) -> None:
        config = TrafficConfig(period="month")
        reset_at = datetime(2026, 9, 10, 12)
        assert usage_window(config, datetime(2026, 9, 26), reset_at) == (reset_at, datetime(2026, 10, 1))

    def test_reset_before_the_period_is_ignored(self) -> None:
        config = TrafficConfig(period="month")
        assert usage_window(config, datetime(2026, 9, 26), datetime(2026, 8, 10)) == (
            datetime(2026, 9, 1), datetime(2026, 10, 1)
        )

    def test_no_reset(self) -> None:
        config = TrafficConfig(period="day")
        assert usage_window(config, datetime(2026, 9, 26, 5), None) == (datetime(2026, 9, 26), datetime(2026, 9, 27))
