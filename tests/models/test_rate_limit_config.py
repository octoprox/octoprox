# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for RateLimitConfig model and validate_rate_limit_config."""

import pytest
from pydantic import ValidationError

from api.models.connector import RateLimitConfig, validate_rate_limit_config


class TestRateLimitConfig:
    """Tests for RateLimitConfig Pydantic model."""

    def test_valid_config(self) -> None:
        config = RateLimitConfig(
            max_requests=100,
            window_seconds=60,
            quarantine_seconds_min=30,
            quarantine_seconds_max=300,
        )
        assert config.max_requests == 100
        assert config.window_seconds == 60
        assert config.quarantine_seconds_min == 30
        assert config.quarantine_seconds_max == 300

    def test_min_equals_max_is_valid(self) -> None:
        config = RateLimitConfig(
            max_requests=10,
            window_seconds=10,
            quarantine_seconds_min=60,
            quarantine_seconds_max=60,
        )
        assert config.quarantine_seconds_min == config.quarantine_seconds_max

    def test_min_greater_than_max_raises(self) -> None:
        with pytest.raises(ValidationError, match="quarantine_seconds_min must be <= quarantine_seconds_max"):
            RateLimitConfig(
                max_requests=100,
                window_seconds=60,
                quarantine_seconds_min=300,
                quarantine_seconds_max=60,
            )

    def test_max_requests_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            RateLimitConfig(
                max_requests=0,
                window_seconds=60,
                quarantine_seconds_min=30,
                quarantine_seconds_max=300,
            )

    def test_window_seconds_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            RateLimitConfig(
                max_requests=100,
                window_seconds=0,
                quarantine_seconds_min=30,
                quarantine_seconds_max=300,
            )

    def test_quarantine_min_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            RateLimitConfig(
                max_requests=100,
                window_seconds=60,
                quarantine_seconds_min=0,
                quarantine_seconds_max=300,
            )

    def test_window_seconds_max_limit(self) -> None:
        with pytest.raises(ValidationError):
            RateLimitConfig(
                max_requests=100,
                window_seconds=86401,
                quarantine_seconds_min=30,
                quarantine_seconds_max=300,
            )

    def test_boundary_values(self) -> None:
        config = RateLimitConfig(
            max_requests=1,
            window_seconds=1,
            quarantine_seconds_min=1,
            quarantine_seconds_max=86400,
        )
        assert config.max_requests == 1
        assert config.quarantine_seconds_max == 86400

    def test_sticky_quarantine_defaults_false(self) -> None:
        config = RateLimitConfig(
            max_requests=100,
            window_seconds=60,
            quarantine_seconds_min=30,
            quarantine_seconds_max=300,
        )
        assert config.sticky_quarantine is False

    def test_sticky_quarantine_enabled(self) -> None:
        config = RateLimitConfig(
            max_requests=100,
            window_seconds=60,
            quarantine_seconds_min=30,
            quarantine_seconds_max=300,
            sticky_quarantine=True,
        )
        assert config.sticky_quarantine is True


class TestValidateRateLimitConfig:
    """Tests for validate_rate_limit_config function."""

    def test_empty_config_returns_empty(self) -> None:
        result = validate_rate_limit_config({})
        assert result == {}

    def test_valid_config_returns_all_fields(self) -> None:
        result = validate_rate_limit_config({
            "max_requests": 50,
            "window_seconds": 120,
            "quarantine_seconds_min": 10,
            "quarantine_seconds_max": 60,
        })
        assert result == {
            "max_requests": 50,
            "window_seconds": 120,
            "quarantine_seconds_min": 10,
            "quarantine_seconds_max": 60,
            "sticky_quarantine": False,
        }

    def test_invalid_config_raises(self) -> None:
        with pytest.raises(ValidationError):
            validate_rate_limit_config({
                "max_requests": -1,
                "window_seconds": 60,
                "quarantine_seconds_min": 30,
                "quarantine_seconds_max": 300,
            })

    def test_missing_fields_raises(self) -> None:
        with pytest.raises(ValidationError):
            validate_rate_limit_config({"max_requests": 100})

    def test_min_greater_than_max_raises(self) -> None:
        with pytest.raises(ValidationError, match="quarantine_seconds_min must be <= quarantine_seconds_max"):
            validate_rate_limit_config({
                "max_requests": 100,
                "window_seconds": 60,
                "quarantine_seconds_min": 300,
                "quarantine_seconds_max": 60,
            })
