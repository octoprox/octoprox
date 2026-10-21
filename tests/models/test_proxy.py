# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Proxy model's derived values."""

from api.models.proxy import Proxy, ProxyStatus, metadata_diff


class TestDisplayedHost:
    """Attribution writes the exit it saw into the metadata alone; that is what the UI shows."""

    def test_the_exit_attribution_saw_wins(self) -> None:
        proxy = Proxy(host="pr.vendor.test", port=7777, connector_id="c", display_host="198.51.100.1", metadata={"discovered_ip": "203.0.113.9"})
        assert proxy.effective_display_host == "203.0.113.9"

    def test_then_what_discovery_stored_then_where_it_connects(self) -> None:
        assert Proxy(host="pr.vendor.test", port=1, connector_id="c", display_host="198.51.100.1").effective_display_host == "198.51.100.1"
        assert Proxy(host="pr.vendor.test", port=1, connector_id="c").effective_display_host == "pr.vendor.test"
        assert Proxy(host="pr.vendor.test", port=1, connector_id="c", metadata={"discovered_ip": ""}).effective_display_host == "pr.vendor.test"


class TestRetiring:
    """A proxy draining or terminating is on its way out; in any other status it serves, or may again."""

    def test_only_draining_and_terminating_are_retiring(self) -> None:
        assert [status for status in ProxyStatus if status.retiring] == [ProxyStatus.DRAINING, ProxyStatus.TERMINATING]


class TestMetadataDiff:
    """What a write of a row's metadata changes, key by key, so a writer merges only what it touched."""

    def test_the_keys_set_anew_with_their_values_and_the_keys_dropped(self) -> None:
        before = {"kept": 1, "moved": "a", "gone": True}
        after = {"kept": 1, "moved": "b", "new": [1]}

        assert metadata_diff(before, after) == ({"moved": "b", "new": [1]}, {"gone"})
        assert metadata_diff(before, before) == ({}, set())
