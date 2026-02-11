"""Proxy model definitions."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ProxyProtocol(str, Enum):
    """Supported proxy protocols."""
    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"


class ProxyStatus(str, Enum):
    """Proxy health status."""
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class Proxy(BaseModel):
    """Represents a proxy server."""
    
    id: str = Field(default_factory=lambda: str(uuid4()))
    host: str
    port: int
    protocol: ProxyProtocol = ProxyProtocol.HTTP
    username: str | None = None
    password: str | None = None
    
    # Status and health
    status: ProxyStatus = ProxyStatus.UNKNOWN
    consecutive_failures: int = 0
    last_check_latency_ms: float = 0.0
    
    # Statistics
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_latency_ms: float = 0.0
    
    # Metadata
    source_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    
    @property
    def url(self) -> str:
        """Get the proxy URL."""
        auth = ""
        if self.username and self.password:
            auth = f"{self.username}:{self.password}@"
        return f"{self.protocol.value}://{auth}{self.host}:{self.port}"

    @property
    def success_rate(self) -> float:
        """Calculate success rate percentage."""
        if self.request_count == 0:
            return 0.0
        return (self.success_count / self.request_count) * 100


class ProxyCreate(BaseModel):
    """Schema for creating a new proxy."""
    host: str
    port: int
    protocol: ProxyProtocol = ProxyProtocol.HTTP
    username: str | None = None
    password: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProxyUpdate(BaseModel):
    """Schema for updating a proxy."""
    host: str | None = None
    port: int | None = None
    protocol: ProxyProtocol | None = None
    username: str | None = None
    password: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class ProxyResponse(BaseModel):
    """Schema for proxy API responses."""
    id: str
    host: str
    port: int
    protocol: str
    status: str
    request_count: int
    success_count: int
    failure_count: int
    success_rate: float
    avg_latency_ms: float
    tags: list[str]
    created_at: datetime

