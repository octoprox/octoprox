# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The rule a project weighs its dynamic connectors at a place by: each judged one's success beside the best one's, to
the power Strength - 1, never under the Lowest factor; at strength 1 the trim routing always made, max(floor, success).
Connectors with IPs and dynamic ones not judged are left as they are."""

import pytest

from api.models.project import Project
from api.reputation.weight import ConnectorReputation, ReputationRule

# Blocked 12% and 1.3% of the time.
A, B = 0.88, 0.987


def judged(success: float, place: str = "US") -> ConnectorReputation:
    """A dynamic connector with a factor of ``success`` at ``place``."""
    return ConnectorReputation.by_block_rate({place: success}, place)


NOT_JUDGED = ConnectorReputation.by_block_rate({}, "US")


def values(rule: ReputationRule, reputations: dict[str, ConnectorReputation]) -> dict[str, float]:
    return {connector_id: reputation.value for connector_id, reputation in rule.apply(reputations).items()}


@pytest.mark.parametrize("success", [1.0, 0.987, 0.88, 0.5, 0.1, 0.05, 0.005])
def test_strength_1_is_the_plain_trim_whatever_the_best(success: float) -> None:
    rule = ReputationRule()

    rated = values(rule, {"a": judged(success), "b": judged(1.0), "c": NOT_JUDGED})

    assert rated == {"a": max(0.1, success), "b": 1.0, "c": 1.0}


def test_strength_10_weighs_a_connector_against_the_best() -> None:
    rated = ReputationRule(strength=10).apply({"a": judged(A), "b": judged(B)})

    assert rated["a"].value == pytest.approx(A * (A / B) ** 9)
    assert rated["a"].value == pytest.approx(0.31, abs=0.005)
    # The best keeps its own success.
    assert rated["b"].value == B
    assert (rated["a"].success, rated["a"].best, rated["a"].best_connector_id) == (A, B, "b")
    assert (rated["b"].success, rated["b"].best, rated["b"].best_connector_id) == (B, B, "b")


def test_a_connector_not_judged_keeps_its_weight_and_is_never_the_best() -> None:
    rated = ReputationRule(strength=10).apply({"a": judged(A), "b": NOT_JUDGED})

    # Alone among the judged, it is the best.
    assert (rated["a"].value, rated["a"].best_connector_id) == (A, "a")
    assert rated["b"] == NOT_JUDGED and rated["b"].success is None


def test_connectors_with_ips_are_left_as_they_are() -> None:
    ips = ConnectorReputation.by_usable_ips(1, 4)

    rated = ReputationRule(strength=10, floor=0.5).apply({"a": judged(A), "b": judged(B), "pool": ips})

    assert rated["pool"] == ips and rated["pool"].value == 0.25


def test_never_under_the_lowest_factor() -> None:
    rated = values(ReputationRule(strength=20, floor=0.2), {"a": judged(0.5), "b": judged(1.0)})

    assert rated == {"a": 0.2, "b": 1.0}


def test_a_tie_for_the_best_goes_to_the_lowest_connector_id() -> None:
    rated = ReputationRule(strength=5).apply({"z": judged(0.9), "m": judged(0.9), "a": judged(0.5)})

    assert {reputation.best_connector_id for reputation in rated.values()} == {"m"}
    assert rated["z"].value == rated["m"].value == 0.9


def test_the_level_a_factor_came_from_is_kept() -> None:
    rated = ReputationRule(strength=10).apply({"a": judged(A, "US"), "b": judged(B, "")})

    assert (rated["a"].place, rated["b"].place) == ("US", "")


def test_a_project_s_settings_make_its_rule() -> None:
    project = Project(name="P", username="u", password="p", exit_reputation_strength=7, exit_reputation_floor=25)

    assert ReputationRule.of(project) == ReputationRule(strength=7, floor=0.25)
    assert ReputationRule.of(Project(name="P", username="u", password="p")) == ReputationRule(strength=1, floor=0.1)
