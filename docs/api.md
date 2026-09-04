---
layout: docs
title: API Reference
nav_id: api
---

# API Reference

<p class="subtitle">Complete REST API documentation for managing projects, credentials, connectors, and proxies.</p>

## Base URL

All API endpoints are prefixed with `/api/v1`. For example:

```
http://localhost:8000/api/v1/projects
```

## Authentication

All API endpoints except `/api/v1/auth/*` and `/health` require a valid JWT token. Mutation endpoints require at least Editor role; user management requires Admin role.

### Login

```bash
POST /api/v1/auth/login
Content-Type: application/json

{
  "username": "admin",
  "password": "your-password"
}
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 86400
}
```

### Using the Token

Include the token in the `Authorization` header:

```bash
curl -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..." \
  http://localhost:8000/api/v1/projects
```

### Check Auth Status

```bash
GET /api/v1/auth/status
```

---

## Projects

Projects provide multi-tenancy support. Each project has its own credentials, connectors, and proxy pools.

### List Projects

```bash
GET /api/v1/projects
```

### Create Project

```bash
POST /api/v1/projects
Content-Type: application/json

{
  "name": "My Project",
  "description": "Production proxy pool",
  "username": "proxy-user",
  "password": "proxy-password",
  "routing_strategy": "round_robin"
}
```

### Get Project

```bash
GET /api/v1/projects/{project_id}
```

### Update Project

```bash
PATCH /api/v1/projects/{project_id}
Content-Type: application/json

{
  "name": "Updated Name",
  "routing_strategy": "least_used"
}
```

### Delete Project

```bash
DELETE /api/v1/projects/{project_id}
```

---

## Credentials

Credentials store cloud provider authentication details.

### List Credentials

```bash
GET /api/v1/projects/{project_id}/credentials
```

### Create Credential

```bash
POST /api/v1/projects/{project_id}/credentials
Content-Type: application/json

{
  "name": "AWS Production",
  "type": "aws",
  "config": {
    "access_key": "AKIAIOSFODNN7EXAMPLE",
    "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
  }
}
```

**Credential Types:** `aws`, `gcp`, `azure`, `static`

### Get Credential

```bash
GET /api/v1/projects/{project_id}/credentials/{credential_id}
```

### Update Credential

```bash
PATCH /api/v1/projects/{project_id}/credentials/{credential_id}
```

### Delete Credential

```bash
DELETE /api/v1/projects/{project_id}/credentials/{credential_id}
```

---

## Connectors

Connectors define how proxies are provisioned (cloud instances or static).

### List Connectors

```bash
GET /api/v1/projects/{project_id}/connectors
```

### Create Connector

```bash
POST /api/v1/projects/{project_id}/connectors
Content-Type: application/json

{
  "name": "AWS US-East Proxies",
  "credential_id": "credential-uuid",
  "config": {
    "instance_name": "octoprox-proxy",
    "region": "us-east-1",
    "instance_type": "t3.micro",
    "security_group": "sg-0123456789abcdef0",
    "min_proxies": 1,
    "max_proxies": 10
  },
  "routing_config": {
    "domain_whitelist": ["example.com", "api.example.com"]
  },
  "rate_limit_config": {
    "max_requests": 100,
    "window_seconds": 60,
    "quarantine_seconds_min": 120,
    "quarantine_seconds_max": 300
  }
}
```

#### Domain Filtering (routing_config)

Connectors support optional domain-based filtering to control which target domains their proxies serve. The `routing_config` field accepts:

- **`domain_whitelist`** — Only route requests for these domains through this connector's proxies.
- **`domain_blacklist`** — Route all requests *except* these domains through this connector's proxies.

Whitelist and blacklist are mutually exclusive — you can set one or the other, but not both.

Domain matching is hierarchical: entering `bing.com` matches `bing.com` and all subdomains (`www.bing.com`, `images.bing.com`, etc.).

**Examples:**

Whitelist — only allow specific domains:
```json
{
  "routing_config": {
    "domain_whitelist": ["example.com", "api.example.com"]
  }
}
```

Blacklist — block specific domains:
```json
{
  "routing_config": {
    "domain_blacklist": ["blocked.com", "ads.tracker.net"]
  }
}
```

No restrictions (default):
```json
{
  "routing_config": {}
}
```

#### Rate Limiting (rate_limit_config)

Connectors support optional per-proxy rate limiting. When a proxy exceeds `max_requests` within `window_seconds`, it is quarantined (excluded from selection) for a random duration between `quarantine_seconds_min` and `quarantine_seconds_max`. See the [Rate Limiting]({{ site.baseurl }}/rate-limiting) guide for details.

| Field | Type | Description |
|-------|------|-------------|
| `max_requests` | integer | Max requests per proxy in the window |
| `window_seconds` | integer | Sliding window duration (1–86400) |
| `quarantine_seconds_min` | integer | Min quarantine duration (1–86400) |
| `quarantine_seconds_max` | integer | Max quarantine duration (1–86400) |
| `sticky_quarantine` | boolean | Block sticky session fallback (default: `false`) |

**Example:**
```json
{
  "rate_limit_config": {
    "max_requests": 100,
    "window_seconds": 60,
    "quarantine_seconds_min": 120,
    "quarantine_seconds_max": 300,
    "sticky_quarantine": false
  }
}
```

Disabled (default):
```json
{
  "rate_limit_config": {}
}
```

#### Unquarantine Proxy

Forcefully remove a proxy from quarantine:

```bash
POST /api/v1/projects/{project_id}/proxies/{proxy_id}/unquarantine
```

Returns `200` with `{"status": "ok", "proxy_id": "..."}` on success, `400` if the proxy is not quarantined.

### Get Connector Options

Get available regions, instance types, and other options for each cloud provider:

```bash
GET /api/v1/connector-options
```

### Get Connector

```bash
GET /api/v1/projects/{project_id}/connectors/{connector_id}
```

### Update Connector

```bash
PATCH /api/v1/projects/{project_id}/connectors/{connector_id}
Content-Type: application/json

{
  "name": "Updated Name",
  "routing_config": {
    "domain_whitelist": ["new-domain.com"]
  }
}
```

### Delete Connector

```bash
DELETE /api/v1/projects/{project_id}/connectors/{connector_id}
```

---

## Proxies

Proxies are the actual proxy servers managed by Octoprox.

### List Proxies

```bash
GET /api/v1/projects/{project_id}/proxies
```

### Create Proxy (Static)

```bash
POST /api/v1/projects/{project_id}/proxies
Content-Type: application/json

{
  "connector_id": "connector-uuid",
  "host": "192.168.1.100",
  "port": 3128,
  "protocol": "http",
  "username": "proxy-user",
  "password": "proxy-pass"
}
```

**Protocols:** `http`, `https`, `socks4`, `socks5`

### Upload Proxies (Bulk)

Upload multiple proxies from a CSV file:

```bash
POST /api/v1/projects/{project_id}/proxies/upload
Content-Type: multipart/form-data

file: proxies.csv
connector_id: connector-uuid
```

CSV format (one proxy per line):
```
http://192.168.1.1:8080
socks5://user:pass@10.0.0.1:1080
```

### Get Proxy

```bash
GET /api/v1/projects/{project_id}/proxies/{proxy_id}
```

### Delete Proxy

```bash
DELETE /api/v1/projects/{project_id}/proxies/{proxy_id}
```

---

## Metrics

### Get Project Metrics

```bash
GET /api/v1/projects/{project_id}/metrics
```

**Response:**
```json
{
  "total_proxies": 10,
  "healthy_proxies": 8,
  "total_requests": 15420,
  "success_rate": 0.98,
  "avg_latency_ms": 145.2
}
```

### Prometheus Metrics

Export metrics in Prometheus format:

```bash
GET /api/v1/projects/{project_id}/metrics/prometheus
```

---

## Backup & Migration

Admin-only endpoints for exporting the entire Octoprox setup to a single
encrypted file and restoring it on the same or another instance. Useful for
disaster recovery and for migrating between deployments.

### Export Backup

```bash
POST /api/v1/backup/export
```

**Request:**
```json
{
  "passphrase": "correct horse battery staple",
  "include_metrics": false
}
```

| Field | Type | Notes |
|-------|------|-------|
| `passphrase` | string | Minimum 8 characters. Required again to import — it cannot be recovered. |
| `include_metrics` | boolean | Include historical proxy/project metrics. Default `false` (smaller file). |

**Response:** the backup file as `application/octet-stream` with a
`Content-Disposition: attachment; filename="octoprox-backup-YYYY-MM-DD.opbak"`
header.

The file covers users (including password hashes), projects, credentials,
connectors, proxies and, optionally, metrics.

### Import Backup

```bash
POST /api/v1/backup/import
Content-Type: multipart/form-data
```

| Form field | Type | Notes |
|------------|------|-------|
| `file` | file | The `.opbak` file produced by export. |
| `passphrase` | string | The passphrase used when exporting. |
| `mode` | string | Only `replace` is supported (default). |
| `keep_current_user` | boolean | Default `false`. See below. |

**Import replaces all existing data** on the instance. The wipe and restore run
in a single transaction, so a failure leaves the existing data untouched.
Afterwards the live proxy cache is rebuilt and stale Redis state for the
replaced projects and proxies is purged.

With `keep_current_user=true` the calling admin's own account survives the
wipe, so an admin importing a backup taken from another instance is not locked
out. Imported users that would collide with the kept account are adjusted:

- same `id` → the imported user receives a fresh id
- same `username` → the imported user is renamed to `<username>-imported`
  (then `-imported-2`, `-imported-3`, … if needed)
- same non-empty `email` → the imported user's email is cleared

With `keep_current_user=false` (the default) users are restored exactly as they
are in the backup; the current session's user may no longer exist, so log in
again with credentials that are valid in the backup.

**Response:**
```json
{
  "users": 3,
  "projects": 2,
  "credentials": 1,
  "connectors": 2,
  "proxies": 14,
  "proxy_metrics": 0,
  "project_metrics": 0,
  "kept_current_user": true,
  "user_conflicts": [
    {
      "original_username": "admin",
      "new_username": "admin-imported",
      "new_id": true,
      "email_cleared": false
    }
  ]
}
```

`users` counts imported rows only; the kept account is not included.

**Errors (HTTP 400):**

- Incorrect passphrase or corrupt file.
- File is not an Octoprox backup.
- Backup was created by a newer Octoprox (`format_version` too high).
- Schema mismatch: the backup's Alembic revision differs from this instance's.
  Upgrade both instances to the same Octoprox version, then retry. This check
  runs before decryption, so it does not require the passphrase.
- `keep_current_user=true` but the caller's account has no database row.

### Backup File Format

An `.opbak` file is a small JSON envelope. The metadata is plain text so an
importer can check compatibility before asking for the passphrase; the data is
encrypted:

```json
{
  "format": "octoprox-backup",
  "format_version": 1,
  "created_at": "2026-09-04T10:15:00+00:00",
  "app_version": "1.0.1",
  "schema_version": "<alembic revision>",
  "includes_metrics": false,
  "kdf": { "algo": "pbkdf2-sha256", "iterations": 600000, "salt": "<base64>" },
  "ciphertext": "<base64 Fernet token>"
}
```

The ciphertext is gzipped JSON encrypted with Fernet (AES-128-CBC + HMAC-SHA256).
The key is derived from the passphrase with PBKDF2-HMAC-SHA256 and a random
per-file salt. Treat the file as sensitive: it contains password hashes and
provider credentials, protected only by the passphrase.

---

## Health Check

Public endpoint for load balancer health checks:

```bash
GET /health
```

**Response:**
```json
{
  "status": "healthy"
}
```

