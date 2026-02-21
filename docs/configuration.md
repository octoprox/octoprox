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
| `OCTOPROX_AUTH_ENABLED` | Enable authentication for the web UI and API | false |
| `OCTOPROX_AUTH_USERNAME` | Login username (required if auth enabled) | admin |
| `OCTOPROX_AUTH_PASSWORD` | Login password (required if auth enabled) | (empty) |
| `OCTOPROX_JWT_SECRET` | Secret key for JWT token signing | change-me-in-production |
| `OCTOPROX_JWT_EXPIRY_HOURS` | JWT token expiry in hours | 24 |

## Authentication

Octoprox supports optional authentication to protect the web UI and API endpoints. When enabled, users must log in with a username and password to access the dashboard and API.

### Enabling Authentication

Set the following environment variables to enable authentication:

```bash
export OCTOPROX_AUTH_ENABLED=true
export OCTOPROX_AUTH_USERNAME=admin
export OCTOPROX_AUTH_PASSWORD=your-secure-password
export OCTOPROX_JWT_SECRET=your-random-secret-key
```

Or create a `.env` file in the project root:

```env
OCTOPROX_AUTH_ENABLED=true
OCTOPROX_AUTH_USERNAME=admin
OCTOPROX_AUTH_PASSWORD=your-secure-password
OCTOPROX_JWT_SECRET=your-random-secret-key
```

### Security Notes

- **Always set a strong `OCTOPROX_JWT_SECRET`** in production. The default value is insecure.
- **Never commit credentials** to version control. Use environment variables or `.env` files (which should be gitignored).
- The `/api/v1/auth/login` and `/api/v1/auth/status` endpoints are always public.
- The `/health` endpoint is always public for load balancer health checks.
- All other API endpoints require authentication when `OCTOPROX_AUTH_ENABLED=true`.

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

## Routing Strategies

Octoprox supports multiple routing strategies for distributing requests across proxy pools:

| Strategy | Description |
|----------|-------------|
| `round_robin` | Distributes requests evenly across all healthy proxies in order |
| `least_used` | Routes to the proxy with the fewest active connections |
| `random` | Randomly selects a healthy proxy for each request |
| `sticky` | Routes requests from the same client to the same proxy |
| `health_based` | Prioritizes proxies with better health scores and lower latency |

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

3. **Enable authentication**: Set `OCTOPROX_AUTH_ENABLED=true` in production.

4. **Use HTTPS**: Deploy behind a reverse proxy (nginx, Caddy) with TLS termination.

5. **Monitor health**: Use the `/health` endpoint for load balancer health checks.

