# Copyright 2025 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Configuration management for Octoprox."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource


def _load_yaml_config(config_path: Path) -> dict[str, Any]:
    """Load and flatten YAML configuration file."""
    if not config_path.exists():
        return {}

    with open(config_path) as f:
        config_data = yaml.safe_load(f) or {}

    # Flatten nested config
    flat_config: dict[str, Any] = {}

    if "server" in config_data:
        flat_config.update(config_data["server"])
    if "logging" in config_data:
        flat_config["log_level"] = config_data["logging"].get("level")
    if "redis" in config_data:
        flat_config["redis_url"] = config_data["redis"].get("url")
    if "database" in config_data:
        db_cfg = config_data["database"]
        flat_config["db_host"] = db_cfg.get("host")
        flat_config["db_port"] = db_cfg.get("port")
        flat_config["db_user"] = db_cfg.get("user")
        flat_config["db_password"] = db_cfg.get("password")
        flat_config["db_name"] = db_cfg.get("name")
        flat_config["db_application_name"] = db_cfg.get("application_name")
        if "metrics_flush_interval" in db_cfg:
            flat_config["metrics_flush_interval"] = db_cfg["metrics_flush_interval"]
    if "proxy" in config_data:
        proxy_cfg = config_data["proxy"]
        flat_config["default_strategy"] = proxy_cfg.get("default_strategy")
        if "health_check" in proxy_cfg:
            flat_config["health_check_interval"] = proxy_cfg["health_check"].get("interval_seconds")
            flat_config["health_check_timeout"] = proxy_cfg["health_check"].get("timeout_seconds")
        if "connection" in proxy_cfg:
            flat_config["connection_timeout"] = proxy_cfg["connection"].get("timeout_seconds")
            flat_config["max_retries"] = proxy_cfg["connection"].get("max_retries")

    # Remove None values
    return {k: v for k, v in flat_config.items() if v is not None}


# Global to store YAML config path for settings sources
_yaml_config_path: Path | None = None


class YamlConfigSettingsSource(PydanticBaseSettingsSource):
    """Custom settings source that reads from YAML config file."""

    def get_field_value(
        self, field: Any, field_name: str
    ) -> tuple[Any, str, bool]:
        """Get field value from YAML config."""
        if _yaml_config_path is None:
            return None, field_name, False

        yaml_config = _load_yaml_config(_yaml_config_path)
        if field_name in yaml_config:
            return yaml_config[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        """Return all values from YAML config."""
        if _yaml_config_path is None:
            return {}
        return _load_yaml_config(_yaml_config_path)


class Settings(BaseSettings):
    """Application settings loaded from environment and config files."""

    # Server settings
    host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)
    proxy_port: int = Field(default=8080)
    debug: bool = Field(default=False)

    # Environment
    env: str = Field(default="development")

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0")

    # Database
    db_host: str = Field(default="localhost")
    db_port: int = Field(default=5432)
    db_user: str = Field(default="postgres")
    db_password: str = Field(default="")
    db_name: str = Field(default="octoprox")
    db_application_name: str = Field(default="octoprox")

    @property
    def database_url(self) -> str:
        """Construct async database URL from components.

        Note: asyncpg doesn't support application_name in URL params,
        it must be passed via connect_args in the engine configuration.
        """
        from urllib.parse import quote_plus

        # Build auth part only if user is specified
        if self.db_user:
            if self.db_password:
                auth = f"{quote_plus(self.db_user)}:{quote_plus(self.db_password)}@"
            else:
                auth = f"{quote_plus(self.db_user)}@"
        else:
            auth = ""

        return f"postgresql+asyncpg://{auth}{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def database_url_sync(self) -> str:
        """Construct sync database URL for migrations."""
        from urllib.parse import quote_plus

        # Build auth part only if user is specified
        if self.db_user:
            if self.db_password:
                auth = f"{quote_plus(self.db_user)}:{quote_plus(self.db_password)}@"
            else:
                auth = f"{quote_plus(self.db_user)}@"
        else:
            auth = ""

        return (
            f"postgresql://{auth}{self.db_host}:{self.db_port}/{self.db_name}"
            f"?application_name={quote_plus(self.db_application_name)}"
        )

    # Metrics flush interval (seconds) - how often to flush Redis metrics to Postgres
    metrics_flush_interval: int = Field(default=60)

    # Logging
    log_level: str = Field(default="INFO")

    # CORS
    cors_origins: list[str] = Field(default=["http://localhost:3000", "http://localhost:5173"])

    # Proxy settings
    default_strategy: str = Field(default="round_robin")
    health_check_interval: int = Field(default=60)
    health_check_timeout: int = Field(default=30)
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
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Customize settings sources priority.

        Priority (highest to lowest):
        1. Init settings (explicit kwargs)
        2. Environment variables (OCTOPROX_*)
        3. .env file
        4. YAML config file
        5. Default values (handled by pydantic)
        """
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
        )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Priority (highest to lowest):
    1. Environment variables (OCTOPROX_*)
    2. YAML config file (config/dev.yaml or config/prod.yaml)
    3. Default values
    """
    global _yaml_config_path

    env = os.getenv("OCTOPROX_ENV", "development")

    if env == "development":
        config_path = Path("config/dev.yaml")
    elif env == "production":
        config_path = Path("config/prod.yaml")
    else:
        config_path = Path("config") / f"{env.lower()}.yaml"

    # Store path globally so YamlConfigSettingsSource can access it
    _yaml_config_path = config_path

    return Settings()


settings = get_settings()

