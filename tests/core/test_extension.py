# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Loading the extension: the one ``octoprox.extensions`` entry point, or the free edition when it cannot be had.

The entry points are patched; their targets are classes of this module, or a module that does not exist.
"""

from collections.abc import Sequence
from importlib.metadata import EntryPoint
from typing import Any, ClassVar, cast
from unittest.mock import patch

import pytest
import structlog
from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.audit import AuditLog
from api.core.config import Settings
from api.core.extension import ENTRY_POINT_GROUP, Edition, Extension


class GoodExtension(Extension):
    """An extension as a package would ship one."""

    name: ClassVar[str] = "good"

    def routers(self) -> Sequence[APIRouter]:
        return (APIRouter(),)


class NotAnExtension:
    """A class that is no Extension."""


class BrokenExtension(Extension):
    """An extension whose construction fails."""

    def __init__(self) -> None:
        raise RuntimeError("no license store")


def _entry(name: str, target: str) -> EntryPoint:
    return EntryPoint(name=name, value=target, group=ENTRY_POINT_GROUP)


def _installed(*entries: EntryPoint) -> tuple[Extension, list[dict[str, Any]]]:
    """The extension ``installed`` picks among ``entries``, and what it logged."""
    with (
        patch("api.core.extension.entry_points", lambda group: [e for e in entries if e.group == group]),
        structlog.testing.capture_logs() as logs,
    ):
        return Extension.installed(), logs


class TestInstalled:
    def test_without_an_entry_point_the_free_edition_runs(self) -> None:
        extension, logs = _installed()

        assert type(extension) is Extension and extension.name == "free"
        assert logs == []

    def test_the_one_entry_point_names_the_extension(self) -> None:
        extension, logs = _installed(_entry("good", f"{__name__}:GoodExtension"))

        assert type(extension) is GoodExtension and extension.name == "good"
        assert logs == []

    @pytest.mark.parametrize(
        ("target", "event"),
        [
            ("octoprox_nowhere.extension:Missing", "Extension failed to load; running the free edition"),
            (f"{__name__}:NotAnExtension", "Extension entry point names no Extension; running the free edition"),
            (f"{__name__}:BrokenExtension", "Extension failed to load; running the free edition"),
        ],
    )
    def test_one_that_cannot_be_had_leaves_the_free_edition(self, target: str, event: str) -> None:
        extension, logs = _installed(_entry("enterprise", target))

        assert type(extension) is Extension
        assert [(log["event"], log["log_level"], log["entry_point"]) for log in logs] == [
            (event, "error", f"enterprise = {target}")
        ]

    def test_two_entry_points_leave_the_free_edition(self) -> None:
        extension, logs = _installed(
            _entry("good", f"{__name__}:GoodExtension"), _entry("other", f"{__name__}:GoodExtension")
        )

        assert type(extension) is Extension
        assert [(log["event"], log["entry_points"]) for log in logs] == [
            (
                "More than one extension is installed; running the free edition",
                [f"good = {__name__}:GoodExtension", f"other = {__name__}:GoodExtension"],
            )
        ]

    def test_an_entry_point_of_another_group_is_not_an_extension(self) -> None:
        other = EntryPoint(name="acme", value=f"{__name__}:GoodExtension", group="octoprox.providers")

        extension, _logs = _installed(other)

        assert type(extension) is Extension


class TestTheFreeEdition:
    async def test_it_records_into_the_plain_audit_log_adds_no_routes_and_names_itself_free(
        self, test_settings: Settings
    ) -> None:
        extension = Extension()

        assert type(extension.audit_log(test_settings)) is AuditLog
        assert extension.routers() == ()
        assert await extension.edition(cast(AsyncSession, None)) == Edition(name="free", organisation=None)
