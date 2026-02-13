"""Authentication routes for Octoprox."""

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request
from jose import JWTError, jwt
from pydantic import BaseModel

from api.core.config import settings

logger = structlog.get_logger()

router = APIRouter(prefix="/auth")

ALGORITHM = "HS256"


class LoginRequest(BaseModel):
    """Login request payload."""
    username: str
    password: str


class LoginResponse(BaseModel):
    """Login response with JWT token."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthStatus(BaseModel):
    """Authentication status response."""
    enabled: bool
    authenticated: bool = False
    username: str | None = None


def create_jwt(payload: dict[str, Any], secret: str, expiry_hours: int) -> str:
    """Create a JWT token using python-jose.

    Args:
        payload: The token payload data
        secret: Secret key for signing
        expiry_hours: Token expiry in hours

    Returns:
        JWT token string
    """
    to_encode = payload.copy()
    expire = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, secret, algorithm=ALGORITHM)


def verify_jwt(token: str, secret: str) -> dict[str, Any] | None:
    """Verify a JWT token and return the payload.

    Args:
        token: JWT token string
        secret: Secret key for verification

    Returns:
        Token payload if valid, None otherwise
    """
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


@router.post("/login", response_model=LoginResponse)
async def login(login_req: LoginRequest) -> LoginResponse:
    """Authenticate user and return JWT token."""
    if not settings.auth_enabled:
        raise HTTPException(
            status_code=400,
            detail="Authentication is not enabled"
        )
    
    # Validate credentials
    if (login_req.username != settings.auth_username or 
        login_req.password != settings.auth_password):
        logger.warning("Failed login attempt", username=login_req.username)
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password"
        )
    
    # Create JWT token
    token = create_jwt(
        payload={"sub": login_req.username},
        secret=settings.jwt_secret,
        expiry_hours=settings.jwt_expiry_hours,
    )
    
    logger.info("User logged in", username=login_req.username)
    
    return LoginResponse(
        access_token=token,
        expires_in=settings.jwt_expiry_hours * 3600,
    )


@router.get("/status", response_model=AuthStatus)
async def auth_status(request: Request) -> AuthStatus:
    """Get current authentication status."""
    if not settings.auth_enabled:
        return AuthStatus(enabled=False)
    
    # Check for token in Authorization header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        payload = verify_jwt(token, settings.jwt_secret)
        if payload:
            return AuthStatus(
                enabled=True,
                authenticated=True,
                username=payload.get("sub"),
            )
    
    return AuthStatus(enabled=True, authenticated=False)

