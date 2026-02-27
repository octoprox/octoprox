# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""MITM interception package.

Provides pluggable TLS interception with multiple upstream relay strategies:

- **Plain**: Python's ssl module — debug-friendly, detectable fingerprint.
- **CffiRelay**: curl_cffi — browser-grade TLS fingerprints via libcurl.
- **RnetRelay**: rnet — browser-grade TLS fingerprints via Rust/BoringSSL.

Use ``create_relay()`` to instantiate the correct relay based on project settings.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from api.core.mitm.base import MitmRelay
from api.core.mitm.handler import MitmHandler
from api.models.project import MitmEngine, MitmMode

if TYPE_CHECKING:
    from api.models.project import Project

__all__ = ["MitmHandler", "MitmRelay", "create_relay"]


def create_relay(
    project: Project,
    upstream_reader: asyncio.StreamReader | None = None,
    upstream_writer: asyncio.StreamWriter | None = None,
    target_host: str = "",
) -> MitmRelay:
    """Factory: create the appropriate relay based on project MITM settings.

    Args:
        project: Project with tls_mitm_mode, tls_mitm_engine, tls_mitm_browser.
        upstream_reader: Pre-established upstream connection (required for plain mode).
        upstream_writer: Pre-established upstream connection (required for plain mode).
        target_host: Target hostname (required for plain mode TLS SNI).

    Returns:
        A MitmRelay instance ready to send requests.

    Raises:
        ValueError: If mode/engine combination is invalid or missing required args.
    """
    mode = project.tls_mitm_mode

    if mode == MitmMode.PLAIN:
        if upstream_reader is None or upstream_writer is None:
            msg = "Plain relay requires upstream_reader and upstream_writer"
            raise ValueError(msg)
        from api.core.mitm.plain_relay import PlainRelay

        return PlainRelay(upstream_reader, upstream_writer, target_host)

    # Engine modes: close pre-established upstream (engines make their own connections)
    if upstream_writer is not None:
        upstream_writer.close()

    engine = project.tls_mitm_engine or MitmEngine.CURL_CFFI
    browser = project.tls_mitm_browser

    if engine == MitmEngine.CURL_CFFI:
        from api.core.mitm.cffi_relay import CffiRelay

        return CffiRelay(mode=mode, browser=browser)

    if engine == MitmEngine.RNET:
        from api.core.mitm.rnet_relay import RnetRelay

        return RnetRelay(mode=mode, browser=browser)

    msg = f"Unknown MITM engine: {engine}"
    raise ValueError(msg)
