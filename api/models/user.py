# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""User model definitions for authentication and authorization."""

import re
from datetime import datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from api.core import utc_now


class UserRole(str, Enum):
    """User roles for access control."""

    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class User(BaseModel):
    """Domain model for a user."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    username: str
    email: str = ""
    password_hash: str | None = None
    role: UserRole = UserRole.VIEWER
    is_active: bool = True
    invite_token: str | None = None
    invite_token_expires_at: datetime | None = None
    theme_preference: str = "light"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


_EMAIL_RE = re.compile(r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}$")


def _validate_email(v: str) -> str:
    """Validate email format. Empty string is allowed (email is optional)."""
    v = v.strip()
    if v and not _EMAIL_RE.fullmatch(v):
        raise ValueError("Invalid email address")
    return v


# Keep in sync with THEMES in web/src/themes/index.ts
ALLOWED_THEMES = frozenset(
    {
        "light",
        "dark",
        "solarized-light",
        "solarized-dark",
        "dracula",
        "nord",
        "high-contrast",
    }
)


def _validate_theme(v: str | None) -> str | None:
    if v is None:
        return v
    if v not in ALLOWED_THEMES:
        raise ValueError(f"Invalid theme: {v}")
    return v


class UserCreate(BaseModel):
    """Schema for creating a new user (admin action)."""

    username: str
    email: str = ""
    password: str
    role: UserRole = UserRole.VIEWER

    @field_validator("email")
    @classmethod
    def check_email(cls, v: str) -> str:
        return _validate_email(v)


class UserUpdate(BaseModel):
    """Schema for updating a user (admin action)."""

    username: str | None = None
    email: str | None = None
    password: str | None = None
    role: UserRole | None = None
    is_active: bool | None = None
    theme_preference: str | None = None

    @field_validator("email")
    @classmethod
    def check_email(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_email(v)

    @field_validator("theme_preference")
    @classmethod
    def check_theme(cls, v: str | None) -> str | None:
        return _validate_theme(v)


class UserSelfUpdate(BaseModel):
    """Schema for a user updating their own profile."""

    email: str | None = None
    password: str | None = None
    current_password: str | None = None
    theme_preference: str | None = None

    @field_validator("email")
    @classmethod
    def check_email(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_email(v)

    @field_validator("theme_preference")
    @classmethod
    def check_theme(cls, v: str | None) -> str | None:
        return _validate_theme(v)


class UserInviteCreate(BaseModel):
    """Schema for creating a user via invite link (no password)."""

    username: str
    email: str = ""
    role: UserRole = UserRole.VIEWER

    @field_validator("email")
    @classmethod
    def check_email(cls, v: str) -> str:
        return _validate_email(v)


class SetPasswordRequest(BaseModel):
    """Schema for setting password via invite token."""

    token: str
    password: str


class UserResponse(BaseModel):
    """Schema for user API responses (never exposes password_hash)."""

    id: str
    username: str
    email: str
    role: UserRole
    is_active: bool
    has_password: bool
    theme_preference: str
    created_at: datetime
    updated_at: datetime


class InviteResponse(BaseModel):
    """Response for invite user endpoint."""

    user: UserResponse
    invite_url: str
