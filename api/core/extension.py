# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The extension seam: the free edition, and the base an extension package subclasses to add to the api.

An installed package names its subclass under the ``octoprox.extensions`` entry point group; an api process loads it at
start, and a package that cannot load leaves the process on the free edition (see docs/architecture.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, ClassVar

import structlog
from pydantic import BaseModel

from api.core.audit import AuditLog
from api.core.logging import error_text

if TYPE_CHECKING:
    from fastapi import APIRouter
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.core.config import Settings

logger = structlog.get_logger()

ENTRY_POINT_GROUP = "octoprox.extensions"


class Edition(BaseModel):
    """The edition an install runs, as ``GET /api/v1/edition`` answers it for the web app."""

    name: str
    # Who the edition is licensed to, when an extension says.
    organisation: str | None = None


class Extension:
    """The free edition: the plain audit log, no routes of its own, and the name ``free``.

    A subclass in an installed package may record more in its audit log, add routers under ``/api/v1``, and describe
    its edition.
    """

    name: ClassVar[str] = "free"

    @classmethod
    def installed(cls) -> Extension:
        """The extension the one ``octoprox.extensions`` entry point names, or the free edition when there is none.

        A package that fails to load, names no ``Extension``, or comes with another one is logged as an error, and the
        process runs the free edition rather than not at all.
        """
        found = list(entry_points(group=ENTRY_POINT_GROUP))
        if not found:
            return Extension()
        if len(found) > 1:
            logger.error(
                "More than one extension is installed; running the free edition",
                entry_points=sorted(f"{entry.name} = {entry.value}" for entry in found),
            )
            return Extension()
        [entry] = found
        try:
            target = entry.load()
            if not (isinstance(target, type) and issubclass(target, Extension)):
                logger.error(
                    "Extension entry point names no Extension; running the free edition",
                    entry_point=f"{entry.name} = {entry.value}",
                )
                return Extension()
            return target()
        except Exception as exc:  # a broken extension must not take the server down
            logger.error(
                "Extension failed to load; running the free edition",
                entry_point=f"{entry.name} = {entry.value}",
                error=error_text(exc),
            )
            return Extension()

    def audit_log(self, settings: Settings) -> AuditLog:
        """The audit log this api process records into."""
        return AuditLog()

    def routers(self) -> Sequence[APIRouter]:
        """Routers served under ``/api/v1``, each declaring its own dependencies."""
        return ()

    async def edition(self, session: AsyncSession) -> Edition:
        """The edition as ``GET /api/v1/edition`` answers it."""
        return Edition(name=self.name)
