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

When authentication is enabled (`OCTOPROX_AUTH_ENABLED=true`), all API endpoints except `/api/v1/auth/*` and `/health` require a valid JWT token.

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

