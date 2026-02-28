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
| `OCTOPROX_TLS_MITM_CA_CERT_PATH` | Path to the MITM CA certificate | data/ca/octoprox-ca.crt |
| `OCTOPROX_TLS_MITM_CA_KEY_PATH` | Path to the MITM CA private key | data/ca/octoprox-ca.key |

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

## Routing Strategies

Octoprox supports multiple routing strategies for distributing requests across proxy pools:

| Strategy | Description |
|----------|-------------|
| `round_robin` | Distributes requests evenly across all healthy proxies in order |
| `least_used` | Routes to the proxy with the fewest active connections |
| `random` | Randomly selects a healthy proxy for each request |
| `sticky` | Routes requests from the same client to the same proxy |
| `health_based` | Prioritizes proxies with better health scores and lower latency |

### Session IDs

When using the `sticky` routing strategy, you can control session affinity by embedding a session ID in the proxy authentication username. This allows you to group requests under a specific session, ensuring they are all routed to the same upstream proxy.

**Format:** `<username>-sessid-<session_id>`

**Examples:**

```
# Without session ID — uses client IP for session affinity
Proxy-Authorization: Basic base64(myuser:password)

# With session ID — uses "order-123" for session affinity
Proxy-Authorization: Basic base64(myuser-sessid-order-123:password)

# Hyphenated username works too
Proxy-Authorization: Basic base64(my-project-sessid-abc456:password)
```

**Behavior:**

- The `-sessid-` delimiter separates the real username from the session ID. The password remains unchanged.
- When a session ID is provided, it replaces the client IP as the session identifier for the sticky strategy.
- If the upstream proxy assigned to a session becomes unhealthy (e.g., IP rotation), a new proxy is automatically assigned on the next request.
- Without a session ID, the sticky strategy falls back to using the client IP address, which is the default behavior.
- This feature only takes effect when the project's routing strategy is set to `sticky`. Other strategies ignore the session ID.

> **Note:** The string `-sessid-` is a reserved delimiter and should not appear in your project username.

## Domain Filtering

Connectors support optional domain-based filtering to control which target domains their proxies serve. This is configured per-connector via the `routing_config` field.

- **Whitelist mode** — Only requests for the listed domains are routed through the connector.
- **Blacklist mode** — All requests *except* those for the listed domains are routed through the connector.

Domain matching is hierarchical: `bing.com` matches `bing.com` and all subdomains (`www.bing.com`, `images.bing.com`, etc.).

Connectors with no domain filtering rules (the default) allow all domains. See the [API Reference]({{ site.baseurl }}/api#domain-filtering-routing_config) for configuration details.

## TLS Interception (MITM)

Octoprox can decrypt and re-encrypt HTTPS traffic between your client and the target server. This enables header inspection, User-Agent overriding, and browser-grade TLS fingerprint impersonation. TLS interception is configured per-project from the web UI.

### Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| **Disabled** | Traffic forwarded through an encrypted tunnel as-is. The proxy cannot inspect headers or content. Your client's TLS fingerprint, User-Agent, and all headers reach the target unchanged. | You handle anti-detection yourself. |
| **Plain** | Decrypts HTTPS to inspect headers, then re-encrypts using Python's standard TLS library. The target sees a Python/OpenSSL fingerprint — easily detectable by anti-bot systems. | Debugging and development only. |
| **Browser Match** | Decrypts HTTPS for inspection, then re-encrypts using a browser-grade TLS engine that matches the client's User-Agent. If your client sends a Chrome User-Agent, the target sees a Chrome TLS fingerprint. | Production scraping with basic anti-detection. |
| **Browser Override** | Same as Browser Match, but also replaces the client's User-Agent with the selected browser's default. TLS fingerprint and User-Agent are guaranteed consistent. | Maximum anti-detection when you don't need to control the User-Agent. |

### TLS Engines

When using Browser Match or Browser Override mode, you can choose between two TLS engines:

| Engine | Description |
|--------|-------------|
| **curl_cffi** | C/libcurl with BoringSSL. Mature, Chrome-grade fingerprints. |
| **rnet** | Rust/BoringSSL. Fast, 113+ browser profiles. |

### Browser Profiles

When using Browser Override mode, you choose which browser to impersonate:

| Profile | Description |
|---------|-------------|
| **Chrome** | Most common browser fingerprint, lowest detection risk. |
| **Firefox** | Alternative fingerprint for diversity. |
| **Safari** | macOS/iOS fingerprint. |
| **Edge** | Chromium-based, Windows-like fingerprint. |
| **Random** | Randomly selects a different browser profile for each request. Useful for fingerprint diversity across a large volume of requests. |

In Browser Match mode, the browser profile is automatically detected from the client's User-Agent header. If the User-Agent doesn't match any known browser, the engine defaults to Chrome and replaces the unrecognized User-Agent with Chrome's default.

### How It Works

TLS interception inherently creates two separate TLS sessions:

1. **Client &rarr; Proxy**: Your client connects to the proxy via TLS. The proxy presents a dynamically generated certificate for the target domain, signed by the Octoprox CA.
2. **Proxy &rarr; Target**: The proxy opens a new TLS connection to the target server using the selected engine. The target sees the engine's TLS fingerprint, not your client's.

This means the upstream TLS fingerprint (JA3/JA4) is always determined by the relay engine, never by your client. Your client's original fingerprint is not and cannot be passed through.

### Protocol Support

When MITM is **disabled**, Octoprox creates a raw TCP tunnel (`CONNECT`) and is completely protocol-agnostic — HTTP/2, HTTP/3 (QUIC), WebSocket, and any other protocol work transparently.

When MITM is **enabled**, the proxy terminates TLS and parses traffic itself. The current implementation uses [h11](https://h11.readthedocs.io/) on the client-facing side, which limits interception to **HTTP/1.1** only. On the relay side, the impersonation engines (`curl_cffi`, `rnet`) negotiate HTTP/2 with the target server automatically.

WebSocket and HTTP/2 client-side support are planned for a future release.

### CA Certificate

When any MITM mode is enabled, clients must trust the Octoprox CA certificate. Without it, clients will see TLS verification errors.

**Auto-generation**: The CA certificate and private key are automatically generated on first startup if they don't already exist. They are stored at the paths configured by `OCTOPROX_TLS_MITM_CA_CERT_PATH` and `OCTOPROX_TLS_MITM_CA_KEY_PATH` (default: `data/ca/`).

**Download**: The CA certificate can be downloaded from the web UI (shown when editing a project with MITM enabled) or directly at:

```
GET /api/v1/projects/ca-certificate
```

**Install the CA certificate** in your client's trust store:

```bash
# Download the certificate
curl -o octoprox-ca.crt http://localhost:8000/api/v1/projects/ca-certificate

# Python (requests/httpx)
export REQUESTS_CA_BUNDLE=/path/to/octoprox-ca.crt
export SSL_CERT_FILE=/path/to/octoprox-ca.crt

# curl
curl --cacert /path/to/octoprox-ca.crt https://example.com

# Node.js
export NODE_EXTRA_CA_CERTS=/path/to/octoprox-ca.crt

# System-wide (macOS)
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain octoprox-ca.crt

# System-wide (Ubuntu/Debian)
sudo cp octoprox-ca.crt /usr/local/share/ca-certificates/octoprox-ca.crt
sudo update-ca-certificates
```

**Bring your own CA**: To use a custom CA certificate instead of the auto-generated one, place your PEM-encoded certificate and private key at the configured paths before starting the server. The files must be readable by the Octoprox process.

```bash
# Example: provide your own CA
mkdir -p data/ca
cp /path/to/my-ca.crt data/ca/octoprox-ca.crt
cp /path/to/my-ca.key data/ca/octoprox-ca.key
chmod 600 data/ca/octoprox-ca.key
```

Or generate a new CA manually with OpenSSL:

```bash
# Generate a CA private key
openssl genrsa -out data/ca/octoprox-ca.key 2048
chmod 600 data/ca/octoprox-ca.key

# Generate a self-signed CA certificate (valid for 10 years)
openssl req -new -x509 -key data/ca/octoprox-ca.key \
  -out data/ca/octoprox-ca.crt -days 3650 \
  -subj "/O=Octoprox/CN=Octoprox MITM CA"
```

### Docker

In Docker deployments, the CA files are persisted in a named volume (`ca_data`) mounted at `/app/data/ca`. This ensures the CA certificate survives container restarts — clients only need to install it once.

### YAML Configuration

The CA certificate paths can also be set via YAML:

```yaml
tls_mitm:
  ca_cert_path: "data/ca/octoprox-ca.crt"
  ca_key_path: "data/ca/octoprox-ca.key"
```

### Security Notes

- **Never commit CA private keys** to version control. The `.gitignore` excludes `data/` and `*.key` by default.
- The CA private key allows anyone who has it to generate trusted certificates for any domain. Treat it like a password.
- CA certificates are valid for 10 years. Domain certificates generated for individual hosts are valid for 1 year.
- In production, consider using a dedicated CA with restricted access rather than the auto-generated one.

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

