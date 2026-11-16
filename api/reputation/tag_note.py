# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What a gateway notes about a request a client named with ``-tag-``, for the exit reports that name the request by
its tag."""

from __future__ import annotations

from pydantic import BaseModel

from api.db.redis import TAG_NOTE_KEY


class TagNote(BaseModel):
    """The exit a tagged request went through: its connector and proxy row, and the row's exit IP when its connector has
    IPs; None for a dynamic gateway, whose exit is the request's own, and for a row not seen exiting yet."""

    connector_id: str
    proxy_id: str
    # Canonical text (api.feed.models.static_exit_ip), what routing keeps the row's avoids under.
    ip: str | None = None

    @staticmethod
    def key(project_id: str, tag: str) -> str:
        """Where the note of the project's request tagged ``tag`` is kept."""
        return TAG_NOTE_KEY.format(project_id=project_id, tag=tag)
