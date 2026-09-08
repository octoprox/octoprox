# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Helpers shared by the entity routes."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError


def unique_name_violation(exc: IntegrityError, kind: str, name: str) -> HTTPException:
    """Translate a hit on the per-project unique name index into a 400.

    The database is the single source of truth for name uniqueness (see
    migration 022); routes call this from an ``except IntegrityError`` so the
    user gets a readable message. Any other integrity error is re-raised
    unchanged, so it still surfaces as a server error rather than being
    mislabelled.
    """
    if f"ix_{kind}s_project_name_unique" not in str(exc.orig or exc):
        raise exc
    return HTTPException(status_code=400, detail=f"A {kind} named '{name}' already exists in this project")
