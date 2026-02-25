# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""SQLAlchemy base configuration."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""
    pass

