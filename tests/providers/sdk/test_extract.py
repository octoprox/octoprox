# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for JMESPath extraction and value mapping."""

import pytest

from api.providers.sdk.descriptor import MapRule, ValueExpr
from api.providers.sdk.extract import ExtractionError, ValueExtractor


@pytest.fixture
def extractor() -> ValueExtractor:
    return ValueExtractor()


def test_search_and_root(extractor: ValueExtractor) -> None:
    doc = {"a": {"b": [1, 2, 3]}, "name": "zone"}
    assert extractor.search("a.b[1]", doc) == 2
    assert extractor.search("@", doc) is doc
    assert extractor.extract_str("name", doc) == "zone"
    assert extractor.extract_str("a.b[0]", doc) == "1"
    assert extractor.extract_str("missing", doc) is None


def test_extract_list_and_truthy(extractor: ValueExtractor) -> None:
    doc = {"results": [{"id": 1}, {"id": 2}], "empty": [], "flag": False}
    assert extractor.extract_list("results", doc) == [{"id": 1}, {"id": 2}]
    assert extractor.extract_list("missing", doc) == []
    with pytest.raises(ExtractionError):
        extractor.extract_list("flag", doc)
    assert extractor.truthy(None, doc)
    assert extractor.truthy("results", doc)
    assert not extractor.truthy("empty", doc)
    assert not extractor.truthy("flag", doc)
    assert extractor.truthy("results[0].id == `1`", doc)


def test_value_mapping(extractor: ValueExtractor) -> None:
    expr = ValueExpr(
        path="type",
        map=[
            MapRule(equals="res_static", to="isp"),
            MapRule(starts_with="res", to="residential"),
            MapRule(regex="^dc", to="datacenter"),
        ],
        default="unknown",
    )
    assert extractor.extract(expr, {"type": "res_static"}) == "isp"
    assert extractor.extract(expr, {"type": "res_rotating"}) == "residential"
    assert extractor.extract(expr, {"type": "dc_shared"}) == "datacenter"
    assert extractor.extract(expr, {"type": "mobile"}) == "unknown"
    assert extractor.extract(ValueExpr(path="missing", default="d"), {}) == "d"


def test_invalid_expression_raises(extractor: ValueExtractor) -> None:
    with pytest.raises(ExtractionError):
        extractor.search("foo[", {})
    with pytest.raises(ExtractionError):
        extractor.validate_expression("]]")
    extractor.validate_expression("@")


def test_map_rule_requires_exactly_one_matcher() -> None:
    with pytest.raises(ValueError):
        MapRule(to="x")
    with pytest.raises(ValueError):
        MapRule(equals="a", starts_with="b", to="x")
