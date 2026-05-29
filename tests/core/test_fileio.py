# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Tests for atomic file writes."""

import os
import stat
from pathlib import Path

import pytest

from api.core.fileio import atomic_write


def test_writes_file_with_mode_and_no_leftovers(tmp_path: Path) -> None:
    target = tmp_path / "secret.key"
    atomic_write(target, b"payload", mode=0o600)

    assert target.read_bytes() == b"payload"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [target]  # no .tmp left behind


def test_cleans_up_temp_file_when_a_write_step_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "secret.key"

    def boom(_fd: int) -> None:
        raise OSError("disk full")

    # Fail after the temp file exists and the fd is open, before the rename.
    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="disk full"):
        atomic_write(target, b"payload", mode=0o600)

    # The finally block closed the fd and removed the temp file, and the
    # target was never published — the directory is left clean.
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
