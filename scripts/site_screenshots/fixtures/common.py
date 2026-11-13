# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The demo clock, and the stable noise and ids the demo data is made from.

Everything is generated relative to NOW, which the driver also pins in the browser, so every
relative time, chart axis and "x min ago" label is the same on every run.
"""

from __future__ import annotations

import base64
import hashlib
import math
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# Thursday afternoon, a little past the daily peak. The browser runs in Europe/London, which is UTC
# in November, so the charts show the hours the data was made for.
NOW = datetime(2026, 11, 12, 17, 30, 20, tzinfo=UTC)
# The release the UI is built as: an API naming another one would put the new-version bar in every shot.
VERSION: str = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
GB = 1_000_000_000


def iso(dt: datetime) -> str:
    """Naive UTC, the way the API serialises timestamps."""
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def ago(**delta: float) -> str:
    """The API timestamp of ``timedelta(**delta)`` before NOW."""
    return iso(NOW - timedelta(**delta))


def unit(key: str, i: float | str = 0) -> float:
    """A stable pseudo-random number in [0, 1) for (key, i)."""
    digest = hashlib.blake2b(f"{key}:{i}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def signed(key: str, i: float | str = 0) -> float:
    """A stable pseudo-random number in [-1, 1)."""
    return unit(key, i) * 2 - 1


def smooth(key: str, minute: float, period: float) -> float:
    """Value noise in [-1, 1]: random knots every ``period`` minutes, cosine-interpolated."""
    k = math.floor(minute / period)
    f = minute / period - k
    w = (1 - math.cos(f * math.pi)) / 2
    return signed(key, k) * (1 - w) + signed(key, k + 1) * w


def minute_index(dt: datetime) -> int:
    return int(dt.timestamp() // 60)


def from_minute(m: int) -> datetime:
    return datetime.fromtimestamp(m * 60, tz=UTC)


def short_id(key: str, n: int = 32) -> str:
    """A stable hex id of ``n`` characters, at most 32."""
    return hashlib.blake2b(key.encode(), digest_size=16).hexdigest()[:n]


def uuid_for(key: str) -> str:
    """A stable id in the shape of a version 4 UUID."""
    h = short_id(key)
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def key_for(key: str) -> str:
    """A stable 32-byte key in base64, the shape of a WireGuard key."""
    return base64.b64encode(hashlib.blake2b(key.encode(), digest_size=32).digest()).decode()
