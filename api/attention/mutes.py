# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Mutes: the lines of a project's Needs attention card its editors silenced, shared by everyone in the project.

A mute is stored on the project by line key with the severity it was muted at. A timed one stops applying when it ends
and is pruned on the next mute write; one until the line clears ends when the line is gone. A line worse than when it
was muted shows again while it stays worse.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from api.models.attention import AttentionMute, MuteDuration, Severity

if TYPE_CHECKING:
    from api.attention.checks import Finding
    from api.core.audit import Actor

SEVERITY_RANK = {Severity.WARNING: 0, Severity.PROBLEM: 1}
DURATIONS: dict[MuteDuration, timedelta | None] = {
    MuteDuration.DAY: timedelta(days=1),
    MuteDuration.WEEK: timedelta(days=7),
    MuteDuration.MONTH: timedelta(days=30),
    MuteDuration.UNTIL_CLEAR: None,
}


@dataclass(frozen=True)
class _Mute:
    """One stored mute, read."""

    severity: Severity
    duration: MuteDuration
    # None until the line clears.
    until: datetime | None
    by: str
    at: datetime

    @classmethod
    def read(cls, stored: Any) -> _Mute | None:
        """A stored mute; None for one this release cannot read, which hides nothing."""
        try:
            until = stored.get("until")
            return cls(
                severity=Severity(stored["severity"]),
                duration=MuteDuration(stored["duration"]),
                until=datetime.fromisoformat(until) if until else None,
                by=str(stored.get("by") or ""),
                at=datetime.fromisoformat(stored["at"]),
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return None


class MuteBook:
    """A project's stored mutes, read at one moment: which line each hides, and which have ended."""

    def __init__(self, stored: Mapping[str, Any], now: datetime) -> None:
        self._stored = dict(stored)
        self._now = now
        self._mutes = {key: mute for key, value in self._stored.items() if (mute := _Mute.read(value)) is not None}

    def mute_of(self, finding: Finding) -> AttentionMute | None:
        """The finding's mute; None when there is none or its time is over. Active unless the line got worse."""
        mute = self._in_force(finding.key)
        if mute is None:
            return None
        return AttentionMute(
            severity=mute.severity,
            duration=mute.duration,
            until=mute.until,
            muted_by=mute.by,
            muted_at=mute.at,
            active=SEVERITY_RANK[finding.severity] <= SEVERITY_RANK[mute.severity],
        )

    def holds(self, key: str) -> bool:
        """Whether ``key`` has a mute in force."""
        return self._in_force(key) is not None

    def cleared(self, present: Collection[str]) -> list[str]:
        """The until-clear mutes whose line is gone."""
        return sorted(key for key, mute in self._mutes.items() if mute.until is None and key not in present)

    def written(self, key: str, finding: Finding, duration: MuteDuration, actor: Actor) -> dict[str, Any]:
        """The stored mutes with ``key`` muted now, ended timed mutes dropped."""
        length = DURATIONS[duration]
        kept = self._kept()
        kept[key] = {
            "severity": finding.severity.value,
            "duration": duration.value,
            "until": (self._now + length).isoformat() if length is not None else None,
            "by": actor.name,
            "by_id": actor.id,
            "at": self._now.isoformat(),
            "text": finding.sentence,
        }
        return kept

    def without(self, key: str) -> dict[str, Any]:
        """The stored mutes with ``key`` unmuted, ended timed mutes dropped."""
        kept = self._kept()
        kept.pop(key, None)
        return kept

    def text_of(self, key: str) -> str:
        """The line's sentence when it was muted, as History names it; empty when not stored."""
        stored = self._stored.get(key)
        return str(stored.get("text") or "") if isinstance(stored, Mapping) else ""

    def _in_force(self, key: str) -> _Mute | None:
        """The key's mute while it applies: until its line clears, or until its end."""
        mute = self._mutes.get(key)
        if mute is None or (mute.until is not None and mute.until <= self._now):
            return None
        return mute

    def _kept(self) -> dict[str, Any]:
        """The stored mutes but the timed ones that have ended; one this release cannot read is kept as stored."""
        return {
            key: value
            for key, value in self._stored.items()
            if (mute := self._mutes.get(key)) is None or mute.until is None or mute.until > self._now
        }
