"""Configuration management for Octoprox."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment and config files."""
    
    # Server settings
    host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)
    proxy_port: int = Field(default=8080)
    debug: bool = Field(default=False)
    
    # Environment
    env: str = Field(default="development", alias="OCTOPROX_ENV")
    
    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0", alias="OCTOPROX_REDIS_URL")
    
    # Logging
    log_level: str = Field(default="INFO", alias="OCTOPROX_LOG_LEVEL")
    
    # CORS
    cors_origins: list[str] = Field(default=["http://localhost:3000", "http://localhost:5173"])
    
    # Proxy settings
    default_strategy: str = Field(default="round_robin")
    health_check_interval: int = Field(default=30)
    health_check_timeout: int = Field(default=10)
    connection_timeout: int = Field(default=30)
    max_retries: int = Field(default=3)

    # Authentication settings
    auth_enabled: bool = Field(default=False, description="Enable authentication")
    auth_username: str = Field(default="admin", description="Login username")
    auth_password: str = Field(default="", description="Login password (required if auth enabled)")
    jwt_secret: str = Field(
        default="change-me-in-production",
        description="Secret key for JWT token signing"
    )
    jwt_expiry_hours: int = Field(default=24, description="JWT token expiry in hours")

    model_config = {
        "env_prefix": "OCTOPROX_",
        "env_file": ".env",
        "extra": "ignore",
    }
    
    @classmethod
    def from_yaml(cls, config_path: Path) -> "Settings":
        """Load settings from a YAML configuration file."""
        if not config_path.exists():
            return cls()
        
        with open(config_path) as f:
            config_data = yaml.safe_load(f) or {}
        
        # Flatten nested config
        flat_config: dict[str, Any] = {}
        
        if "server" in config_data:
            flat_config.update(config_data["server"])
        if "logging" in config_data:
            flat_config["log_level"] = config_data["logging"].get("level", "INFO")
        if "redis" in config_data:
            flat_config["redis_url"] = config_data["redis"].get("url", "redis://localhost:6379/0")
        if "proxy" in config_data:
            proxy_cfg = config_data["proxy"]
            flat_config["default_strategy"] = proxy_cfg.get("default_strategy", "round_robin")
            if "health_check" in proxy_cfg:
                flat_config["health_check_interval"] = proxy_cfg["health_check"].get("interval_seconds", 30)
                flat_config["health_check_timeout"] = proxy_cfg["health_check"].get("timeout_seconds", 10)
            if "connection" in proxy_cfg:
                flat_config["connection_timeout"] = proxy_cfg["connection"].get("timeout_seconds", 30)
                flat_config["max_retries"] = proxy_cfg["connection"].get("max_retries", 3)
        
        return cls(**flat_config)


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    env = os.getenv("OCTOPROX_ENV", "development")
    config_path = Path("config") / f"{env.lower()}.yaml"
    
    if env == "development":
        config_path = Path("config/dev.yaml")
    elif env == "production":
        config_path = Path("config/prod.yaml")
    
    return Settings.from_yaml(config_path)


settings = get_settings()

