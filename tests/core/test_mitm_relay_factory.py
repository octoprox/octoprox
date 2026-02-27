# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for the MITM relay factory."""

from unittest.mock import MagicMock

import pytest

from api.core.mitm import create_relay
from api.core.mitm.cffi_relay import CffiRelay
from api.core.mitm.plain_relay import PlainRelay
from api.core.mitm.rnet_relay import RnetRelay
from api.models.project import MitmBrowser, MitmEngine, MitmMode, Project


def _make_project(**kwargs: object) -> Project:
    """Create a Project with MITM fields for testing."""
    defaults = {
        "name": "test",
        "username": "user",
        "password": "pass",
        "tls_mitm_mode": MitmMode.OFF,
        "tls_mitm_engine": None,
        "tls_mitm_browser": None,
    }
    defaults.update(kwargs)
    return Project(**defaults)  # type: ignore[arg-type]


class TestCreateRelay:
    """Tests for the create_relay() factory function."""

    def test_plain_mode_returns_plain_relay(self) -> None:
        """Plain mode should return a PlainRelay instance."""
        project = _make_project(tls_mitm_mode=MitmMode.PLAIN)
        reader = MagicMock()
        writer = MagicMock()
        relay = create_relay(project, upstream_reader=reader, upstream_writer=writer, target_host="example.com")
        assert isinstance(relay, PlainRelay)

    def test_plain_mode_requires_upstream(self) -> None:
        """Plain mode without upstream connection should raise ValueError."""
        project = _make_project(tls_mitm_mode=MitmMode.PLAIN)
        with pytest.raises(ValueError, match="upstream_reader"):
            create_relay(project)

    def test_match_ua_cffi_returns_cffi_relay(self) -> None:
        """match_ua with curl_cffi engine should return CffiRelay."""
        project = _make_project(tls_mitm_mode=MitmMode.MATCH_UA, tls_mitm_engine=MitmEngine.CURL_CFFI)
        relay = create_relay(project)
        assert isinstance(relay, CffiRelay)
        assert relay._mode == MitmMode.MATCH_UA

    def test_match_ua_rnet_returns_rnet_relay(self) -> None:
        """match_ua with rnet engine should return RnetRelay."""
        project = _make_project(tls_mitm_mode=MitmMode.MATCH_UA, tls_mitm_engine=MitmEngine.RNET)
        relay = create_relay(project)
        assert isinstance(relay, RnetRelay)
        assert relay._mode == MitmMode.MATCH_UA

    def test_override_ua_cffi_returns_cffi_relay(self) -> None:
        """override_ua with curl_cffi engine should return CffiRelay."""
        project = _make_project(
            tls_mitm_mode=MitmMode.OVERRIDE_UA,
            tls_mitm_engine=MitmEngine.CURL_CFFI,
            tls_mitm_browser=MitmBrowser.FIREFOX,
        )
        relay = create_relay(project)
        assert isinstance(relay, CffiRelay)
        assert relay._mode == MitmMode.OVERRIDE_UA
        assert relay._configured_browser == MitmBrowser.FIREFOX

    def test_override_ua_rnet_returns_rnet_relay(self) -> None:
        """override_ua with rnet engine should return RnetRelay."""
        project = _make_project(
            tls_mitm_mode=MitmMode.OVERRIDE_UA,
            tls_mitm_engine=MitmEngine.RNET,
            tls_mitm_browser=MitmBrowser.SAFARI,
        )
        relay = create_relay(project)
        assert isinstance(relay, RnetRelay)
        assert relay._mode == MitmMode.OVERRIDE_UA
        assert relay._configured_browser == MitmBrowser.SAFARI

    def test_default_engine_is_curl_cffi(self) -> None:
        """When engine is None, should default to curl_cffi."""
        project = _make_project(tls_mitm_mode=MitmMode.MATCH_UA, tls_mitm_engine=None)
        relay = create_relay(project)
        assert isinstance(relay, CffiRelay)

    def test_unknown_engine_rejected_by_model(self) -> None:
        """Unknown engine should be rejected by Pydantic validation."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _make_project(tls_mitm_mode=MitmMode.MATCH_UA, tls_mitm_engine="unknown")
