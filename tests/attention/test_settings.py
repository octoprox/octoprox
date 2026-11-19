# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A project's alert settings: their bounds and the rules between them, what a project stores of them, and how a stored
value this release cannot read falls back to its default."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from api.models.alerts import AlertSettings, AlertSettingsUpdate


def _message(**values: Any) -> str:
    with pytest.raises(ValidationError) as caught:
        AlertSettingsUpdate(**values)
    return str(caught.value.errors()[0]["msg"])


class TestBounds:
    @pytest.mark.parametrize(
        ("field", "low", "high", "beside"),
        [
            # The success pair is set apart enough to let either edge through.
            ("success_warning_below", 1, 100, {"success_problem_below": 0}),
            ("success_problem_below", 0, 99, {"success_warning_below": 100}),
            ("success_min_requests", 1, 1_000_000, {}),
            ("exits_outside_percent", 1, 100, {}),
            ("vendor_min", 1, 1_000_000, {}),
            ("connector_errors_min", 1, 100, {}),
            ("stuck_deleting_minutes", 5, 10_080, {}),
            ("site_blocked_percent", 1, 100, {}),
            ("keep_days", 0, 36_500, {}),
        ],
    )
    def test_each_field_takes_its_range_and_nothing_past_it(
        self, field: str, low: int, high: int, beside: dict[str, int]
    ) -> None:
        assert getattr(AlertSettings(**{field: low}, **beside), field) == low
        assert getattr(AlertSettings(**{field: high}, **beside), field) == high
        for refused in (low - 1, high + 1):
            with pytest.raises(ValidationError):
                AlertSettings(**{field: refused}, **beside)

    def test_a_field_this_release_does_not_know_is_refused(self) -> None:
        assert "Extra inputs are not permitted" in _message(not_a_setting=100)


class TestRules:
    @pytest.mark.parametrize(
        ("warning", "problem"),
        [
            ("refusals_warning_percent", "refusals_problem_percent"),
            ("vendor_rate_limited_warning_percent", "vendor_rate_limited_problem_percent"),
            ("vendor_forbidden_warning_percent", "vendor_forbidden_problem_percent"),
            ("connector_failing_warning_percent", "connector_failing_problem_percent"),
        ],
    )
    def test_a_warning_never_above_its_problem(self, warning: str, problem: str) -> None:
        assert AlertSettings(**{warning: 40, problem: 40})
        assert _message(**{warning: 41, problem: 40}) == f"Value error, {warning} cannot be above {problem}"

    def test_the_success_problem_below_the_warning(self) -> None:
        assert AlertSettings(success_warning_below=60, success_problem_below=59)
        assert _message(success_warning_below=60, success_problem_below=60) == (
            "Value error, success_problem_below must be below success_warning_below"
        )


class TestStored:
    def test_only_what_differs_from_the_defaults(self) -> None:
        assert AlertSettings().stored() == {}
        sent = AlertSettingsUpdate(success_warning_below=80, vendor_min=20, keep_days=0)

        assert AlertSettings.model_validate(sent.model_dump()).stored() == {"vendor_min": 20, "keep_days": 0}

    def test_read_back_as_stored(self) -> None:
        stored = AlertSettings(refusals_min=3, success_rate=False).stored()

        assert AlertSettings.read(stored) == AlertSettings(refusals_min=3, success_rate=False)


class TestRead:
    def test_a_bad_value_falls_back_to_its_default_and_keeps_the_rest(self) -> None:
        read = AlertSettings.read({"vendor_min": "many", "refusals_min": 7, "stuck_deleting_minutes": 1})

        assert (read.vendor_min, read.refusals_min, read.stuck_deleting_minutes) == (10, 7, 30)

    def test_a_key_this_release_does_not_know_is_ignored(self) -> None:
        assert AlertSettings.read({"not_a_setting": 5, "unhealthy_min": 9}) == AlertSettings(unhealthy_min=9)

    def test_an_inconsistent_pair_is_the_defaults(self) -> None:
        read = AlertSettings.read({"refusals_warning_percent": 50, "refusals_problem_percent": 40, "vendor_min": 3})

        assert read == AlertSettings()

    def test_a_value_readable_alone_is_kept_when_its_pair_is_stored_beside_it(self) -> None:
        """A warning above the default problem is fine beside its own stored problem."""
        read = AlertSettings.read({"refusals_warning_percent": 30, "refusals_problem_percent": 40, "vendor_min": "x"})

        assert (read.refusals_warning_percent, read.refusals_problem_percent, read.vendor_min) == (30, 40, 10)
