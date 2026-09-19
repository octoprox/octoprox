# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Octoprox - A dynamic and flexible proxy manager."""

from importlib import metadata

try:
    # Single source of truth: the version declared in pyproject.toml, as
    # recorded by the install. Editable installs cache it at install time, so
    # after bumping pyproject re-run `pip install -e . --no-deps` to refresh.
    __version__ = metadata.version("octoprox")
except metadata.PackageNotFoundError:  # running from a source tree, not installed
    __version__ = "unknown"
