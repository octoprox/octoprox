# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A fake Octoprox API full of made-up demo data, for the site screenshots."""

from .api import handle
from .common import NOW, VERSION

__all__ = ["NOW", "VERSION", "handle"]
