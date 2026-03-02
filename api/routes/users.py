# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""User management routes for Octoprox."""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.auth import CurrentUserDep, RequireAdminDep
from api.core.password import hash_password, verify_password
from api.db.repository import UserRepository
from api.db.session import get_db
from api.models.user import User, UserCreate, UserResponse, UserSelfUpdate, UserUpdate

logger = structlog.get_logger()

router = APIRouter(prefix="/users")

DbDep = Annotated[AsyncSession, Depends(get_db)]


class UserListResponse(BaseModel):
    """Response for listing users."""

    total: int
    users: list[UserResponse]


def _to_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


# --- Self endpoints (any authenticated user) ---
# These must be declared BEFORE /{user_id} to avoid path conflicts.


@router.get("/me", response_model=UserResponse)
async def get_self(session: DbDep, user: CurrentUserDep) -> UserResponse:
    """Get the current user's profile."""
    repo = UserRepository(session)
    db_user = await repo.get_by_id(user.id)

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    return _to_response(db_user)


@router.patch("/me", response_model=UserResponse)
async def update_self(
    session: DbDep, data: UserSelfUpdate, user: CurrentUserDep
) -> UserResponse:
    """Update the current user's own profile (email, password)."""
    repo = UserRepository(session)
    db_user = await repo.get_by_id(user.id)
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    if data.password is not None:
        if not data.current_password:
            raise HTTPException(
                status_code=400, detail="Current password is required to change password"
            )
        if not verify_password(data.current_password, db_user.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        db_user.password_hash = hash_password(data.password)

    if data.email is not None:
        if data.email and data.email != db_user.email:
            existing_email = await repo.get_by_email(data.email)
            if existing_email:
                raise HTTPException(
                    status_code=400, detail=f"Email '{data.email}' already exists"
                )
        db_user.email = data.email

    await repo.update(db_user)

    return _to_response(db_user)


# --- Admin-only endpoints ---


@router.get("", response_model=UserListResponse)
async def list_users(session: DbDep, _admin: RequireAdminDep) -> UserListResponse:
    """List all users (admin only)."""
    repo = UserRepository(session)
    users = await repo.get_all()

    return UserListResponse(total=len(users), users=[_to_response(u) for u in users])


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    session: DbDep, data: UserCreate, _admin: RequireAdminDep
) -> UserResponse:
    """Create a new user (admin only)."""
    repo = UserRepository(session)
    existing = await repo.get_by_username(data.username)
    if existing:
        raise HTTPException(
            status_code=400, detail=f"Username '{data.username}' already exists"
        )
    if data.email:
        existing_email = await repo.get_by_email(data.email)
        if existing_email:
            raise HTTPException(
                status_code=400, detail=f"Email '{data.email}' already exists"
            )

    user = User(
        username=data.username,
        email=data.email,
        password_hash=hash_password(data.password),
        role=data.role,
    )
    await repo.create(user)

    logger.info("User created", username=user.username, role=user.role.value)
    return _to_response(user)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    session: DbDep, user_id: str, _admin: RequireAdminDep
) -> UserResponse:
    """Get a user by ID (admin only)."""
    repo = UserRepository(session)
    user = await repo.get_by_id(user_id)

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return _to_response(user)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    session: DbDep, user_id: str, data: UserUpdate, _admin: RequireAdminDep
) -> UserResponse:
    """Update a user (admin only)."""
    repo = UserRepository(session)
    user = await repo.get_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if data.username is not None:
        if data.username != user.username:
            existing = await repo.get_by_username(data.username)
            if existing:
                raise HTTPException(
                    status_code=400, detail=f"Username '{data.username}' already exists"
                )
        user.username = data.username
    if data.email is not None:
        if data.email and data.email != user.email:
            existing_email = await repo.get_by_email(data.email)
            if existing_email:
                raise HTTPException(
                    status_code=400, detail=f"Email '{data.email}' already exists"
                )
        user.email = data.email
    if data.password is not None:
        user.password_hash = hash_password(data.password)
    if data.role is not None:
        user.role = data.role
    if data.is_active is not None:
        user.is_active = data.is_active

    await repo.update(user)

    logger.info("User updated", user_id=user_id)
    return _to_response(user)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    session: DbDep, user_id: str, user: CurrentUserDep, _admin: RequireAdminDep
) -> None:
    """Delete a user (admin only). Cannot delete yourself."""
    if user.id == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    repo = UserRepository(session)
    if not await repo.delete(user_id):
        raise HTTPException(status_code=404, detail="User not found")

    logger.info("User deleted", user_id=user_id)
