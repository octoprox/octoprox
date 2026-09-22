---
layout: docs
title: Configuration
nav_id: configuration
---

# Configuration

<p class="subtitle">Configure Octoprox using environment variables and YAML configuration files.</p>

## Overview

Configuration is loaded from YAML files in the `config/` directory based on the `OCTOPROX_ENV` environment variable. Environment variables take precedence over YAML configuration.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OCTOPROX_ENV` | Environment (development/production) | development |
| `OCTOPROX_REDIS_URL` | Redis connection URL | redis://localhost:6379/0 |
| `OCTOPROX_LOG_LEVEL` | Logging level | INFO |
| `OCTOPROX_AUTH_USERNAME` | Initial admin username (used to seed admin on first startup) | admin |
| `OCTOPROX_AUTH_PASSWORD` | Initial admin password (required) | (empty) |
| `OCTOPROX_JWT_SECRET` | Secret key for JWT token signing | change-me-in-production |
| `OCTOPROX_JWT_EXPIRY_HOURS` | JWT token expiry in hours | 24 |
| `OCTOPROX_TLS_MITM_CA_CERT_PATH` | Path to the MITM CA certificate | data/ca/octoprox-ca.crt |
| `OCTOPROX_TLS_MITM_CA_KEY_PATH` | Path to the MITM CA private key | data/ca/octoprox-ca.key |
| `OCTOPROX_PROVIDERS_DIR` | Directory of operator-supplied provider descriptor YAML files | (unset) |
| `OCTOPROX_PROVIDER_EGRESS_ALLOW_HTTP` | Allow descriptors to call plain-http vendor APIs (development only) | false |
| `OCTOPROX_PROVIDER_EGRESS_ALLOW_PRIVATE` | Allow descriptor API calls to private addresses (development only) | false |
| `OCTOPROX_PROVIDER_HTTP_TIMEOUT_SECONDS` | Timeout for a vendor API request made for a descriptor | 60 |
| `OCTOPROX_PROVIDER_HTTP_MAX_RESPONSE_BYTES` | Largest vendor API response accepted (0 disables) | 52428800 |

## Authentication

Octoprox requires authentication for all web UI and API access. On first startup, an admin user is automatically created from `OCTOPROX_AUTH_USERNAME` and `OCTOPROX_AUTH_PASSWORD`.

There are three roles:
- **Admin** - Full access including user management
- **Editor** - Can manage projects, proxies, credentials, and connectors (no user management)
- **Viewer** - Read-only access to all data

### Configuration

Set the following environment variables:

```bash
export OCTOPROX_AUTH_USERNAME=admin
export OCTOPROX_AUTH_PASSWORD=your-secure-password
export OCTOPROX_JWT_SECRET=your-random-secret-key
```

Or create a `.env` file in the project root:

```env
OCTOPROX_AUTH_USERNAME=admin
OCTOPROX_AUTH_PASSWORD=your-secure-password
OCTOPROX_JWT_SECRET=your-random-secret-key
```

### Security Notes

- **Always set a strong `OCTOPROX_JWT_SECRET`** in production. The default value is insecure.
- **Never commit credentials** to version control. Use environment variables or `.env` files (which should be gitignored).
- The `/api/v1/auth/login` and `/api/v1/auth/status` endpoints are always public.
- The `/health` endpoint is always public for load balancer health checks.
- All other API endpoints require authentication.

## YAML Configuration

### Example Configuration File

Create a configuration file at `config/development.yaml` or `config/production.yaml`:

```yaml
server:
  host: "0.0.0.0"
  api_port: 8000
  proxy_port: 8080

proxy:
  default_strategy: round_robin
  health_check:
    enabled: true
    interval_seconds: 60
    timeout_seconds: 30

tls_mitm:
  ca_cert_path: "data/ca/octoprox-ca.crt"
  ca_key_path: "data/ca/octoprox-ca.key"
```

### Server Configuration

| Option | Description | Default |
|--------|-------------|---------|
| `server.host` | Host address to bind to | 0.0.0.0 |
| `server.api_port` | Port for the REST API and web UI | 8000 |
| `server.proxy_port` | Port for the proxy server | 8080 |

### Proxy Configuration

| Option | Description | Default |
|--------|-------------|---------|
| `proxy.default_strategy` | Default routing strategy | round_robin |
| `proxy.health_check.enabled` | Enable automatic health checks | true |
| `proxy.health_check.interval_seconds` | Interval between health checks | 60 |
| `proxy.health_check.timeout_seconds` | Timeout for health check requests | 30 |
| `proxy.geo_lookup.enabled` | Look up the exit IP and country of static proxies when they are added (one request through the proxy) | true |
| `proxy.geo_lookup.url` | JSON endpoint requested through the proxy. Also seeds the IP attribution echo endpoint; point it at an Octoprox `/echo` in production. | https://httpbin.org/ip |
| `proxy.geo_lookup.ip_path` | JMESPath to the IP in the response | origin |
| `proxy.geo_lookup.country_path` | JMESPath to the ISO country code in the response; empty when the endpoint reports none | (empty) |
| `proxy.geo_lookup.timeout_seconds` | Timeout for one lookup request | 15 |

The `proxy.geo_lookup` endpoint seeds the echo endpoint of the [IP attribution]({{ site.baseurl }}/ip-attribution) policy on a fresh install; once an admin saves the policy in the UI, the policy's `echo_url` is what is used.

### IP Attribution

The install-wide settings (default source precedence and conflict rule, echo endpoint, preflight, retention) are edited in Settings → IP attribution and stored in the database; projects may override the source precedence and conflict rule. The config file describes this process and seeds a fresh install:

| Option | Description | Default |
|--------|-------------|---------|
| `geo.cache_dir` | Where this instance caches IP database files fetched from Postgres | data/geo |
| `geo.databases` | Operator-managed database files: list of `{path, name, priority, enabled}` entries loaded at startup | [] |
| `geo.defaults` | Initial settings for a fresh install: `default_sources`, `default_conflict_rule`, `echo_url`, `echo_ip_path`, `echo_country_path`, `echo_timeout_seconds`, `health_check_attribution`, `preflight_session_ttl_seconds`, `preflight_max_attempts`, `observation_retention_days`, `exit_ip_retention_days` | built-in |
| `geo.echo.enabled` | Serve the public `/echo` endpoint on this instance | true |
| `geo.echo.trusted_proxies` | CIDRs of load balancers whose `X-Forwarded-For` the `/echo` endpoint trusts for the client IP | [] |
| `geo.observations.publish_interval_seconds` | How often buffered IP observations are pushed to Redis | 5 |
| `geo.observations.flush_interval_seconds` | How often the leader drains the Redis queue into Postgres | 30 |
| `geo.observations.max_buffer` | Observations one instance keeps in memory before dropping the oldest | 5000 |
| `geo.updater.check_interval_seconds` | How often the leader checks for scheduled database downloads | 3600 |

Connector config keys read by the health checker: `healthcheck_url` (custom check URL; without it the attribution echo endpoint is checked), `healthcheck_ip_path` (JMESPath, or `@text`, of the exit IP in that URL's response; unset means the response carries no IP unless the URL is the echo or httpbin endpoint) and `healthcheck_country_path`.

## Database Configuration

Octoprox uses PostgreSQL for persistent storage:

| Variable | Description | Default |
|----------|-------------|---------|
| `OCTOPROX_DB_HOST` | PostgreSQL host | localhost |
| `OCTOPROX_DB_PORT` | PostgreSQL port | 5432 |
| `OCTOPROX_DB_NAME` | Database name | octoprox |
| `OCTOPROX_DB_USER` | Database user | octoprox |
| `OCTOPROX_DB_PASSWORD` | Database password | (required) |

## Redis Configuration

Redis is used for session storage and caching:

| Variable | Description | Default |
|----------|-------------|---------|
| `OCTOPROX_REDIS_URL` | Full Redis connection URL | redis://localhost:6379/0 |

## Production Recommendations

1. **Set strong secrets**: Always use secure, random values for `OCTOPROX_JWT_SECRET` and `OCTOPROX_AUTH_PASSWORD`.

2. **Use environment variables**: Don't hardcode sensitive values in configuration files.

3. **Set a strong admin password**: Use a secure value for `OCTOPROX_AUTH_PASSWORD`.

4. **Use HTTPS**: Deploy behind a reverse proxy (nginx, Caddy) with TLS termination.

5. **Monitor health**: Use the `/health` endpoint for load balancer health checks.

