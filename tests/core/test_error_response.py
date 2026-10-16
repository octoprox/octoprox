# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for what an error answer tells the client besides its status code."""

import pytest

from api.core.error_response import (
    MAX_REASON_DETAIL_CHARS,
    proxy_status,
    reason_phrase,
    refusal_message,
    upstream_proxy_status,
)
from api.core.failure_log import FailureKind, RequestFailure


def _failure(kind: FailureKind, upstream_status: int | None = None) -> RequestFailure:
    return RequestFailure(project_id="p", kind=kind, status=502, message="m", upstream_status=upstream_status)


class TestReasonPhrase:
    def test_the_detail_follows_the_phrase(self) -> None:
        assert reason_phrase("Bad Gateway", "Exit country mismatch") == "Bad Gateway (Exit country mismatch)"

    def test_no_detail_is_the_phrase_alone(self) -> None:
        assert reason_phrase("Bad Request") == "Bad Request"
        assert reason_phrase("Bad Request", " \r\n") == "Bad Request"

    def test_a_line_break_cannot_end_the_status_line(self) -> None:
        phrase = reason_phrase("Bad Gateway", "429 Too Many\r\nSet-Cookie: x=1\n\tRequests")
        assert phrase == "Bad Gateway (429 Too Many Set-Cookie: x=1 Requests)"

    def test_the_detail_is_folded_to_ascii(self) -> None:
        assert reason_phrase("Bad Gateway", "city são paulo, 東京") == "Bad Gateway (city sao paulo,)"

    def test_a_long_detail_is_cut(self) -> None:
        phrase = reason_phrase("Bad Gateway", "x" * 1000)
        assert phrase == f"Bad Gateway ({'x' * (MAX_REASON_DETAIL_CHARS - 3)}...)"


class TestProxyStatus:
    def test_the_error_type_and_the_next_hops_status(self) -> None:
        assert proxy_status("connection_refused", 429) == "octoprox; error=connection_refused; received-status=429"
        assert proxy_status("connection_timeout") == "octoprox; error=connection_timeout"

    @pytest.mark.parametrize(("kind", "upstream_status", "expected"), [
        (FailureKind.UPSTREAM_REJECTED, 429, "octoprox; error=connection_refused; received-status=429"),
        (FailureKind.UPSTREAM_REJECTED, None, "octoprox; error=connection_refused"),
        (FailureKind.UPSTREAM_TIMEOUT, None, "octoprox; error=connection_timeout"),
        (FailureKind.UPSTREAM_UNREACHABLE, None, "octoprox; error=destination_unavailable"),
        (FailureKind.UPSTREAM_ERROR, None, "octoprox; error=http_protocol_error"),
    ])
    def test_each_upstream_failure_is_named(
        self, kind: FailureKind, upstream_status: int | None, expected: str
    ) -> None:
        assert upstream_proxy_status(_failure(kind, upstream_status)) == expected

    def test_a_client_that_went_away_is_not_named(self) -> None:
        assert upstream_proxy_status(_failure(FailureKind.CLIENT_CLOSED)) is None


def test_a_refusal_reads_as_the_vendors_answer() -> None:
    assert refusal_message("429 Too Many Requests") == "Upstream proxy refused the tunnel: 429 Too Many Requests"
    assert refusal_message("") == "Upstream proxy refused the tunnel"
