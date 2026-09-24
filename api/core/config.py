# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Configuration management for Octoprox."""

import os
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from api.core.logging import LOG_FORMATS


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
        logging_cfg = config_data["logging"] or {}
        flat_config["log_level"] = logging_cfg.get("level")
        flat_config["log_format"] = logging_cfg.get("format")
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
    if "system" in config_data:
        system_cfg = config_data["system"] or {}
        flat_config["system_metrics_interval"] = system_cfg.get("metrics_interval")
        flat_config["system_metrics_retention_days"] = system_cfg.get("metrics_retention_days")
    if "proxy" in config_data:
        proxy_cfg = config_data["proxy"]
        flat_config["default_strategy"] = proxy_cfg.get("default_strategy")
        if "health_check" in proxy_cfg:
            flat_config["health_check_interval"] = proxy_cfg["health_check"].get("interval_seconds")
            flat_config["health_check_timeout"] = proxy_cfg["health_check"].get("timeout_seconds")
        if "connection" in proxy_cfg:
            flat_config["connection_timeout"] = proxy_cfg["connection"].get("timeout_seconds")
            flat_config["max_retries"] = proxy_cfg["connection"].get("max_retries")
        if "ip_refresh_interval" in proxy_cfg:
            flat_config["ip_refresh_interval"] = proxy_cfg["ip_refresh_interval"]
        if "geo_lookup" in proxy_cfg:
            geo_cfg = proxy_cfg["geo_lookup"] or {}
            flat_config["geo_lookup_enabled"] = geo_cfg.get("enabled")
            flat_config["geo_lookup_url"] = geo_cfg.get("url")
            flat_config["geo_lookup_ip_path"] = geo_cfg.get("ip_path")
            flat_config["geo_lookup_country_path"] = geo_cfg.get("country_path")
            flat_config["geo_lookup_timeout_seconds"] = geo_cfg.get("timeout_seconds")
    if "geo" in config_data:
        geo_cfg = config_data["geo"] or {}
        flat_config["geo_cache_dir"] = geo_cfg.get("cache_dir")
        flat_config["geo_databases"] = geo_cfg.get("databases")
        flat_config["geo_policy_defaults"] = geo_cfg.get("defaults")
        if "echo" in geo_cfg:
            echo_cfg = geo_cfg["echo"] or {}
            flat_config["geo_echo_enabled"] = echo_cfg.get("enabled")
            flat_config["geo_echo_trusted_proxies"] = echo_cfg.get("trusted_proxies")
        if "observations" in geo_cfg:
            obs_cfg = geo_cfg["observations"] or {}
            flat_config["geo_observation_publish_interval"] = obs_cfg.get("publish_interval_seconds")
            flat_config["geo_observation_flush_interval"] = obs_cfg.get("flush_interval_seconds")
            flat_config["geo_observation_max_buffer"] = obs_cfg.get("max_buffer")
        if "updater" in geo_cfg:
            upd_cfg = geo_cfg["updater"] or {}
            flat_config["geo_updater_interval"] = upd_cfg.get("check_interval_seconds")
    if "providers" in config_data:
        providers_cfg = config_data["providers"] or {}
        flat_config["providers_dir"] = providers_cfg.get("dir")
        if "egress" in providers_cfg:
            egress_cfg = providers_cfg["egress"] or {}
            flat_config["provider_egress_allow_http"] = egress_cfg.get("allow_http")
            flat_config["provider_egress_allow_private"] = egress_cfg.get("allow_private")
        if "http" in providers_cfg:
            http_cfg = providers_cfg["http"] or {}
            flat_config["provider_http_timeout_seconds"] = http_cfg.get("timeout_seconds")
            flat_config["provider_http_max_response_bytes"] = http_cfg.get("max_response_bytes")

    if "tls_mitm" in config_data:
        mitm_cfg = config_data["tls_mitm"]
        if "ca_cert_path" in mitm_cfg:
            flat_config["tls_mitm_ca_cert_path"] = mitm_cfg["ca_cert_path"]
        if "ca_key_path" in mitm_cfg:
            flat_config["tls_mitm_ca_key_path"] = mitm_cfg["ca_key_path"]

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

    # Environment
    env: str = Field(default="development")

    # Instance identity for multi-instance deployments. Used by the event bus
    # to drop self-echoes, by Redis heartbeats to advertise membership, and by
    # leadership leases to identify the holder.
    instance_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique identifier for this process. Auto-generated per boot if unset.",
    )

    # Process role. Today only `all` (monolith) is wired; the flag exists so
    # Option B can split into control/data/worker without re-introducing it.
    role: Literal["all", "control", "data", "worker"] = Field(
        default="all",
        description=(
            "Process role: 'all' runs everything in one process (default), "
            "'control' / 'data' / 'worker' reserved for the future tier split."
        ),
    )

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

    # Install-wide gauge snapshots behind the admin System trend charts.
    # One row per interval for the whole install, so the volume is small and
    # retention alone keeps it bounded (no compaction tiers).
    system_metrics_interval: int = Field(
        default=300,
        description=(
            "How often to snapshot install-wide system gauges, in seconds "
            "(minimum 60; 0 disables snapshotting entirely)"
        ),
    )
    system_metrics_retention_days: int = Field(
        default=90,
        description="How long to keep system metric snapshots, in days (0 keeps them forever)",
    )

    # Logging
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)"
    )
    log_format: str = Field(
        default="console",
        description="Log output format: console (human-readable) or json (one object per line)",
    )

    @field_validator("log_format")
    @classmethod
    def _check_log_format(cls, v: str) -> str:
        v = v.lower()
        if v not in LOG_FORMATS:
            raise ValueError(f"log_format must be one of: {', '.join(LOG_FORMATS)}")
        return v

    @property
    def debug(self) -> bool:
        """Debug mode is enabled when log level is DEBUG."""
        return self.log_level.upper() == "DEBUG"

    # CORS
    cors_origins: list[str] = Field(default=["http://localhost:3000", "http://localhost:5173"])

    # Proxy settings
    default_strategy: str = Field(default="round_robin")
    health_check_interval: int = Field(default=60)
    health_check_timeout: int = Field(default=30)
    connection_timeout: int = Field(default=30)
    max_retries: int = Field(default=3)
    ip_refresh_interval: int = Field(default=3600, description="IP refresh interval in seconds for port-based proxies")

    # Exit-location lookup for manually added (static) proxies: a request is
    # made through the proxy to a JSON endpoint reporting the caller's IP and country.
    geo_lookup_enabled: bool = Field(
        default=True, description="Look up the exit IP and country of static proxies when they are added"
    )
    # httpbin answers every caller with the IP and nothing else, which is all
    # attribution needs from an echo. Point this at an Octoprox /echo in
    # production (Settings -> IP attribution).
    geo_lookup_url: str = Field(default="https://httpbin.org/ip", description="JSON endpoint requested through the proxy")
    geo_lookup_ip_path: str = Field(default="origin", description="JMESPath to the IP in the response")
    geo_lookup_country_path: str = Field(default="", description="JMESPath to the ISO country code in the response (empty when the endpoint reports none)")
    geo_lookup_timeout_seconds: float = Field(default=15.0, description="Timeout for one lookup request")

    # IP attribution (see docs/ip-attribution.md). The live settings row is
    # edited in the admin panel; these are the parts that describe this process
    # or seed a fresh install.
    geo_cache_dir: str = Field(
        default="data/geo",
        description="Directory where this instance caches IP database files fetched from Postgres",
    )
    geo_databases: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Operator-managed IP database files: list of {path, name?, priority?} entries loaded at startup",
    )
    geo_policy_defaults: dict[str, Any] = Field(
        default_factory=dict,
        description="Initial attribution settings for a fresh install (fields of GeoSettings); the admin panel edits the live row",
    )
    geo_echo_enabled: bool = Field(
        default=True, description="Serve the public /echo endpoint that reports the caller's IP and attribution"
    )
    geo_echo_trusted_proxies: list[str] = Field(
        default_factory=list,
        description="CIDRs of load balancers whose X-Forwarded-For the /echo endpoint trusts for the client IP",
    )
    geo_observation_publish_interval: float = Field(
        default=5.0, description="How often buffered IP observations are pushed to the Redis queue, in seconds"
    )
    geo_observation_flush_interval: float = Field(
        default=30.0, description="How often the leader drains the Redis observation queue into Postgres, in seconds"
    )
    geo_observation_max_buffer: int = Field(
        default=5000, description="Observations one instance holds in memory before dropping the oldest"
    )
    geo_updater_interval: float = Field(
        default=3600.0, description="How often the leader checks for scheduled IP database downloads, in seconds"
    )

    # Provider SDK settings
    providers_dir: str | None = Field(
        default=None,
        description="Directory of operator-supplied provider descriptor YAML files (loaded at startup)",
    )
    provider_egress_allow_http: bool = Field(
        default=False, description="Allow descriptors to call plain-http vendor APIs (development only)"
    )
    provider_egress_allow_private: bool = Field(
        default=False,
        description="Allow descriptor API calls to private/loopback addresses (development and tests only)",
    )
    provider_http_timeout_seconds: float = Field(
        default=60.0, description="Timeout for a single vendor API request made for a descriptor"
    )
    provider_http_max_response_bytes: int = Field(
        default=50 * 1024 * 1024,
        description="Largest vendor API response accepted (0 disables the limit)",
    )

    # TLS MITM settings
    tls_mitm_ca_cert_path: str = Field(
        default="data/ca/octoprox-ca.crt",
        description="Path to the MITM CA certificate file (auto-generated if missing)",
    )
    tls_mitm_ca_key_path: str = Field(
        default="data/ca/octoprox-ca.key",
        description="Path to the MITM CA private key file (auto-generated if missing)",
    )

    # Authentication settings
    auth_username: str = Field(default="admin", description="Admin username for initial seed")
    auth_password: str = Field(default="", description="Admin password for initial seed")
    jwt_secret: str = Field(
        default="change-me-in-production",
        description="Secret key for JWT token signing"
    )
    jwt_expiry_hours: int = Field(default=24, description="JWT token expiry in hours")
    invite_token_expiry_hours: int = Field(default=168, description="Invite token expiry in hours (default 7 days)")

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

