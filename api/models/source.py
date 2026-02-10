"""Proxy source model definitions."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    """Types of proxy sources."""
    STATIC = "static"
    API = "api"
    AWS = "aws"
    GCP = "gcp"
    AZURE = "azure"


class ProxySource(BaseModel):
    """Represents a source of proxies."""
    
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    type: SourceType
    enabled: bool = True
    
    # Configuration for the source
    config: dict[str, Any] = Field(default_factory=dict)
    
    # Statistics
    proxy_count: int = 0
    last_refresh: datetime | None = None
    refresh_interval_seconds: int = 300
    
    # Metadata
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    model_config = {"use_enum_values": True}


class SourceCreate(BaseModel):
    """Schema for creating a new source."""
    name: str
    type: SourceType
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    refresh_interval_seconds: int = 300


class SourceUpdate(BaseModel):
    """Schema for updating a source."""
    name: str | None = None
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    refresh_interval_seconds: int | None = None


class SourceResponse(BaseModel):
    """Schema for source API responses."""
    id: str
    name: str
    type: str
    enabled: bool
    proxy_count: int
    last_refresh: datetime | None
    refresh_interval_seconds: int
    created_at: datetime

