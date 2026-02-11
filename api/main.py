"""Main entry point for Octoprox API server."""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
import uvicorn
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.core.auth import require_auth
from api.core.config import settings
from api.core.proxy_manager import ProxyManager
from api.routes import auth, forward, health, metrics, proxies, sources

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    logger.info("Starting Octoprox", version="0.1.0")
    
    # Initialize proxy manager
    proxy_manager = ProxyManager()
    app.state.proxy_manager = proxy_manager
    
    # Start background tasks
    await proxy_manager.start()
    
    yield
    
    # Cleanup
    logger.info("Shutting down Octoprox")
    await proxy_manager.stop()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Octoprox",
        description="A dynamic and flexible proxy manager",
        version="0.1.0",
        lifespan=lifespan,
    )
    
    # Configure CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Include routers
    # Auth routes (public)
    app.include_router(auth.router, prefix="/api/v1/auth", tags=["Auth"])

    # Health check (public)
    app.include_router(health.router, tags=["Health"])

    # Protected routes - require auth when enabled
    auth_dependency = [Depends(require_auth)]
    app.include_router(
        proxies.router,
        prefix="/api/v1/proxies",
        tags=["Proxies"],
        dependencies=auth_dependency,
    )
    app.include_router(
        sources.router,
        prefix="/api/v1/sources",
        tags=["Sources"],
        dependencies=auth_dependency,
    )
    app.include_router(
        metrics.router,
        prefix="/api/v1/metrics",
        tags=["Metrics"],
        dependencies=auth_dependency,
    )
    app.include_router(
        forward.router,
        prefix="/api/v1/forward",
        tags=["Forward"],
        dependencies=auth_dependency,
    )

    return app


app = create_app()


def run() -> None:
    """Run the application using uvicorn."""
    uvicorn.run(
        "api.main:app",
        host=settings.host,
        port=settings.api_port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    run()

