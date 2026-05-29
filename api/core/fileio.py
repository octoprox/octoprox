# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Small filesystem helpers."""

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    """Write *data* to *path* atomically with the given permission bits.

    Writes to a temp file in the same directory, fsyncs it, then renames it
    over the target, so a concurrent reader sees either the old file or the
    complete new one — never a half-written file. The temp file is cleaned up
    if any step before the rename fails.
    """
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, path)
        tmp = ""
    finally:
        if fd != -1:
            os.close(fd)
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
