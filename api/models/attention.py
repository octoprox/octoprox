# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""What the Needs attention endpoints answer: a project's lines with their mutes, every project's problem count, and the
install-wide lines of the Projects page; see docs/api.md (Needs attention)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Severity(StrEnum):
    """How bad a line is: requests fail now, or they are degraded or about to be."""

    PROBLEM = "problem"
    WARNING = "warning"


class MuteDuration(StrEnum):
    """How long a mute lasts: a day, a week, 30 days, or until its line is gone."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    UNTIL_CLEAR = "until_clear"


class AttentionWhen(BaseModel):
    """The one time a line shows: its last failure, its last failed sync, or when its traffic period resets."""

    kind: Literal["last_at", "last_failed", "resets"]
    at: datetime


class AttentionMute(BaseModel):
    """A line's stored mute, as long as it lasts."""

    severity: Severity = Field(description="The line's severity when it was muted")
    duration: MuteDuration
    until: datetime | None = Field(description="The end of a timed mute; null until the line clears")
    muted_by: str = Field(description="The username of who muted it")
    muted_at: datetime
    active: bool = Field(description="Whether it hides the line now: false while the line is worse than when muted")


class AttentionLine(BaseModel):
    """One thing that needs attention in a project; the sentence is ``lead + subject + text``."""

    key: str = Field(description="<kind>, or <kind>:<subject id> for a connector, failure kind or site; stable")
    kind: str
    severity: Severity
    lead: str = Field(description="Text before the subject, usually empty")
    subject: str = Field(description="What the line is about, such as a connector's name; may be empty")
    text: str = Field(description="Text after the subject")
    detail: str | None = Field(description="Extra plain words, such as the vendor's status and the counts")
    when: AttentionWhen | None
    link: str = Field(description="The page that fixes it, relative to /projects/{project_id}/")
    action: str = Field(description="The link's label")
    mute: AttentionMute | None = Field(description="Its mute; null when none or a timed one has ended")


class ProjectAttention(BaseModel):
    """A project's lines, muted or not, in display order, and how many of each kind show."""

    generated_at: datetime
    lines: list[AttentionLine]
    problems: int = Field(description="Problem lines whose mute is not active")
    warnings: int = Field(description="Warning lines whose mute is not active")
    muted: int = Field(description="Lines whose mute is active")


class ProjectProblems(BaseModel):
    """One project's problem lines whose mute is not active."""

    project_id: str
    problems: int


class ProjectsAttention(BaseModel):
    """Every project's problem count, oldest project first; a project pending deletion is left out."""

    projects: list[ProjectProblems]


class MuteRequest(BaseModel):
    """Mute one line of a project for everyone in it."""

    key: str = Field(min_length=1, max_length=300, description="The line's key")
    duration: MuteDuration


class InstallAttentionLine(BaseModel):
    """One install-wide line of the Projects page, in plain words with no deployment detail."""

    key: str
    severity: Severity
    text: str = Field(description="May hold {time}, which a client replaces with the local time of at")
    at: datetime | None
    admin_page: Literal["system", "geo"] = Field(description="Where an admin sees more")


class InstallAttention(BaseModel):
    """The install-wide lines, the same for every role."""

    generated_at: datetime
    lines: list[InstallAttentionLine]
