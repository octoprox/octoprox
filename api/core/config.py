# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Configuration management for Octoprox."""

import os
import socket
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml  # type: ignore[import-untyped]
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource

from api.core.logging import LOG_FORMATS
from api.core.roles import JobKind, Role


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
        flat_config["redis_request_timeout"] = config_data["redis"].get("request_timeout_seconds")
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
    if "metrics" in config_data:
        hosts_cfg = (config_data["metrics"] or {}).get("hosts") or {}
        flat_config["host_metrics_enabled"] = hosts_cfg.get("enabled")
        flat_config["host_metrics_max_hosts"] = hosts_cfg.get("max_hosts")
    if "failure_log" in config_data:
        failure_cfg = config_data["failure_log"] or {}
        flat_config["failure_log_enabled"] = failure_cfg.get("enabled")
        flat_config["failure_log_max_entries"] = failure_cfg.get("max_entries")
        flat_config["failure_log_retention_hours"] = failure_cfg.get("retention_hours")
        flat_config["failure_log_max_per_window"] = failure_cfg.get("max_per_window")
    if "system" in config_data:
        system_cfg = config_data["system"] or {}
        flat_config["system_metrics_interval"] = system_cfg.get("metrics_interval")
        flat_config["system_metrics_retention_days"] = system_cfg.get("metrics_retention_days")
    if "feed" in config_data:
        feed_cfg = config_data["feed"] or {}
        flat_config["feed_compile_interval"] = feed_cfg.get("compile_interval_seconds")
        flat_config["reconcile_interval"] = feed_cfg.get("reconcile_interval_seconds")
    if "proxy" in config_data:
        proxy_cfg = config_data["proxy"]
        flat_config["default_strategy"] = proxy_cfg.get("default_strategy")
        if "health_check" in proxy_cfg:
            flat_config["health_check_interval"] = proxy_cfg["health_check"].get("interval_seconds")
            flat_config["health_check_timeout"] = proxy_cfg["health_check"].get("timeout_seconds")
            flat_config["health_check_concurrency"] = proxy_cfg["health_check"].get("concurrency")
        if "connection" in proxy_cfg:
            flat_config["connection_timeout"] = proxy_cfg["connection"].get("timeout_seconds")
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

    if "wireguard" in config_data:
        wg_cfg = config_data["wireguard"] or {}
        flat_config["wireguard_enabled"] = wg_cfg.get("enabled")
        flat_config["wireguard_interface"] = wg_cfg.get("interface")
        flat_config["wireguard_listen_port"] = wg_cfg.get("listen_port")
        flat_config["wireguard_mtu"] = wg_cfg.get("mtu")
        flat_config["wireguard_defaults"] = wg_cfg.get("defaults")

    if "openvpn" in config_data:
        ovpn_cfg = config_data["openvpn"] or {}
        flat_config["openvpn_enabled"] = ovpn_cfg.get("enabled")
        flat_config["openvpn_interface"] = ovpn_cfg.get("interface")
        flat_config["openvpn_listen_port"] = ovpn_cfg.get("listen_port")
        flat_config["openvpn_mtu"] = ovpn_cfg.get("mtu")
        flat_config["openvpn_defaults"] = ovpn_cfg.get("defaults")

    if "tunnel" in config_data:
        tunnel_cfg = config_data["tunnel"] or {}
        flat_config["tunnel_transparent_port"] = tunnel_cfg.get("transparent_port")
        flat_config["tunnel_dns_port"] = tunnel_cfg.get("dns_port")
        flat_config["tunnel_fake_ip_range"] = tunnel_cfg.get("fake_ip_range")
        flat_config["tunnel_sniff_timeout_seconds"] = tunnel_cfg.get("sniff_timeout_seconds")
        flat_config["tunnel_block_encrypted_dns"] = tunnel_cfg.get("block_encrypted_dns")

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
    # Where clients reach the proxy listener, as written into the URLs the
    # Connect page generates. Behind a load balancer or a NAT mapping the
    # listener's own address and port are not the ones clients use, and a
    # balancer that terminates TLS in front of it makes it an HTTPS proxy.
    proxy_public_url: str = Field(
        default="",
        description=(
            "The URL clients reach the proxy at: http:// or https://, a host and an optional port, "
            "such as https://proxy.example.com. Without a port clients use the scheme's default "
            "(https URLs leave it out; http ones name 80, since curl reads a bare http:// proxy as 1080). "
            "Empty means http:// on the host the browser reached the web UI at and proxy_port"
        ),
    )

    # Environment
    env: str = Field(default="development")

    # Instance identity: names this process in its log lines, as the holder of
    # the controller lease, in its heartbeat on the System page and in the
    # tunnel statuses it publishes. The host name makes it recognisable (a
    # Kubernetes pod, a compose container); the random suffix tells apart two
    # processes on one host, and two boots of one container.
    instance_id: str = Field(
        default_factory=lambda: f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}",
        description="Unique identifier for this process. The host name and a random suffix per boot if unset.",
    )

    # What this process runs. A cluster runs each role in its own processes,
    # scaled on its own; a single instance runs them all in one.
    role: Role = Field(
        default=Role.ALL,
        description=(
            "What this process runs: 'all' runs everything in one process (default); "
            "'api', 'gateway', 'controller', 'worker' or 'echo' run one part of a cluster"
        ),
    )
    # The job kinds a worker process takes, so a kind can have workers of its
    # own: health checks on one set of workers never hold up the syncs and
    # scale checks of another.
    worker_jobs: Annotated[list[JobKind], NoDecode] = Field(
        default_factory=lambda: [*JobKind],
        description=(
            "Job kinds this worker runs, comma-separated in the environment (health,sync); "
            "every kind by default"
        ),
    )

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0")
    # How long a Redis call may take when something waits on it: a connect,
    # a call on a request's path (sticky bindings, preflight verdicts, rate
    # windows, on-demand asks, MITM captures, tunnel fake IPs), the steps
    # after a committed write (its change event, a job asked for at once) and
    # the heartbeat's own write. Past it the request path fails open, so a
    # stuck Redis costs a request this long rather than hanging it, and a
    # change event is sent again in the background. Any other reply may take
    # up to 30 s (api.db.redis.REPLY_TIMEOUT_SECONDS), a backstop for dead
    # connections: those calls belong to background loops, and what they
    # carry is safe to send again.
    redis_request_timeout: float = Field(
        default=2.0,
        gt=0,
        description=(
            "Seconds one Redis connect may take, and one call on a request's path or right after a save, before "
            "it is given up (the request path then fails open); other replies may take up to 30 seconds"
        ),
    )

    # The routing feed (api.feed) and the change stream it is compiled on.
    # Every committed write to a definition appends an entry to a Redis
    # stream. The leading controller compiles the feed as entries arrive, at
    # most once per feed_compile_interval, so a burst of writes costs one
    # compile per interval, and gateways wake as each compile is published;
    # the api and workers re-read their definitions when an entry concerns
    # them. Every reconcile_interval each process reads in full what it
    # follows anyway, which picks up writes that announced nothing: a crash
    # between a commit and its entry, manual SQL, an older release writing
    # to the same database during an upgrade.
    feed_compile_interval: float = Field(
        default=1.0,
        gt=0,
        description=(
            "The least time between two compiles of the routing feed, in seconds: a change after a quiet spell "
            "is compiled at once, and changes that arrive sooner are compiled together"
        ),
    )
    reconcile_interval: float = Field(
        default=3600.0,
        gt=0,
        description=(
            "How often every process reads in full what it follows, whatever its streams announced, in "
            "seconds: the controller compiles everything and folds in every quarantine and traffic block, "
            "gateways re-read the feed, the api and workers their definitions"
        ),
    )

    # Database
    db_host: str = Field(default="localhost")
    db_port: int = Field(default=5432)
    db_user: str = Field(default="postgres")
    db_password: str = Field(default="")
    db_name: str = Field(default="octoprox")
    # Names this process's sessions in pg_stat_activity. Unset, it names the
    # role (octoprox-api, octoprox-worker, ...), so each role's load on the
    # database can be told apart.
    db_application_name: str = Field(
        default="",
        description="Name of this process's Postgres sessions (pg_stat_activity); octoprox-<role> when unset",
    )

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

        # The driver is named: SQLAlchemy 2.1 maps a bare postgresql:// to
        # psycopg 3, and the migrations run on psycopg2.
        return (
            f"postgresql+psycopg2://{auth}{self.db_host}:{self.db_port}/{self.db_name}"
            f"?application_name={quote_plus(self.db_application_name)}"
        )

    # Metrics flush interval (seconds) - how often to flush Redis metrics to Postgres
    metrics_flush_interval: int = Field(default=60)

    # Per-host metrics: a project's requests by destination host and connector
    # (the Hosts page). The one metrics table whose cardinality the operator
    # does not control: a pool used to crawl the open web touches hosts
    # without end. Every host is kept by default; an install that crawls can
    # switch per-host counting off, or set a ceiling on how many hosts are
    # named per project per window (the busiest ones), the rest sharing one
    # "(other)" row.
    host_metrics_enabled: bool = Field(
        default=True,
        description="Count requests per destination host and connector (the Hosts page)",
    )
    host_metrics_max_hosts: int = Field(
        default=0,
        ge=0,
        description=(
            "How many hosts one gateway names per project in each 5-second metrics window, "
            "busiest first; the rest are folded into \"(other)\". 0 keeps every host"
        ),
    )

    # The per-project failure log (the Failures page): why requests failed.
    # Every failure is counted (the failure_metrics history); examples are
    # kept in one capped Redis stream per project and kind. A burst of
    # identical failures is folded into one example, and past the per-window
    # cap failures are only counted.
    failure_log_enabled: bool = Field(
        default=True,
        description="Record why requests failed or were refused, per project (the Failures page)",
    )
    failure_log_max_entries: int = Field(
        default=1000,
        ge=10,
        description="How many examples the failure log keeps of each kind of failure, per project, newest first",
    )
    failure_log_retention_hours: int = Field(
        default=168,
        ge=1,
        description="How long failure log examples are kept, in hours (the counts follow the metrics retention)",
    )
    failure_log_max_per_window: int = Field(
        default=100,
        ge=1,
        description=(
            "How many distinct examples of each kind one gateway logs per project in each 5-second window; "
            "beyond that failures are only counted"
        ),
    )

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

    @field_validator("proxy_public_url")
    @classmethod
    def _check_proxy_public_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v:
            return v
        parts = urlsplit(v)
        try:
            port = parts.port
        except ValueError:
            port = 0
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or port == 0
            or any(ch.isspace() or ch == "\\" for ch in v)
            or parts.username is not None
            # Nothing after the host and port: no path, and no query or fragment, not even an empty one.
            or v[len(parts.scheme) + 3 :] != parts.netloc
        ):
            raise ValueError(
                "proxy_public_url is http:// or https://, a host and an optional port, such as https://proxy.example.com"
            )
        return v

    @field_validator("worker_jobs", mode="before")
    @classmethod
    def _split_worker_jobs(cls, v: Any) -> Any:
        # The environment holds "health,sync" (NoDecode keeps pydantic-settings
        # from parsing it as JSON); YAML and keyword arguments hold a list.
        if isinstance(v, str):
            return [kind.strip() for kind in v.split(",") if kind.strip()]
        return v

    @field_validator("worker_jobs")
    @classmethod
    def _dedupe_worker_jobs(cls, v: list[JobKind]) -> list[JobKind]:
        # Named twice, a kind still gets one runner.
        return list(dict.fromkeys(v))

    @field_validator("internal_api_url")
    @classmethod
    def _check_internal_api_url(cls, v: str) -> str:
        # Paths are appended to it, so a trailing slash would double up.
        v = v.strip().rstrip("/")
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("internal_api_url is http:// or https:// and a host, such as http://octoprox-api:8000")
        return v

    @field_validator("default_strategy")
    @classmethod
    def _check_default_strategy(cls, v: str) -> str:
        # Imported here, once this module's names exist, so the strategies' modules may import them.
        from api.strategies import strategy_name

        return strategy_name(v)

    @model_validator(mode="after")
    def _name_database_sessions(self) -> "Settings":
        if not self.db_application_name:
            self.db_application_name = f"octoprox-{self.role}"
        return self

    @property
    def proxy_endpoint(self) -> tuple[Literal["http", "https"], str | None, int | None]:
        """``(scheme, host, port)`` clients reach the proxy at.

        ``host`` None means the host the browser reached the web UI at;
        ``port`` None means the scheme's default, which URLs then leave out.
        """
        if self.proxy_public_url:
            parts = urlsplit(self.proxy_public_url)
            return "https" if parts.scheme == "https" else "http", parts.hostname, parts.port
        return "http", None, self.proxy_port

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
    # How many proxies one worker probes at once. Each check holds a connection
    # through its proxy for up to health_check_timeout, so this bounds the
    # sockets one worker opens when a large pool falls due together.
    health_check_concurrency: int = Field(
        default=50, ge=1, description="How many health checks one worker runs at once"
    )
    connection_timeout: int = Field(default=30)
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
        description=(
            "Directory where this process caches IP database files "
            "(fetched from Postgres, or from the api on a gateway)"
        ),
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
        default=5.0, description="How often buffered IP observations are appended to the observations stream, in seconds"
    )
    geo_observation_max_buffer: int = Field(
        default=5000, description="Observations one process holds in memory before dropping the oldest"
    )
    geo_updater_interval: float = Field(
        default=3600.0,
        description=(
            "How often a worker checks for scheduled IP database downloads, in seconds: the controller publishes "
            "an IP database update job this often"
        ),
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

    # WireGuard endpoint (see docs/wireguard.md). The key pair, public endpoint
    # and subnet are install-wide and live in Postgres; these describe this
    # process: whether it terminates the tunnel, and on what.
    wireguard_enabled: bool = Field(
        default=False,
        description="Terminate the WireGuard tunnel on this gateway (needs CAP_NET_ADMIN, wireguard-tools and nftables)",
    )
    wireguard_interface: str = Field(default="wg0", description="Name of the tunnel interface this gateway creates")
    wireguard_listen_port: int | None = Field(
        default=None,
        description="UDP port to listen on when it differs from the install's endpoint port (behind a port-mapping NAT)",
    )
    wireguard_mtu: int = Field(default=1420, ge=1280, le=1500, description="MTU of the tunnel interface")
    wireguard_defaults: dict[str, Any] = Field(
        default_factory=dict,
        description="Seed for the install-wide WireGuard settings row on a fresh install: endpoint_host, endpoint_port, subnet, persistent_keepalive, client_mtu",
    )

    # OpenVPN endpoint (see docs/openvpn.md). The CA, server certificate, public
    # endpoint, transport and subnet are install-wide and live in Postgres;
    # these describe this process: whether it runs the daemon, and on what.
    openvpn_enabled: bool = Field(
        default=False,
        description="Run the OpenVPN daemon on this gateway (needs CAP_NET_ADMIN, /dev/net/tun, openvpn and nftables)",
    )
    openvpn_interface: str = Field(default="ovpn0", description="Name of the tun interface the daemon creates")
    openvpn_listen_port: int | None = Field(
        default=None,
        description="Port to listen on when it differs from the install's endpoint port (behind a port-mapping NAT)",
    )
    openvpn_mtu: int = Field(default=1500, ge=1280, le=1500, description="tun-mtu of the daemon's interface")
    openvpn_defaults: dict[str, Any] = Field(
        default_factory=dict,
        description="Seed for the install-wide OpenVPN settings row on a fresh install: endpoint_host, endpoint_port, protocol, subnet, keepalive_interval, keepalive_timeout, client_mtu",
    )

    # The tunnel data plane every tunnel protocol shares (see docs/wireguard.md):
    # the listeners tunnel traffic is redirected to and how destination names
    # are recovered. Per process; a tunnel's own settings sit with its protocol.
    tunnel_transparent_port: int = Field(
        default=8081, description="Local TCP port tunnel connections are redirected to (bound on the tunnel gateway addresses only)"
    )
    tunnel_dns_port: int = Field(
        default=5353, description="Local port of the tunnel's fake-IP DNS resolver (port 53 on the tunnel is redirected to it)"
    )
    tunnel_fake_ip_range: str = Field(
        default="198.18.0.0/15", description="IPv4 range the tunnel DNS answers from; connections to it carry the name"
    )
    tunnel_sniff_timeout_seconds: float = Field(
        default=2.0, gt=0, description="How long to wait for a connection's first bytes before routing it by address alone"
    )
    tunnel_block_encrypted_dns: bool = Field(
        default=True,
        description="Close tunnel connections to DNS-over-TLS (port 853) and known DNS-over-HTTPS resolvers so devices keep using the tunnel resolver",
    )

    # Authentication settings
    public_url: str = Field(
        default="",
        description=(
            "The URL users reach this install at (scheme and host, no trailing slash), as written into "
            "invite links and registered with sign-in providers as the redirect URI. Behind a load "
            "balancer or reverse proxy the request's own Host header names the wrong place; empty "
            "means use it anyway"
        ),
    )
    auth_username: str = Field(default="admin", description="Admin username for initial seed")
    auth_password: str = Field(default="", description="Admin password for initial seed")
    jwt_secret: str = Field(
        default="change-me-in-production",
        description="Secret key for JWT token signing"
    )
    jwt_expiry_hours: int = Field(default=24, description="JWT token expiry in hours")
    invite_token_expiry_hours: int = Field(default=168, description="Invite token expiry in hours (default 7 days)")

    # Traffic between the roles. internal_secret keys the routing feed in Redis
    # (its encryption and digests) and the token gateways present to the api's
    # internal endpoints. The api, gateways and controllers (and role all) need
    # the same value; workers and echo never read it. Gateways get it instead
    # of jwt_secret, so a gateway cannot mint a sign-in. Like jwt_secret it
    # belongs in the environment, not in a config file.
    internal_secret: str = Field(
        default="change-me-in-production",
        description=(
            "Secret the api, gateways and controllers share: it encrypts the routing feed and signs "
            "gateway calls to the api"
        ),
    )
    # Gateways have no Postgres: they fetch IP database files from the api's
    # internal endpoint at this address.
    internal_api_url: str = Field(
        default="http://localhost:8000",
        description="Where gateways reach the api, such as http://octoprox-api:8000; the default suits only role all",
    )

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
        """The settings sources, highest priority first: arguments, ``OCTOPROX_*`` variables, the ``.env`` file, the YAML file.

        Pydantic fills what none of them sets with its default.
        """
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
        )


@lru_cache
def get_settings() -> Settings:
    """The process's settings, built once, from the sources :meth:`Settings.settings_customise_sources` orders.

    ``OCTOPROX_ENV`` names the YAML file: ``config/dev.yaml`` for ``development`` (the default), ``config/prod.yaml`` for
    ``production``, ``config/<name>.yaml`` for any other.
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

