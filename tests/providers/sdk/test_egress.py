# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the outbound request policy."""

import pytest

from api.providers.sdk.egress import EgressDeniedError, EgressGuard, EgressPolicy, is_public_address


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("10.0.0.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("192.168.1.10", False),
        ("172.16.5.5", False),
        ("::1", False),
        ("fe80::1", False),
        ("2606:4700::1111", True),
        ("::ffff:10.0.0.1", False),
        ("not-an-ip", False),
    ],
)
def test_is_public_address(address: str, public: bool) -> None:
    assert is_public_address(address) is public


def test_static_checks_enforce_https_and_public_literals() -> None:
    guard = EgressGuard()
    assert guard.check_static("https://api.example.com/x") == "api.example.com"
    with pytest.raises(EgressDeniedError):
        guard.check_static("http://api.example.com/x")
    with pytest.raises(EgressDeniedError):
        guard.check_static("https://169.254.169.254/latest/meta-data")
    with pytest.raises(EgressDeniedError):
        guard.check_static("https://localhost/admin")
    with pytest.raises(EgressDeniedError):
        guard.check_static("ftp://api.example.com/x")


def test_relaxed_policy_allows_private_and_http() -> None:
    guard = EgressGuard(EgressPolicy(allow_http=True, allow_private=True, pin_dns=False))
    assert guard.check_static("http://127.0.0.1:8000/") == "127.0.0.1"


async def test_resolve_rejects_private_resolution() -> None:
    guard = EgressGuard(EgressPolicy())
    with pytest.raises(EgressDeniedError):
        await guard.resolve("https://127.0.0.1/")


async def test_resolve_pins_literal_address() -> None:
    guard = EgressGuard(EgressPolicy())
    target = await guard.resolve("https://8.8.8.8/dns-query")
    assert target.address == "8.8.8.8"
    assert target.hostname == "8.8.8.8"


async def test_relaxed_policy_skips_lookup() -> None:
    guard = EgressGuard(EgressPolicy(allow_http=True, allow_private=True, pin_dns=False))
    target = await guard.resolve("https://does-not-resolve.invalid/path")
    assert target.hostname == "does-not-resolve.invalid"
    assert str(target.url) == "https://does-not-resolve.invalid/path"
