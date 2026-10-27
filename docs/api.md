---
layout: docs
title: "REST API Reference"
nav_id: api
description: "REST API reference for Octoprox: manage projects, credentials, connectors and proxies, with authentication, request IDs and curl examples for every endpoint."
---

# API Reference

<p class="subtitle">Complete REST API documentation for managing projects, credentials, connectors, and proxies.</p>

## Base URL

All API endpoints are prefixed with `/api/v1`. For example:

```
http://localhost:8000/api/v1/projects
```

Request bodies are JSON, and a request that sends one must say so with `Content-Type: application/json`; a JSON body without it is refused with `422`. File uploads (IP databases, proxy lists, backups) are `multipart/form-data`.

## Authentication

All API endpoints except `/api/v1/auth/*` and the ops endpoints (`/live`, `/ready`, `/health`, `/metrics`, see [Health Check](#health-check)) require a bearer token: either a session token from a login, or a personal access token. Mutation endpoints require at least Editor role; user management requires Admin role.

The API is served by the api role (or a single instance); any api process answers any request, a write included, since every one reads the entities from Postgres on each request, and the provider catalog (`GET /api/v1/providers`, `GET /api/v1/providers/{provider_id}`) and the attribution settings (`GET /api/v1/geo/settings`) as well. Each api also keeps the custom provider descriptors, the attribution settings and the IP database list in memory for its other routes (credential checks, previews, lookups); those follow a change made through another api as soon as the change stream announces it.

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

### Personal Access Tokens

A token (`octo_...`) is sent exactly like a session token and acts as its user with the user's current role; revoking it, or disabling the user, takes effect on the next call. Only a hash is stored, so the secret appears in the create response and nowhere else. See [Single Sign-On and API Tokens]({{ site.baseurl }}/sso#personal-access-tokens).

| Method | Path | Description |
|---|---|---|
| GET | `/users/me/tokens` | The caller's tokens: `id`, `name`, `token_prefix`, `expires_at`, `last_used_at`, `expired` |
| POST | `/users/me/tokens` | Create one from `{name, expires_in_days}` (`expires_in_days` null for no expiry). Returns `{token, api_token}`. Refused (403) when the caller is itself a token |
| DELETE | `/users/me/tokens/{id}` | Revoke |
| GET | `/users/{user_id}/tokens` | A user's tokens (admin) |
| POST | `/users/{user_id}/tokens` | Issue a token on a user's behalf (admin); how a service account gets one |
| DELETE | `/users/{user_id}/tokens/{id}` | Revoke (admin) |

```bash
curl -X POST -H "Authorization: Bearer <session token>" -H "Content-Type: application/json" \
  -d '{"name": "ci", "expires_in_days": 90}' http://localhost:8000/api/v1/users/me/tokens
# {"token": "octo_...", "api_token": {"id": "...", "name": "ci", "token_prefix": "octo_abcdefg", ...}}

curl -H "Authorization: Bearer octo_..." http://localhost:8000/api/v1/projects
```

### Sign in with a Provider

The round trip through Google, Microsoft or GitHub is driven by the browser, not by API clients; scripts use a personal access token. These endpoints are public. See [Single Sign-On and API Tokens]({{ site.baseurl }}/sso).

| Method | Path | Description |
|---|---|---|
| GET | `/auth/sso/providers` | The enabled providers, `[{kind, label}]`, for the login page |
| GET | `/auth/sso/{kind}/start` | Redirects the browser to the provider (`kind`: `google`, `microsoft`, `github`) |
| GET | `/auth/sso/{kind}/callback` | Where the provider sends the browser back. Ends at `/sso/complete?code=...` on success, or at `/?sso_error=...` with a reason |
| POST | `/auth/sso/exchange` | Trade the one-time `{code}` from the callback for `{access_token, token_type, expires_in}`. Single use, valid for a minute |

The providers themselves are set up by an admin:

| Method | Path | Description |
|---|---|---|
| GET | `/sso/providers` | Every provider with `enabled`, `client_id`, `has_client_secret`, `tenant_id`, `configured` and the `redirect_uri` to register |
| PUT | `/sso/providers/{kind}` | Save `{enabled, client_id, client_secret, tenant_id}`; a null `client_secret` keeps the stored one. Microsoft requires a single tenant ID |

Linked provider accounts are listed and removed under `/users/me/identities` and `/users/{user_id}/identities` (admin), each with `provider`, `email`, `created_at` and `last_login_at`. User responses carry `sign_in_providers`, the kinds linked to the user.

---


### Request IDs

Every response carries an `X-Request-ID` header. Send your own value in the same request header (up to 128 characters of letters, digits, `.`, `_`, `:` and `-`) to have it used and echoed back; otherwise one is generated. Quote the ID when reporting a failed call, as it appears on every server log line written while handling that request.

```bash
curl -i -H "X-Request-ID: deploy-42-step-3" http://localhost:8000/api/v1/projects
```

### When Postgres or Redis cannot be reached

A route that needs Postgres or Redis while it cannot be reached answers `503` with `Retry-After: 5` and `{"detail": "Postgres cannot be reached; try again shortly"}` (or `Redis`), not a `500`. Retry after the delay.

## Projects

Projects provide multi-tenancy support. Each project has its own credentials, connectors, and proxy pools. A route under `/projects/{project_id}` answers `404` for an id that belongs to another project, and a duplicate name or an unknown `routing_strategy` is a `400`. Changing a project's location rules (`location_contradicted`, `location_unknown`, `location_conflict_rule`, `location_city_radius_km`) queues a re-attribution of its proxies.

`exit_headers` (boolean, default `false`) tells the project's clients which exit IP served each request, in `X-Octoprox-*` headers on the `CONNECT` answer and on plain HTTP responses; see [Exit headers]({{ site.baseurl }}/connecting#exit-headers).

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

### Connect

What a client sends to use the project's proxy for a place and session, and where it would go. This is what the [Connect page]({{ site.baseurl }}/connecting) shows. Every query parameter is optional:

```bash
GET /api/v1/projects/{project_id}/connect?country=us&state=ny&city=New%20York&sessid=job-1
```

The username is composed from the project's and the parameters, then read by the parser the proxy listener uses, so the normalised form and any refusal are exactly what a client would get:

```json
{
  "endpoint": {"scheme": "https", "host": "proxy.example.com", "port": null},
  "username": "myuser-cc-us-st-ny-city-new_york-sessid-job-1",
  "sessid": "job-1",
  "location": {"country": "US", "state": "NY", "city": "new_york"},
  "error": null,
  "routes": [
    {"connector_id": "...", "connector_name": "ISP static", "dynamic": false, "on_demand": false, "proxies": 2, "share": 0.75},
    {"connector_id": "...", "connector_name": "Residential", "dynamic": true, "on_demand": false, "proxies": 1, "share": 0.25}
  ],
  "countries": ["DE", "US"],
  "any_country": false,
  "places": [{"country": "US", "state": "NY", "city": "new_york", "proxies": 2}]
}
```

| Field | Meaning |
|-------|---------|
| `endpoint` | Where clients reach the proxy, from `OCTOPROX_PROXY_PUBLIC_URL`: `scheme` (`http` or `https`), `host`, and `port`, which is `null` when the URL names none (clients use the scheme's default: 443 for `https`, which URLs then leave out, and 80 for `http`, which they should name, since curl reads a bare `http://` proxy as port 1080). Without the setting: `http`, `host` `null` (the host the web UI is reached at) and `server.proxy_port` |
| `username` | The username to send, normalised; as composed when it cannot be read |
| `error` | The proxy's `400` message for the username, or `null`. A value containing a reserved delimiter (or forming one with the dash before or after it), or a session ID with a colon, is refused here too |
| `routes` | The enabled connectors a request with the username would reach now, with the routable proxies of each and the expected share of requests by weight. `dynamic` marks a dynamic-sessions gateway, `on_demand` an "All countries" pool that would create sessions for the country on the first request. Empty means the proxy would refuse the request: `502`, or `429` while the proxies that could serve it are rate limited, or the traffic-limit status (`509` by default) while their connectors are over their limit. No destination is known, so domain filters are not applied |
| `countries` | Countries the project serves: every connector's declared list and the known country of every healthy exit |
| `any_country` | A country outside `countries` is served too: a dynamic-sessions connector or residential pool lists no countries |
| `places` | States and cities healthy fixed exits are known to be in, busiest first, at most 200 |

---

## Credentials

Credentials store cloud provider authentication details.

### List Credentials

```bash
GET /api/v1/projects/{project_id}/credentials
```

### Create Credential

`type` is any provider id from the catalog. The config is validated against the provider's field schema; descriptor providers with a validation call also verify the credential with the vendor and may store captured values (for example Bright Data's customer id). Names are unique per project, ignoring case: a duplicate returns `400`.

```bash
POST /api/v1/projects/{project_id}/credentials
Content-Type: application/json

{
  "name": "AWS Production",
  "type": "aws",
  "config": {
    "auth_mode": "access_keys",
    "access_key": "AKIAIOSFODNN7EXAMPLE",
    "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
  }
}
```

An AWS credential can also carry no secrets at all. With `"auth_mode": "host_role"` Octoprox uses the identity of the host it runs on (EC2 instance profile, ECS task role, EKS pod identity or `AWS_*` environment variables). Either mode may add `role_arn` and an optional `external_id` to assume a role with `sts:AssumeRole` before touching EC2. See the [AWS setup guide](aws-setup) for the IAM side.

```json
{
  "name": "AWS Customer Account",
  "type": "aws",
  "config": {
    "auth_mode": "host_role",
    "role_arn": "arn:aws:iam::123456789012:role/octoprox-proxies",
    "external_id": "acme-7f3b9c"
  }
}
```

GCP and Azure credentials have the same shape. `"auth_mode": "host_identity"` uses the service account, managed identity or workload identity of the host and stores nothing; the defaults `service_account_key` and `client_secret` keep the stored-secret behaviour. GCP may add `impersonate_service_account` in either mode; Azure host mode may add `target_tenant_id` and `target_client_id` for an app registration federated to the host identity. See the [GCP](gcp-setup) and [Azure](azure-setup) setup guides.

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

Connector names are unique per project, ignoring case: a duplicate returns `400`.

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

#### Routing weight (routing_config.weight)

`routing_config.weight` is the connector's relative share of the project's traffic against the other connectors that can serve a request: an integer from 1 to 100, default 1. Weights 1 and 3 split requests 25/75. The share does not depend on how many proxies the connector holds. The default is not stored, so a connector at weight 1 shows no `weight` key. See [Connector weights]({{ site.baseurl }}/routing-strategies#connector-weights).

```json
{
  "routing_config": {
    "weight": 3,
    "domain_blacklist": ["blocked.com"]
  }
}
```

#### Domain Filtering (routing_config)

Connectors support optional domain-based filtering to control which target domains their proxies serve. The `routing_config` field accepts:

- **`domain_whitelist`** - Only route requests for these domains through this connector's proxies.
- **`domain_blacklist`** - Route all requests *except* these domains through this connector's proxies.

Whitelist and blacklist are mutually exclusive - you can set one or the other, but not both.

Domain matching is hierarchical: entering `bing.com` matches `bing.com` and all subdomains (`www.bing.com`, `images.bing.com`, etc.).

**Examples:**

Whitelist - only allow specific domains:
```json
{
  "routing_config": {
    "domain_whitelist": ["example.com", "api.example.com"]
  }
}
```

Blacklist - block specific domains:
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

#### Countries (config)

Connectors declare the countries their proxies exit from inside `config`, so clients can pick them with a `-cc-<code>` suffix on the proxy username (see [Country Routing]({{ site.baseurl }}/routing-strategies#country-routing)). Codes are ISO 3166-1 alpha-2 and are normalised to upper case, with `UK` stored as `GB`; an empty list is dropped.

- Static and cloud connectors: `config.countries`, a list of codes.
- Provider connectors: the provider's country field (`config.country_code` for the built-in descriptors). It accepts one code, a list, or a comma-separated string and is always returned as a list. Listing several countries provisions `num_proxies` slots per country.

```json
{
  "name": "EU residential",
  "credential_id": "…",
  "config": { "num_proxies": 5, "country_code": ["DE", "FR", "NL"] }
}
```

Proxy responses include `country`, the exit country when the vendor reported it or the slot was provisioned for one, and `state_code` and `city`, each with a `state_source` or `city_source` (`manual` when pinned by hand, else the attribution source that resolved it), the place `-st-` and `-city-` requests match the proxy on (see [State and City Routing]({{ site.baseurl }}/routing-strategies#state-and-city-routing)). Connector responses include `target`: the intended pool size (`total`, or null when the connector has none) and how it is derived, `per_country` times the `countries` that have a slot group, with the ones created on demand listed in `on_demand`.

Manually added proxies (static connectors) get their exit location looked up by a worker within seconds of being created: one request goes through the proxy to the `proxy.geo_lookup` endpoint and the discovered IP becomes `display_host` while the country lands in `country`. Pass `country` on `POST .../proxies` to set it yourself and skip the lookup, set `country` to `""` on `PATCH .../proxies/{id}` to clear it, or call `POST .../proxies/{id}/locate` to re-run the lookup and get the updated proxy back (the api makes the request itself, so it needs network access to the proxy; 502 when the request through the proxy fails). `state` (an ISO 3166-2 subdivision part such as `NY`, `ON` or `ENG`, or the full `US-NY`) and `city` (any spelling; stored as a slug) pin the state and city by hand on create and update; an empty string clears each. Pinned values win over the databases for routing and are what attribution verifies the exit against.

#### Rate Limiting (rate_limit_config)

Connectors support optional per-proxy rate limiting. When a proxy exceeds `max_requests` within `window_seconds`, it is quarantined (excluded from selection) for a random duration between `quarantine_seconds_min` and `quarantine_seconds_max`. See the [Rate Limiting]({{ site.baseurl }}/rate-limiting) guide for details.

| Field | Type | Description |
|-------|------|-------------|
| `max_requests` | integer | Max requests per proxy in the window |
| `window_seconds` | integer | Sliding window duration (1-86400) |
| `quarantine_seconds_min` | integer | Min quarantine duration (1-86400) |
| `quarantine_seconds_max` | integer | Max quarantine duration (1-86400) |
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

#### Traffic Limits (traffic_config)

Connectors meter the bytes through their proxies per billing period and can cap them. `traffic_config` holds the period, the limit, what happens at the limit and the price per GB; see the [Traffic Limits & Billing]({{ site.baseurl }}/traffic-limits) guide. Only explicit choices are stored, a key at its default is dropped.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `limit_bytes` | integer | none | Bytes per period before `action` applies; omit to meter only |
| `period` | `day`, `week`, `month` | `month` | How often usage starts over (UTC) |
| `reset_day` | integer | `1` | Day of month (1-28) for `month`, weekday (1 = Monday) for `week` |
| `action` | `alert`, `block`, `interrupt` | `alert` | Flag only, stop new requests, or also cut running transfers |
| `warn_percent` | integer | `80` | Share of the limit at which the connector is flagged |
| `limit_status` | `509` or `429` | `509` | Status when every eligible connector is over its limit |
| `price_per_gb` | number | none | Price per decimal GB (1 GB = 1,000,000,000 bytes) |
| `currency` | string | `USD` | ISO 4217 code |

**Example:**
```json
{
  "traffic_config": {
    "limit_bytes": 500000000000,
    "period": "month",
    "reset_day": 15,
    "action": "block",
    "price_per_gb": 8
  }
}
```

Every connector response carries `traffic_usage`, the usage in the current period: the connector's flushed history since the period started plus what the gateways counted since the last flush, judged with the same rules the traffic job blocks connectors with:

```json
{
  "traffic_usage": {
    "period": "month",
    "period_start": "2026-09-15T00:00:00",
    "period_end": "2026-10-15T00:00:00",
    "bytes_sent": 12000000000,
    "bytes_received": 230000000000,
    "total_bytes": 242000000000,
    "limit_bytes": 500000000000,
    "percent": 48.4,
    "action": "block",
    "status": "ok",
    "blocked": false,
    "pending": false,
    "price_per_gb": 8.0,
    "currency": "USD",
    "cost": 1936.0,
    "reset_at": null
  }
}
```

`status` is `ok`, `warning` (past `warn_percent`) or `exceeded`, judged with the rule the traffic job decides by. `blocked` is the block the traffic job stored, the one the gateways enforce: true while the connector takes no new requests. After a usage reset or a raised limit it stays true until a worker decides again, normally within a second and at most a minute later, and it reads `false` while Redis cannot be read. `pending` is true from a usage reset, or an edit of the limit, the action, the period or the reset day, until a worker stores the decision it asked for; it too reads `false` while Redis cannot be read. `percent` and `cost` are `null` without a limit or a price. `reset_at` is set when usage was reset by hand inside the period; `traffic_reset_at` on the connector holds the same timestamp.

Clients whose request finds every eligible connector over its limit get the configured status (509 by default) with a `Proxy-Status: octoprox; error=bandwidth_limit_exceeded` header.

#### Reset Traffic Usage

Start the connector's usage over from now (editor role). History is kept; the count against the limit restarts, and a traffic decision is asked for, which lifts a block the limit raised:

```bash
POST /api/v1/projects/{project_id}/connectors/{connector_id}/traffic/reset
```

Returns the updated connector. Its usage counts from now, while `traffic_usage.blocked` still shows the stored block, with `pending` true, until a worker has decided (normally within a second; the gateways keep the block until then).

#### Connector Metrics History

The connector's own metrics history, which survives its proxies being rotated or re-synced:

```bash
GET /api/v1/projects/{project_id}/connectors/{connector_id}/metrics/history?range=24h
```

Same `range` values and response shape as [project metrics history](#metrics).

#### Unquarantine Proxy

Forcefully remove a proxy from quarantine:

```bash
POST /api/v1/projects/{project_id}/proxies/{proxy_id}/unquarantine
```

Returns `200` with `{"status": "ok", "proxy_id": "..."}` on success, `400` if the proxy is not quarantined. Every gateway routes to the proxy again within moments, through the routing feed.

### Get Connector Options

Get available regions, instance types, the country list and other static options:

```bash
GET /api/v1/connector-options
```

Provider-specific dynamic options (zones, entry nodes, sub-users) are served by `POST /api/v1/providers/{provider_id}/options/{option_name}`, which asks the vendor's API through the provider's descriptor (see [Providers & SDK]({{ site.baseurl }}/providers)).

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

Returns the updated connector. After the edit is saved, it asks for the work that follows, for a worker to run within seconds:

- a reconcile (`sync`) for a connector of a syncing provider, and a scaling check (`scale`) for a cloud connector;
- a traffic decision when the edit changes the limit, the action, the period or the reset day of `traffic_config`; until it is stored, the answer shows the stored block with `traffic_usage.pending` true. An edit of the price, the currency, `warn_percent` or `limit_status` applies at once and asks for nothing;
- a change of `rate_limit_config` lifts the quarantines of the connector's proxies, tripped under limits that no longer apply.

### Delete Connector

```bash
DELETE /api/v1/projects/{project_id}/connectors/{connector_id}
```

A cloud connector is not deleted at once: it is disabled, its proxies are marked terminating, and a worker terminates their instances and then deletes the connector. Creating or editing a connector of a syncing provider queues a sync, and a cloud connector a scaling check, for a worker to run within seconds.

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

Lines that cannot be read are reported and skipped; the rest are added in one transaction, so they are all added or, when the write fails, none is.

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

A cloud proxy is marked terminating and a worker terminates its instance before the row goes; a slot of a provider connector is deleted and a sync is queued, which provisions a replacement when the connector wants one.

### Proxy location fields

Every proxy response carries the outcome of [IP attribution]({{ site.baseurl }}/ip-attribution): `country` (what routing uses), `country_source` (`database`, `vendor`, `endpoint` or `manual`), `vendor_country` (what the vendor claimed), `location_conflict` (the claim is contradicted), `location_verdict` (`confirmed`, `contradicted` or `open`; null when nothing was claimed or the exit was not judged yet), `location` (region, `state_code`, city, coordinates, ASN and anonymity flags from the databases, when any covers the IP), `state_code`, `state_source`, `city` and `city_source` (the state and city routing matches on: pinned by hand, or resolved from the databases, then the echo endpoint, then the vendor's word, like the country), and `state_verdict` and `city_verdict` (how a claimed state or city fared, with the same values as `location_verdict`). Setting `country` by hand pins it as `manual`: attribution then verifies the exit against it instead of overwriting it.

---

## Metrics

### Get Project Metrics

```bash
GET /api/v1/projects/{project_id}/metrics
```

**Response:**
```json
{
  "pool": {
    "total_proxies": 10,
    "healthy_proxies": 8,
    "unhealthy_proxies": 2,
    "quarantined_proxies": 1,
    "draining_proxies": 0,
    "terminating_proxies": 0,
    "total_requests": 15420,
    "total_successes": 15112,
    "total_failures": 308,
    "overall_success_rate": 98.0,
    "avg_latency_ms": 145.2,
    "total_bytes_sent": 91000000,
    "total_bytes_received": 4830000000
  },
  "strategy": {
    "current_strategy": "round_robin",
    "available_strategies": ["round_robin", "least_used", "random", "sticky", "health_based"]
  }
}
```

The pool is the proxies of the project's enabled connectors that no traffic limit holds back; `healthy_proxies` are healthy and not quarantined, and `unhealthy_proxies` is everything else that is not draining or terminating, quarantined proxies included. The counters are the project's all-time totals plus what the gateways counted since the last metrics flush, so they are within seconds of live traffic and read the same on every api process. `overall_success_rate` is a percentage. The strategy is the project's `routing_strategy`; change it with [Update Project](#update-project).

### Traffic Split

How the project's traffic divides between its connectors under the current [weights]({{ site.baseurl }}/routing-strategies#connector-weights), expected and observed.

```bash
GET /api/v1/projects/{project_id}/metrics/traffic-split?range=1h
```

`range` is one of `1h` (default), `6h`, `24h`, `7d`, `30d` and bounds the observed counts.

**Response:**
```json
{
  "strategy": "round_robin",
  "range": "1h",
  "total_weight": 4,
  "observed_requests": 1200,
  "connectors": [
    {
      "connector_id": "…",
      "name": "Oxylabs residential",
      "credential_type": "oxylabs",
      "enabled": true,
      "weight": 3,
      "dynamic": true,
      "total_proxies": 1,
      "eligible_proxies": 1,
      "expected_share": 75.0,
      "excluded_reason": null,
      "observed_requests": 880,
      "observed_share": 73.33,
      "observed_bytes": 18400000000,
      "cost": 147.2,
      "currency": "USD"
    },
    {
      "connector_id": "…",
      "name": "DC pool",
      "credential_type": "static_proxy_provider",
      "enabled": true,
      "weight": 1,
      "dynamic": false,
      "total_proxies": 20,
      "eligible_proxies": 18,
      "expected_share": 25.0,
      "excluded_reason": null,
      "observed_requests": 320,
      "observed_share": 26.67,
      "observed_bytes": 6100000000,
      "cost": null,
      "currency": null
    }
  ]
}
```

`expected_share` (0-100) is what an untargeted request sees right now: every enabled connector with at least one eligible proxy takes `weight / total_weight`. A connector taking nothing has `excluded_reason` set to `disabled`, `traffic_limit` (blocked by its [traffic limit]({{ site.baseurl }}/traffic-limits)) or `no_eligible_proxies`. `observed_share` is the connector's part of the requests in the window, `null` when there were none; `observed_bytes` is its traffic in the window and `cost` what that traffic costs at the connector's price per GB (`null` when unpriced). The observed figures come from the connector's own metrics history, so they survive proxy rotation. The response also carries `observed_bytes` for the whole project.

### Hosts

Where the project's traffic went in a window, by destination host and by the connector that carried it. The numbers come from the flushed host history (see [Metrics]({{ site.baseurl }}/metrics#hosts-where-the-traffic-goes)), so they lag live traffic by up to one flush interval.

```bash
GET /api/v1/projects/{project_id}/metrics/hosts?range=24h&connector_id=…&search=example&limit=100
```

`range` is one of `1h`, `6h`, `24h` (default), `7d`, `30d`. `connector_id` keeps only the traffic that connector carried, `search` only hosts containing the text (case-insensitive), and `limit` (1-500, default 100) caps how many hosts come back, busiest first; `totals.host_count` says how many matched in all.

**Response:**
```json
{
  "range": "24h",
  "since": "2026-10-09T10:00:00",
  "overflow_host": "(other)",
  "enabled": true,
  "limit": 100,
  "totals": {
    "host_count": 42,
    "request_count": 18200,
    "success_count": 17900,
    "failure_count": 300,
    "avg_latency_ms": 212.4,
    "bytes_sent": 91000000,
    "bytes_received": 4830000000
  },
  "connectors": [
    { "connector_id": "…", "name": "Oxylabs residential", "credential_type": "oxylabs", "enabled": true }
  ],
  "hosts": [
    {
      "host": "www.example.com",
      "request_count": 9100,
      "success_count": 9050,
      "failure_count": 50,
      "avg_latency_ms": 180.2,
      "bytes_sent": 40000000,
      "bytes_received": 2900000000,
      "request_share": 50.0,
      "bytes_share": 59.73,
      "connectors": [
        {
          "connector_id": "…", "name": "Oxylabs residential", "credential_type": "oxylabs",
          "request_count": 9100, "success_count": 9050, "failure_count": 50,
          "avg_latency_ms": 180.2, "bytes_sent": 40000000, "bytes_received": 2900000000
        }
      ]
    }
  ]
}
```

`request_share` and `bytes_share` (0-100) are the host's part of the window's requests and of its bytes both ways. A host named `overflow_host` is where requests to hosts beyond a configured ceiling were folded (see `metrics.hosts.max_hosts` in [Configuration]({{ site.baseurl }}/configuration#host-metrics); it never appears with the default of no ceiling); `enabled` is `false` when per-host counting is switched off, in which case nothing new is recorded. A connector entry whose `name` is `null` was deleted after its rows were written; its rows cascade with it on the next flush.

The busiest hosts over time, for a chart:

```bash
GET /api/v1/projects/{project_id}/metrics/hosts/history?range=24h&top=8&connector_id=…
GET /api/v1/projects/{project_id}/metrics/hosts/history?range=24h&hosts=www.example.com,api.example.com
```

The `top` hosts by requests (1-12, default 8), or the `hosts` named (comma-separated, up to 12), get a series each; every other host is folded into one series under `overflow_host`. Buckets are sized to the range: 1 minute for `1h`, 5 minutes for `6h`, 15 minutes for `24h`, 1 hour for `7d`, 6 hours for `30d`. Every series carries every bucket of the window, with zeros where the host was quiet, so they stack.

**Response:**
```json
{
  "range": "24h",
  "since": "2026-10-09T10:00:00",
  "bucket_seconds": 900,
  "overflow_host": "(other)",
  "series": [
    {
      "host": "www.example.com",
      "points": [
        { "timestamp": "2026-10-09T10:00:00", "request_count": 120, "success_count": 119, "failure_count": 1,
          "avg_latency_ms": 171.0, "bytes_sent": 500000, "bytes_received": 38000000 }
      ]
    },
    { "host": "(other)", "points": [ { "timestamp": "2026-10-09T10:00:00", "request_count": 31, "…": "…" } ] }
  ]
}
```

### Failure log

Why the project's requests failed or were refused (see [Metrics]({{ site.baseurl }}/metrics#failures-why-requests-failed)). The summary counts every failure from the metrics history; the examples, kept per kind, list each distinct failure once and are updated within about 5 seconds of it happening.

#### Summary

```bash
GET /api/v1/projects/{project_id}/failures/summary?range=24h
```

`range` is one of `1h`, `6h`, `24h` (default), `7d`, `30d`. The numbers come from the flushed history, so they trail live traffic by up to one flush interval.

**Response:**
```json
{
  "range": "24h",
  "since": "2026-10-14T11:00:00",
  "bucket_seconds": 900,
  "total": 3750,
  "kinds": [
    { "kind": "upstream_rejected", "count": 3097,
      "upstream_statuses": [ { "status": 407, "count": 2055 }, { "status": 502, "count": 917 } ] },
    { "kind": "upstream_timeout", "count": 463, "upstream_statuses": [] },
    { "kind": "no_proxy_available", "count": 190, "upstream_statuses": [] }
  ],
  "connectors": [
    { "connector_id": "a2b9…", "name": "Bright Data residential", "count": 3560 },
    { "connector_id": null, "name": null, "count": 190 }
  ],
  "series": [
    { "kind": "upstream_rejected", "points": [ { "timestamp": "2026-10-14T11:00:00", "count": 12 } ] }
  ],
  "enabled": true
}
```

`kinds` and `connectors` are ordered most first; a connector of `null` stands for requests refused before a proxy was chosen. `series` has one entry per kind in the same order, each with every bucket of the window (zeros where it was quiet), so they stack.

#### Examples

```bash
GET /api/v1/projects/{project_id}/failures?kind=upstream_rejected&count=200&cursor=…
```

The examples seen last first. `kind` keeps one kind (leave it out for all of them), `count` is 1-1000 (default 200), and `next_cursor` is set when older examples remain; pass it as `cursor` for the next page.

**Response:**
```json
{
  "records": [
    {
      "id": "upstream_rejected:3f9a0c1b2d4e5f60",
      "kind": "upstream_rejected",
      "status": 502,
      "message": "407 Proxy Authentication Required",
      "error_type": "TunnelRefusedError",
      "method": "CONNECT",
      "target": "www.example.com:443",
      "session_id": "order-1",
      "location": "US/NY",
      "peer_id": null,
      "peer_name": null,
      "proxy_id": "6f1c…",
      "connector_id": "a2b9…",
      "connector_name": "Bright Data residential",
      "proxy": "brd.superproxy.io:33335",
      "upstream_status": 407,
      "upstream_message": "x-brd-error: Auth failed",
      "latency_ms": 212.4,
      "count": 37,
      "first_at": "2026-10-15T10:59:58.120000",
      "last_at": "2026-10-15T11:00:02.870000"
    }
  ],
  "next_cursor": null,
  "total": 1,
  "newest": 1760526004211,
  "enabled": true,
  "max_entries": 1000,
  "retention_hours": 168
}
```

`kind` is one of `upstream_timeout`, `upstream_unreachable`, `upstream_rejected`, `upstream_error`, `client_closed`, `interrupted`, `preflight_mismatch` (the failures the request metrics count), `no_proxy_available`, `all_proxies_quarantined`, `traffic_limit_exceeded`, `invalid_location` (refused before a proxy was used), or `intercepted_request_failed` (one request inside a TLS-intercepted connection). `status` is what the client was answered; `upstream_status` is the vendor's HTTP status when an HTTP upstream refused the request, and `upstream_message` what it said about it besides: its error headers (such as `x-brd-error`) and the text of its error page, `null` when it said nothing more. `count` is how many identical failures the example stands for, between `first_at` and `last_at`, from every gateway; `session_id` is the first one's. `id` stays the same while the failure recurs. `total` is how many examples are kept and `max_entries` how many are kept per kind.

`newest` marks where the log stood when the first page was read. To tell how many examples are new or have happened again since, poll the head with it:

```bash
GET /api/v1/projects/{project_id}/failures/head?since=1760526004211&kind=upstream_rejected
```

```json
{ "changed": 3, "total": 1000 }
```

#### Clearing

```bash
DELETE /api/v1/projects/{project_id}/failures
```

Discards the examples (editors and admins). The counts are part of the metrics history and stay, and new failures keep being recorded.

### Prometheus Metrics

Export a project's metrics as Prometheus text (`text/plain; version=0.0.4`):

```bash
GET /api/v1/projects/{project_id}/metrics/prometheus
```

The pool and the project's counters, as [Get Project Metrics](#get-project-metrics) reports them, with a `project` label: `octoprox_proxies_total`, `octoprox_proxies_healthy`, `octoprox_proxies_quarantined` (gauges), `octoprox_requests_total`, `octoprox_requests_success_total`, `octoprox_requests_failure_total`, `octoprox_bytes_sent_total` and `octoprox_bytes_received_total` (counters). Each connector's traffic this period is exported with `project`, `connector` and `name` labels:

| Metric | Type | Description |
|--------|------|-------------|
| `octoprox_connector_traffic_bytes` | gauge | Bytes through the connector in its current traffic period |
| `octoprox_connector_traffic_limit_bytes` | gauge | The period's limit, for connectors that have one |
| `octoprox_connector_traffic_cost` | gauge | Spend at the connector's price per GB, with a `currency` label |
| `octoprox_connector_traffic_blocked` | gauge | 1 while the connector takes no requests because of its limit |

This export needs a sign-in, like the rest of the API. How each process is doing (gateway traffic, the controller, job runs) is exported separately, without sign-in, at `/metrics` on port 8000 of every process; see [Metrics]({{ site.baseurl }}/metrics#prometheus).

### Intercepted Requests

With [TLS interception]({{ site.baseurl }}/tls-interception) on, the requests seen inside a project's intercepted connections are kept for its inspector:

```bash
GET    /api/v1/projects/{project_id}/mitm/requests
DELETE /api/v1/projects/{project_id}/mitm/requests
```

Any signed-in user reads them; clearing them needs an editor (`403` for viewers).

---

## Backup & Migration

Admin-only endpoints for exporting the entire Octoprox setup to a single
encrypted file and restoring it on the same or another install. Useful for
disaster recovery and for migrating between deployments.

### Export Backup

```bash
POST /api/v1/backup/export
```

**Request:**
```json
{
  "passphrase": "correct horse battery staple",
  "include_metrics": false,
  "include_database_files": false
}
```

| Field | Type | Notes |
|-------|------|-------|
| `passphrase` | string | Minimum 8 characters. Required again to import - it cannot be recovered. |
| `include_metrics` | boolean | Include history: proxy/project metrics, exit IP observations and per-connector exit IPs. Default `false` (smaller file). |
| `include_database_files` | boolean | Include the bytes of uploaded and downloaded IP databases. Default `false`; a city database is tens of megabytes. Their rows are always included, so without the files the restored instance lists them without a file, ready to re-upload or refresh. |

**Response:** the backup file as `application/octet-stream` with a
`Content-Disposition: attachment; filename="octoprox-backup-YYYY-MM-DD.opbak"`
header.

The file covers users (including password hashes), projects, credentials,
connectors, proxies, custom provider descriptors and their audit log, and the
IP attribution settings and database records. Optionally it also carries the
history (metrics and attribution observations) and the IP database files.

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

**Import replaces all existing data** on the install. The wipe and restore run
in a single transaction, so a failure leaves the existing data untouched; the
last step rebuilds the running metric totals from the imported history (the
totals themselves are never in the file). Afterwards the Redis keys of the
projects the import removed (MITM captures, failure examples) are deleted, and
the api that answered reads the imported provider descriptors and attribution
settings at once. Every other process follows the change entry the import
appends: the controller compiles the imported definitions and the gateways
receive them through the routing feed, and the other api and worker processes
read them again, within moments.

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
  "provider_descriptors": 0,
  "provider_audit_log": 0,
  "geo_settings": 1,
  "geo_databases": 2,
  "geo_database_blobs": 0,
  "ip_observations": 0,
  "connector_exit_ips": 0,
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
- Schema mismatch: the backup's Alembic revision differs from this install's.
  Upgrade both installs to the same Octoprox version, then retry. This check
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

## System Statistics

Admin-only. One endpoint describing the install as a cluster: every process and what it is doing, the controller, the routing feed and each gateway's copy of it, the work streams, and what the install holds and costs in Postgres and Redis. This is what the **Settings → System** page renders.

```bash
GET /api/v1/system/stats
```

Editors and viewers receive `403`.

**Response sections:**

| Section | Contents |
|---------|----------|
| `instances` | Every live process, grouped by the role it registered with (`all`, `api`, `gateway`, `controller`, `worker`; a role with no live process is absent), each list sorted by instance id. An entry carries `instance_id`, `role`, the process's last heartbeat `snapshot` (below) and `age_seconds`, how old that snapshot is. `{}` when Redis cannot be read. Echo processes do not heartbeat and are never listed. |
| `controller` | `leader`: the instance id holding the controller lease (`null` while none does, during a failover); `changes` and `compiled_at`: how far the leader's last compile read the change stream (an entry ID) and when it was made, from its heartbeat; `changes_head`: the change stream's newest entry ID, read now, ahead of `changes` while the leader has changes to compile (`null` while there is no stream); `standby_poll_ms` and `lease_ttl_ms`: how often a standby asks for the lease and how long a failed leader holds it, the numbers the page words the takeover from; `error` when Redis cannot be read. |
| `feed` | The routing feed in Redis: `digest`, `changes`, `compiled_at` and `sections` of its head (all `null` while there is none), and `gateways`: per gateway the `digest` and `changes` it applied, `in_sync` (it applied the current head) and `age_seconds` since it last applied an update, which grows while nothing changes. `error` when Redis cannot be read or the head was published by another release. |
| `streams` | `by_name`: every work stream by name, `jobs.<kind>` for each of the fourteen job kinds, then `sightings` and `observations`, each with `length` (entries queued and running, since workers delete each entry once handled), `pending` (delivered to a worker and not acknowledged yet: running, or waiting to be reclaimed), `queued` (`length` less `pending`, never below zero: the backlog), `consumers` (worker processes in the `workers` group, a stopped one until index hygiene removes it, within the hour) and `oldest_seconds` (the age of the oldest entry, queued or running, by Redis's clock; `null` when the stream is empty). A stream nobody created yet reads as zeros. `error` when Redis cannot be read. |
| `inventory` | Exact counts of projects, credentials, connectors, proxies, users and providers, and `proxies_by_status` from the proxies' status column. |
| `projects` | Per-project credential, connector and proxy counts, busiest first. |
| `database` | Database size, per-table size (indexes included) and server connections. Each api process's own pool is in its snapshot. |
| `redis` | Memory, throughput, hit rate and a breakdown of the keyspace by purpose; keys without the `o6:` prefix, left by 5.x, are grouped as *Earlier releases*. |

The answer is the same whichever api process the load balancer picked: every process publishes its own state, and the api reads them all. Every section read from Redis reports its own `error`, and the Postgres sections still render without Redis, so the page stays useful during the outage it should explain. Redis's own consumer-group `lag` is never used: it is unknown whenever an entry deleted out of order lies past the group's position, which is when a backlog builds.

**A process's snapshot.** Every process except echo publishes a snapshot every 5 seconds, under a key that expires 10 seconds later; a process that stops withdraws it at once.

| Field | Notes |
|-------|-------|
| `runtime` | `version`, `role`, `instance_id`, `pid`, `started_at`, `uptime_seconds`, and `loop_lag_max_ms` / `loop_lag_mean_ms`: how late the event loop ran timers over the last minute (null before the first minute). A worst case over a second is also logged as `Event loop running behind`. |
| `ready`, `reasons` | Whether the process answers `/ready` with 200, and why not, as `/ready` says it (`"gateway: feed not applied yet"`). |
| `tasks` | The process's background loops and their run counters (below). |
| `streams` | Each event stream the process follows (`changes`, `runtime`, `feed`): `position` (the last entry it read, or the newest at its last full read; `0-0` when there was no stream), `lag_seconds` (how far behind the newest entry its last read found it), `entries` read, `full_reads` by reason (`start`, `gap`, `reconnect`, `reconcile`) and `last_full_read_at`. In role `all`, `changes` is the worker's definitions refresher's. |
| `api` | An api process's Postgres pool (`pool_size`, `pool_checked_out`, `pool_overflow`). |
| `gateway` | A gateway's proxy listener (`listener_port`, `listening`, `active_connections`); the feed it applied (`feed_digest`, `feed_changes`, `feed_applied_at`, `feed_age_seconds`); what it keeps out of routing on its own (`retired_slots`, `local_quarantines`); `in_flight` transfers; its traffic since it started (`requests`, `successes`, `failures`, `bytes_sent`, `bytes_received`, `failures_by_kind`); its buffers (`pending_metric_deltas`, `metric_flush_failures`, `rate_limit_errors`, `observations_pending`, `observations_dropped`, `sightings_pending`, `sightings_dropped`, `failure_log_dropped`, `mitm_captures_dropped`); `preflight_checks` and `preflight_rejections`; its IP databases (`geo_databases_loaded`, `geo_database_errors`, `geo_databases_missing`); and `tunnels`, one entry per tunnel protocol it terminates. |
| `controller` | `is_leader` and `lease_terms`; the last compile (`changes`, `compiled_at`, `compiles`, `compile_ms`, `compile_error`) and publish (`publishes`, `published_at`, `publish_ms`, `publish_error`, `sections`, `sections_written`, `sections_removed`); `quarantined` proxies and `blocked_connectors` as published. A standby reports what it last saw while it led. |
| `worker` | `jobs`: per job kind it takes, `runs`, `failures`, `duration_seconds` and `in_flight`; `sightings_consumed` and `sightings_dropped`, `observations_consumed` and `observations_dropped` (dropped: given up after repeated deliveries). |

A process of role `all` carries all four role sections. `snapshot` is `null` for a process that could not build one, or that runs another release; it is listed anyway.

**Cost.** Entity counts are exact. Table row counts are planner estimates
(`pg_class.reltuples`), so they stay cheap on metric tables with millions of
rows, drift until the next `ANALYZE`, and are `null` on a table autovacuum has
not reached yet. The work streams are read in one round trip (`XLEN`,
`XINFO GROUPS` and the oldest entry of each, and Redis's `TIME`). The Redis
keyspace breakdown is a bounded `SCAN`; past 50,000 keys it reports a sample
and sets `"truncated": true`.

**Example (abridged):**
```json
{
  "generated_at": "2026-10-18T09:12:35.740343",
  "instances": {
    "api": [
      { "instance_id": "api-1", "role": "api", "age_seconds": 2.1,
        "snapshot": {
          "runtime": { "version": "6.0.0", "role": "api", "instance_id": "api-1", "uptime_seconds": 3841.2 },
          "ready": true, "reasons": [], "tasks": [],
          "streams": { "changes": { "position": "1760778750114-0", "lag_seconds": 0.0, "entries": 412,
                                    "full_reads": { "start": 1, "reconcile": 1 }, "last_full_read_at": "2026-10-18T08:12:30.114221" } },
          "api": { "pool_size": 5, "pool_checked_out": 1, "pool_overflow": 0 },
          "gateway": null, "controller": null, "worker": null } }
    ],
    "gateway": [ { "instance_id": "gateway-1", "role": "gateway", "age_seconds": 3.4, "snapshot": { "…": "…" } } ],
    "controller": [ { "instance_id": "controller-1", "role": "controller", "age_seconds": 1.0, "snapshot": { "…": "…" } } ],
    "worker": [ { "instance_id": "worker-1", "role": "worker", "age_seconds": 4.2, "snapshot": { "…": "…" } } ]
  },
  "controller": { "leader": "controller-1", "changes": "1760778750114-0", "compiled_at": "2026-10-18T09:12:30.114221",
                  "changes_head": "1760778750114-0", "standby_poll_ms": 1000, "lease_ttl_ms": 30000, "error": null },
  "feed": {
    "digest": "9c1f…", "changes": "1760778750114-0", "compiled_at": "2026-10-18T09:12:30.114221", "sections": 10,
    "gateways": [ { "instance_id": "gateway-1", "digest": "9c1f…", "changes": "1760778750114-0", "in_sync": true, "age_seconds": 5.2 } ],
    "error": null
  },
  "streams": {
    "by_name": {
      "jobs.health": { "length": 220, "pending": 200, "queued": 20, "consumers": 2, "oldest_seconds": 1.4 },
      "jobs.sync": { "length": 0, "pending": 0, "queued": 0, "consumers": 2, "oldest_seconds": null },
      "sightings": { "length": 3, "pending": 0, "queued": 3, "consumers": 2, "oldest_seconds": 0.2 }
    },
    "error": null
  },
  "inventory": {
    "projects": 2, "credentials": 8, "connectors": 9, "connectors_enabled": 2,
    "proxies": 29, "users": 1, "users_by_role": { "admin": 1 },
    "proxies_by_status": { "healthy": 19, "unhealthy": 10 }
  },
  "database": {
    "name": "octoprox",
    "size_bytes": 8985623,
    "tables": [
      { "name": "proxies", "row_estimate": 29, "total_bytes": 294912,
        "table_bytes": 65536, "index_bytes": 229376,
        "dead_rows": 4, "last_vacuum_at": "2026-10-10T06:12:40Z" }
    ],
    "backends": 9, "error": null
  },
  "redis": {
    "used_memory_bytes": 1558312, "total_keys": 33, "ops_per_sec": 4,
    "groups": [ { "label": "Routing feed", "keys": 3 } ],
    "truncated": false
  }
}
```

A task in `tasks` looks like this:

```json
{ "name": "feed_builder", "scope": "singleton", "lease": "controller",
  "state": "running", "error": null, "interval_seconds": 1,
  "runs": 412, "idle_runs": 0, "failures": 1, "consecutive_failures": 0,
  "overruns": 3, "consecutive_overruns": 0,
  "last_overrun_at": "2026-10-18T07:31:55.417002",
  "last_run_at": "2026-10-18T09:12:30.114221", "last_duration_ms": 84.2,
  "avg_duration_ms": 61.9, "max_duration_ms": 512.7,
  "last_error": "TimeoutError: ", "last_error_at": "2026-10-18T06:04:11.002913" }
```

**Two kinds of loop health.** `state` and `error` describe the asyncio task:
not `running` means the loop in that process has stopped, and `error` carries
the exception that ended it. The run counters describe the cycles *inside* the
loop - one compile, one batch of a job kind, one metering window - so a loop
that raises every cycle and recovers is still `running`, and only
`consecutive_failures` and `last_error` say it is broken. Counters are
per-process and reset with it; a cycle cancelled at shutdown is not counted.
A job runner's runs are the batches it ran, and its failures the batches whose
run raised.

**Runs that had nothing to do.** `idle_runs` is the subset of `runs` where the
loop ticked and returned early - a feed read that found the head already
applied, a metering window with nothing to write, a stream read that timed out
with no entry. Subtract it from `runs` for the cycles that did something. The
split matters because the two numbers answer different questions: a gateway
taking no traffic wakes its loops all day without doing anything, and without
`idle_runs` that reads as busy loops. The durations describe the working cycles
only - an early return is not a measurement of the work it skipped. A loop with
no idle path (the heartbeat) reports `0`.

**Cadence and overruns.** `interval_seconds` is the interval the loop was
started with, declared by the loop itself so it is the value actually slept on
rather than a config setting read elsewhere - worth knowing, because a process
configured differently from the rest will say so here. A loop that acts on
stream entries rather than a timer declares none. `overruns` counts cycles that
took longer than that interval: a loop cannot start its next cycle until the
current one returns, so those cycles pushed it off its cadence, and its work
happened less often than configured.

`overruns` is a lifetime count, so read it with `consecutive_overruns`, which
the first cycle back inside the cadence resets: that pair distinguishes "slow
right now" from "hit one slow cycle during a restart and has been fine since",
and `last_overrun_at` says when the most recent one was. `failures` /
`consecutive_failures` work the same way. A climbing `consecutive_overruns`
with `failures` at zero is the signature of a loop that needs a longer
interval or less to do, rather than one that is broken.

**Which loop runs where.** `scope` is `singleton` for the controller's loops,
the feed builder (`feed_builder`) and one job producer per periodic kind
(`job_producer_health`, `job_producer_metrics_flush`, ...), which act only in
the leading controller, under the one `controller` lease their `lease` names;
it is `instance` for the loops every process of a role runs. A standby
controller's singleton loops are legitimately at `"runs": 0`. The job runners
of a worker are named after their kind (`job_runner_health`), one per kind it
takes, beside `sighting_ingest` and `observation_ingest`.

**How the System page reads it.**

- **The work streams table** shows one row per stream: length, pending, queued, consumers and the oldest entry's age. Two hints read the numbers: *queued with no live worker: no worker takes this kind* shows when entries are queued and either the group has no consumer or none of the live workers, by their heartbeats (`worker.jobs`; every worker takes the sightings and the observations), takes the stream, so it shows within a heartbeat after the last such worker stops, while the group still lists it as a consumer until index hygiene removes it. *Queued stays above zero: too few workers for this kind* shows once every one of the page's own readings has found a backlog for a minute, since a single reading can land in a burst of periodic jobs that drains in milliseconds.
- **Positions are times.** Every "how far" is an entry ID of a stream, shown as the time it carries: the leader's compile, the feed head's, each gateway's applied feed, and each api's and worker's definitions (their **Definitions** rows: how far the process has read the change stream, and how often it read in full instead, by reason). A position at or past `changes_head` is *in sync*; one behind it reads *behind, at <time>*, where the process has read through, coloured as late only once the newest change is 30 seconds older than the process's snapshot, so the moment between a save and the next heartbeat is not flagged. `0-0` reads *before any change*. IDs compare as two numbers, milliseconds then sequence, never as text.
- **The takeover** is worded from `standby_poll_ms` and `lease_ttl_ms`: within a poll of the leader stopping cleanly, or within the TTL and a poll of it failing, each rounded up to whole seconds.

### System Topology

```bash
GET /api/v1/system/topology
```

Admin-only. The cluster sections of `/system/stats` alone, `generated_at`, `instances`, `controller`, `feed` and `streams`, in the same shape. It reads Redis only, without the keyspace breakdown and the table sizes, so it is cheap enough to poll every 5 seconds, the heartbeats' cadence. The System page's **Topology** view polls it while the view is open.

**How the Topology view reads it.** Every live process sits in the column of each runtime its snapshot carries, so a process of role `all` shows in all four. The arrows are the streams between the roles: the change stream from the api to the leading controller, the routing feed from the controller to the gateways, and the work streams from the api, the controller and the gateways to the workers. Rates (requests and bytes per second for a gateway, job runs per second for a worker) are the difference between two of a process's snapshots over the difference of their `uptime_seconds`, so they are timed on the process's own clock and a snapshot read twice counts once.

### System Trends

```bash
GET /api/v1/system/stats/history?range=24h
```

Admin-only. Returns the gauge history behind the **Trends** charts on the
System page. Ranges: `1h`, `24h` (raw snapshots) and `7d`, `30d`, `90d`
(averaged into buckets).

Snapshots are written by the `snapshot` job, which the controller publishes
every `system.metrics_interval` and a worker runs, so the series reads the same
from every api process. The interval is reported from the api's settings, so
set `system.metrics_interval` alike on the controller, the workers and the api.

**Gauges, not counters.** These are readings like "how big is the database",
so downsampling a range **averages** them. Summing, as the proxy and project
metrics pipelines do for request counts, would invent a number that was never
true at any instant. `bucket_seconds` is `null` for raw ranges and set when
points are averages, so a chart can say which it is showing.

| Field | Notes |
|-------|-------|
| `snapshots` | Points oldest-first: database and Redis size, Redis keys, entity counts, proxy totals by health. |
| `table_growth` | Per-table size at both edges of the window plus the delta, biggest mover first. A difference between two points, so no bucketing is involved. |
| `bucket_seconds` | Bucket width, or `null` when points are raw. |
| `interval_seconds` | The configured snapshot cadence, so a client can tell a real gap from an expected one. |

**Configuration** (`config/*.yaml`, or `OCTOPROX_SYSTEM_METRICS_*` env vars):

```yaml
system:
  metrics_interval: 300        # seconds between snapshots; 0 takes none
  metrics_retention_days: 90   # 0 keeps snapshots forever
```

The interval is floored at 60 seconds. At the default cadence this is one row
every five minutes for the whole install - about 26k rows over the 90-day
window - so retention alone keeps it bounded and there are no compaction tiers
like the ones `proxy_metrics` and `project_metrics` use.

**Example:**
```json
{
  "range": "7d",
  "bucket_seconds": 3600,
  "interval_seconds": 300,
  "snapshots": [
    {
      "timestamp": "2026-09-19T21:00:00",
      "database_size_bytes": 8985623, "redis_memory_bytes": 1558312, "redis_keys": 33,
      "projects": 2, "credentials": 8, "connectors": 9, "connectors_enabled": 2, "users": 1,
      "proxies_total": 29, "proxies_healthy": 19, "proxies_unhealthy": 10
    }
  ],
  "table_growth": [
    { "name": "project_metrics", "first_bytes": 131072, "last_bytes": 212992, "delta_bytes": 81920 }
  ]
}
```

---

## IP Attribution

All endpoints live under `/api/v1/geo`. Reads need any authenticated user; writes need an admin. See [IP Attribution]({{ site.baseurl }}/ip-attribution) for the concepts.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/geo/settings` | Install-wide settings (default conflict rule, `default_city_radius_km`, echo endpoint, preflight, retention), whether an admin saved them, and the databases the api process that answered has open |
| PUT | `/geo/settings` | Replace the settings (body: the `settings` object). Applies everywhere within moments, as its change entry reaches each process; a change of the default conflict rule or city radius also queues a re-attribution of every proxy |
| GET | `/geo/databases` | Every database: stored rows plus config-file entries, with `loaded_here` and `load_error` (the api process that answered), `gateways_loaded` out of `gateways_total` (how many live gateways have it open, from their heartbeats) and `gateway_errors` (why not, by gateway instance id), and, for a scheduled download, `last_update_at` (the last check, whether or not the file changed) and `next_update_at` (when the schedule is next due; null without a schedule, while `update_paused`, or while the database is disabled) |
| POST | `/geo/databases` | Upload a file (`multipart/form-data`: `file`, optional `name`, `priority`, `enabled`): an mmdb, an IP2Location BIN, or a GeoNames table (zipped or `.txt`, stored with `vendor: geonames`, `kind: places`, `format: geonames`). Validated before it is stored; an enabled upload queues a re-attribution |
| POST | `/geo/databases/from-url` | Register a vendor download URL (`name`, `update_url` which may carry `{YYYY}` and `{MM}` placeholders filled per run, `update_interval_hours`, `update_auth`, `priority`) and download it now, on the api. `update_auth` takes `{username, password}` for basic auth or `{token}` / `{header, token}`; the list never returns it, only `has_update_auth` |
| POST | `/geo/databases/inspect` | Open an uploaded file and report its metadata without storing it |
| PATCH | `/geo/databases/{id}` | Change `name`, `enabled`, `priority`, `update_url`, `update_interval_hours`, `update_auth`, `update_paused` (true stops the schedule and keeps the file loaded and the URL, credentials and interval; a refresh still downloads) |
| POST | `/geo/databases/{id}/refresh` | Download a scheduled database now, on the api; a changed file of an enabled database queues a re-attribution |
| DELETE | `/geo/databases/{id}` | Remove a stored database everywhere; an enabled one queues a re-attribution |
| POST | `/geo/lookup` | `{ip, claimed_country?, claimed_state?, claimed_city?, project_id?}`: each database's answer and how the IP resolves at every level, judging the optional claim as a vendor's would be under the default conflict rule and city radius, or the project's. `resolution.city_distance_km` is how far the observed city is from the claimed one when both could be placed |
| POST | `/geo/reattribute` | `{connector_id?}`: queue a re-run of attribution, offline, for every proxy with a known exit IP (or the connector's). Answers `202` with `{"kind": "reattribute", "key": "all"}` (or `"connector:<id>"`) and a worker runs it. Each press is a job of its own; presses a worker reads together run once, `all` covering the connectors among them. `404` for an unknown connector, `503` when Redis did not take the job |
| GET | `/geo/observations` | One page of observations, newest first, with `total` for paging. Filters apply on the server and mirror the table's columns: `connector_id`, `proxy_id`, `project_id`, `source`, `ip` (exact), `claimed_country`, `resolved_country`, `verdict` (`contradicted`, `open`, `confirmed`, `no_claim`: the country verdict; the state and city verdicts come back per row and do not move the label), `until` (ISO time: start the log at that moment; without an offset it is UTC); paging with `limit` (max 1000) and `offset`. `total` is counted no further than 10,000 rows past the page: `total_exact` false makes it a lower bound. Each row also carries the place below the country: `claimed_state`, `claimed_city`, `resolved_state`, `resolved_city`, and one verdict per level, `country_conflict`, `state_conflict`, `city_conflict` (true contradicted, false confirmed, null no verdict) |
| GET | `/geo/accuracy` | Per connector, over the distinct exit IPs last seen in the window: `exits`, then `country`, `state` and `city`, each `{claimed, confirmed, contradicted, open, accuracy}`. `open` is a claim without a verdict: for the country one that independent sources disagreed about, counted against `accuracy` (confirmed over claimed); for the state and city one no city-level database could judge, counted in neither (`accuracy` is confirmed over confirmed plus contradicted). `breakdown` lists the contradicted pairs as `{level, claimed, observed, exits}` (`project_id`, `connector_id`, `days`) |
| GET | `/geo/exits` | Per connector: distinct exit IPs first seen in the window and ever, sightings, reuse (`project_id`, `connector_id`, `days`). Dynamic-sessions connectors carry `coverage` (`preflight_on`, `sampled_percent`): below 100 the figures for session-less traffic are undercounts |
| GET | `/geo/exits/sharing` | How one project's connectors share exit IPs seen in the window (`project_id`, required, and `days`). Per connector: `exits` (distinct exits last seen in the window), `reused` (of those, handed out more than once, ever), `shared` (of those, also handed out by another connector of the project in the window), `provider` (the credential type) and `coverage` as for `/geo/exits`. `pairs` lists every two connectors with exits in common as `{connector_id, other_id, shared}`, and the top-level `exits` and `shared` count distinct exits across the project and those behind more than one connector. `404` for an unknown project; past 30 s it answers 504 |
| GET | `/geo/observations/count` | `{"total"}`: every observation under the same filters, counted outright. Quick under `project_id`, `connector_id`, `proxy_id` or `ip`; under the other filters alone it reads the table, and past 30 s it answers 504 |
| GET | `/geo/exits/ips` | One page of distinct exit IPs, most recently seen first, each with first and last sighting, hand-out count and the latest observation's state (proxy, source, vendor claim, resolved country and source, verdict flags, and the same place fields as an observation). Filters: `project_id`, `connector_id`, `ip`, `proxy_id`, `country`, `claimed_country`, `verdict` (the country verdict, as for observations); paging with `limit` (max 1000) and `offset`; `total` and `total_exact` in the response, counted as for observations |
| GET | `/geo/exits/ips/count` | `{"total"}`: every exit IP under the same filters, counted outright, as for observations |
| GET | `/geo/status` | Pipeline health: the databases the answering api process has open and its own observation buffer (Detect records into it), queued (`queued_observations`: published to Redis, not written yet) and stored observations (`stored_observations`: the database's row estimate, within a few percent), preflight counters summed over the live gateways, and `gateways`: per gateway, from its heartbeat, `databases_loaded`, `databases_missing`, `load_errors`, `preflight_checks`, `preflight_rejections`, and its pending and dropped observations and sightings |

Projects carry `location_contradicted` (`evidence`, `vendor`, `refuse`: what to do when the independent sources agree against the vendor's claim), `location_unknown` (`vendor`, `refuse`: what to do when nothing independent places the exit), `location_preflight` (boolean, whether a session's exit is echoed before its first request), and the optional overrides `location_conflict_rule` (`consensus`, `first`, `any`) and `location_city_radius_km` (0 to 500 km; 0 compares city names only) on create, update and in responses. On update an empty string clears an override so the project inherits the install default again. There is no source order: the routing value is always the databases, then the echo endpoint, then the vendor.

### Echo

```bash
GET /echo?nonce=<anything>
```

Public. Returns the caller's IP and, with a database loaded, its attribution:

```json
{"ip": "203.0.113.7", "country": "GB", "region": "England", "city": "London", "asn": 12345, "organization": "Example Ltd", "databases": ["<database id>"], "nonce": "…", "timestamp": "2026-09-21T12:00:00Z"}
```

Requested through a proxy it reports the proxy's exit. Served by the api (and a single instance), and by a process of role `echo` on its own. Behind a load balancer, list the balancer in `geo.echo.trusted_proxies` so the address comes from `X-Forwarded-For`.

---

## WireGuard

Devices that cannot use a proxy join a project through a WireGuard tunnel
(see [WireGuard Devices](wireguard)). Server settings are install-wide and
need an admin to change; devices (peers) are project entities editors manage.

### Server settings

```http
GET /api/v1/wireguard/settings
```

Returns the public key, endpoint, subnet, gateway and the tunnel's status
across the install: every gateway terminating it, from their heartbeats, and
the devices:

```json
{
  "public_key": "HIgo9xNzJMWLKASShiTqIybxZ0U3wGLiUeJ1PKf8ykw=",
  "endpoint_host": "vpn.example.com",
  "endpoint_port": 51820,
  "subnet": "10.66.0.0/16",
  "gateway": "10.66.0.1",
  "persistent_keepalive": 25,
  "client_mtu": null,
  "configured": true,
  "updated_at": "2026-10-03T09:00:00",
  "status": {
    "gateways": [
      {
        "protocol": "wireguard",
        "instance_id": "gateway-1",
        "state": "running",
        "error": null,
        "backend": "kernel",
        "listen_port": 51820,
        "active_connections": 3,
        "restarts": 0,
        "denied": 0,
        "daemon_version": null,
        "interface": "wg0",
        "fake_ip_range": "198.18.0.0/15",
        "block_encrypted_dns": true
      }
    ],
    "peers_total": 4,
    "peers_enabled": 4,
    "peers_online": 2,
    "connections_by_address": 0,
    "encrypted_dns_blocked": 1
  }
}
```

`status.gateways` has one entry per live gateway with the tunnel enabled
(`state` is `starting`, `running`, `failed` or `stopped`; `error` says why it
is not up); it is empty when no gateway terminates the tunnel.

```http
PUT /api/v1/wireguard/settings
Content-Type: application/json

{
  "endpoint_host": "vpn.example.com",
  "endpoint_port": 51820,
  "subnet": "10.66.0.0/16",
  "persistent_keepalive": 25,
  "client_mtu": null
}
```

Admin only. The subnet must still contain every device's address. Gateways
apply a change live, re-addressing their interface for a new subnet; devices
learn the new DNS address with their next config.

```http
POST /api/v1/wireguard/settings/rotate-key
```

Admin only. Issues a new server key pair; every device config becomes invalid.

### Devices (peers)

```http
GET /api/v1/projects/{project_id}/wireguard/peers
```

```json
{
  "total": 1,
  "server_configured": true,
  "server_public_key": "HIgo...",
  "peers": [
    {
      "id": "…",
      "project_id": "…",
      "name": "Living room TV",
      "public_key": "…",
      "has_private_key": true,
      "has_preshared_key": true,
      "address": "10.66.0.2",
      "enabled": true,
      "session_id": "sofa",
      "country": "US",
      "state": "NY",
      "city": "new_york",
      "created_at": "…",
      "updated_at": "…",
      "status": {
        "online": true,
        "last_handshake_at": "2026-10-03T09:04:12",
        "endpoint": "203.0.113.9:40123",
        "live": true,
        "rx_bytes": 1048576,
        "tx_bytes": 23068672
      },
      "metrics": {
        "request_count": 318,
        "success_count": 316,
        "failure_count": 2,
        "avg_latency_ms": 212.4,
        "bytes_sent": 904211,
        "bytes_received": 21874322,
        "connections_by_address": 0,
        "encrypted_dns_blocked": 1
      }
    }
  ]
}
```

`status` is where the device stands. With `live` true it is what a gateway
carrying the tunnel published in the last half minute (every ten seconds
while it runs), including the interface's own byte counters since that
gateway's interface came up; with `live` false it is the last sighting on
record, which a worker's `tunnel_last_seen` job writes to the device's row
whenever a newer handshake appears, and the byte counters are zero. `metrics` is what
the pool relayed for the device: the same counters a connector has, install-wide
and across restarts, plus the two name-resolution signals (see
[WireGuard Devices](wireguard#encrypted-dns-on-devices)).

```http
POST /api/v1/projects/{project_id}/wireguard/peers
Content-Type: application/json

{
  "name": "Living room TV",
  "public_key": null,
  "preshared": true,
  "enabled": true,
  "session_id": "sofa",
  "country": "us",
  "state": "ny",
  "city": "New York"
}
```

Editor or admin. With `public_key` null Octoprox generates the key pair and
keeps the private key so the config can be shown again; with a device's own
public key only the public half is stored. The device gets the next free
address of the subnet. A state or city needs a country. Names are unique per
project, ignoring case.

```http
GET    /api/v1/projects/{project_id}/wireguard/peers/{peer_id}
PATCH  /api/v1/projects/{project_id}/wireguard/peers/{peer_id}
DELETE /api/v1/projects/{project_id}/wireguard/peers/{peer_id}
POST   /api/v1/projects/{project_id}/wireguard/peers/{peer_id}/rotate-keys
```

`PATCH` takes any of `name`, `enabled`, `session_id`, `country`, `state`,
`city`; an empty string clears the last four. `rotate-keys` issues a new key
pair (and preshared key, if the device had one).

```http
GET /api/v1/projects/{project_id}/wireguard/peers/{peer_id}/metrics/history?range=24h
```

The device's own history, in the shape of `metrics` above plus a
`timestamp` per snapshot, wrapped as `{"snapshots": [...]}`. Same `range`
values and bucketing as the project and connector history endpoints
(`1h`, `6h`, `24h` raw minutes; `7d` hourly; `30d` 6-hourly).

```http
GET /api/v1/projects/{project_id}/wireguard/peers/{peer_id}/config
```

```json
{
  "filename": "Living-room-TV.conf",
  "config": "[Interface]\nPrivateKey = …\nAddress = 10.66.0.2/32\nDNS = 10.66.0.1\n\n[Peer]\nPublicKey = …\nPresharedKey = …\nAllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = vpn.example.com:51820\nPersistentKeepalive = 25\n",
  "complete": true,
  "server_configured": true
}
```

`complete` is false when the device holds its own private key (the file
carries a placeholder) or the endpoint host is not set yet. The config
carries the device's private key, so downloading it needs an editor
(`403` for viewers).

## OpenVPN

The second tunnel protocol (see [OpenVPN Devices](openvpn)). The same
shape as the WireGuard endpoints with the credential swapped: the install
has a CA instead of a key pair, a device a certificate instead of keys.

### Server settings

```http
GET /api/v1/openvpn/settings
```

Returns the CA fingerprint and expiry, endpoint, transport, subnet, gateway
and the tunnel's status across the install, in the shape WireGuard's has:
one entry in `status.gateways` per gateway running the daemon, with its
`daemon_version`, `restarts`, `denied` and `error`. Private material (the CA
key, the server key, the tls-crypt key) is never returned.

```json
{
  "ca_fingerprint": "A1:B2:…",
  "ca_expires_at": "2036-10-05T09:00:00",
  "endpoint_host": "vpn.example.com",
  "endpoint_port": 1194,
  "protocol": "udp",
  "subnet": "10.67.0.0/16",
  "gateway": "10.67.0.1",
  "keepalive_interval": 10,
  "keepalive_timeout": 60,
  "client_mtu": null,
  "configured": true,
  "updated_at": "2026-10-05T09:00:00",
  "status": {
    "gateways": [
      {
        "protocol": "openvpn",
        "instance_id": "gateway-1",
        "state": "running",
        "error": null,
        "backend": null,
        "listen_port": 1194,
        "active_connections": 3,
        "restarts": 0,
        "denied": 1,
        "daemon_version": "OpenVPN 2.6.3 x86_64-pc-linux-gnu",
        "interface": "ovpn0",
        "fake_ip_range": "198.18.0.0/15",
        "block_encrypted_dns": true
      }
    ],
    "peers_total": 2,
    "peers_enabled": 2,
    "peers_online": 1,
    "connections_by_address": 0,
    "encrypted_dns_blocked": 0
  }
}
```

```http
PUT /api/v1/openvpn/settings
Content-Type: application/json

{
  "endpoint_host": "vpn.example.com",
  "endpoint_port": 1194,
  "protocol": "udp",
  "subnet": "10.67.0.0/16",
  "keepalive_interval": 10,
  "keepalive_timeout": 60,
  "client_mtu": null
}
```

Admin only. `protocol` is `udp` or `tcp`; `keepalive_timeout` must exceed
`keepalive_interval`; the subnet must still contain every device's address.
A change to the port, transport, subnet or keepalive restarts the daemon on
every gateway running it.

```http
POST /api/v1/openvpn/settings/rotate-identity
```

Admin only. Issues a new CA, server certificate and tls-crypt key, and
reissues every device's certificate under the new CA in one transaction;
every device profile becomes invalid.

### Devices (peers)

```http
GET /api/v1/projects/{project_id}/openvpn/peers
```

```json
{
  "total": 1,
  "server_configured": true,
  "ca_fingerprint": "A1:B2:…",
  "peers": [
    {
      "id": "…",
      "project_id": "…",
      "name": "Living room router",
      "serial": "3891…",
      "certificate_expires_at": "2036-10-05T09:00:00",
      "address": "10.67.0.2",
      "enabled": true,
      "session_id": null,
      "country": "US",
      "state": null,
      "city": null,
      "created_at": "…",
      "updated_at": "…",
      "status": {
        "online": true,
        "connected_since": "2026-10-05T09:04:12",
        "last_seen_at": "2026-10-05T09:04:12",
        "endpoint": "203.0.113.9:40123",
        "live": true,
        "rx_bytes": 1048576,
        "tx_bytes": 23068672
      },
      "metrics": { "request_count": 318, "…": "as for WireGuard devices" }
    }
  ]
}
```

`status.online` means a gateway running the daemon currently carries a
session for the device; `last_seen_at` is that session's start, or the
persisted start of the last one. `metrics` is the same `TunnelPeerMetrics`
shape WireGuard devices carry.

```http
POST /api/v1/projects/{project_id}/openvpn/peers
Content-Type: application/json

{ "name": "Living room router", "session_id": null, "country": "US", "state": null, "city": null, "enabled": true }
```

Editor. Issues a certificate from the install's CA (the device id as common
name) and takes the next free address. There is no bring-your-own key.

```http
GET    /api/v1/projects/{project_id}/openvpn/peers/{peer_id}
PATCH  /api/v1/projects/{project_id}/openvpn/peers/{peer_id}
DELETE /api/v1/projects/{project_id}/openvpn/peers/{peer_id}
POST   /api/v1/projects/{project_id}/openvpn/peers/{peer_id}/rotate-certificate
GET    /api/v1/projects/{project_id}/openvpn/peers/{peer_id}/metrics/history?range=24h
GET    /api/v1/projects/{project_id}/openvpn/peers/{peer_id}/config
```

`PATCH` takes the same partial document as a WireGuard device (`name`,
`enabled`, `session_id`, `country`, `state`, `city`; an empty string
clears). Disabling, rotating or deleting a device ends any session it has,
on every gateway, once the routing feed brings the change (about two
seconds). `rotate-certificate` issues a new certificate and key; the serial
changes and the old profile stops working. `config` returns the `.ovpn`
profile, which carries the device's key, so downloading it needs an editor
(`403` for viewers):

```json
{
  "filename": "Living-room-router.ovpn",
  "config": "client\ndev tun\nproto udp\nremote vpn.example.com 1194\n…<ca>…</ca>\n<cert>…</cert>\n<key>…</key>\n<tls-crypt>…</tls-crypt>\n",
  "complete": true,
  "server_configured": true
}
```

## Release Notes

What's New in the web UI: the release notes of the running version and those before it, and how many the signed-in user has not opened yet. See [Changelog]({{ site.baseurl }}/changelog).

```bash
GET /api/v1/releases
POST /api/v1/releases/seen
```

Any signed-in user. `POST /releases/seen` records that the user has seen the notes up to the running version; both return the same body:

```json
{
  "current_version": "6.1.4",
  "last_seen_version": "6.1.3",
  "unseen": 1,
  "notes": [
    {
      "version": "6.1.4",
      "from": "6.1.0",
      "date": "2026-10-27",
      "title": "IP sharing and exit headers",
      "changes": [{"text": "...", "docs": "connecting#exit-headers"}]
    }
  ],
  "update": null
}
```

- `notes`: newest first. `from` is set on the summaries that cover several releases (those up to 6.1.4); `docs` is a page on octoprox.com.
- `last_seen_version`: null until the user first opens What's New, and `unseen` is then 1, for the newest notes.
- `update`: for admins only, while `OCTOPROX_UPDATE_CHECK` is on and the last daily check found a newer release: its `version`, `checked_at` and the `notes` of every release newer than the running one. Null otherwise.

## Health Check

Every process, whatever its role, answers four paths on port 8000 for probes,
load balancers and Prometheus. None needs a sign-in, and none touches Postgres
or Redis: they read the process's own state.

```bash
GET /live      # 200 while the process serves
GET /ready     # 200 while it can do its work, else 503
GET /health    # the same check, with who answered
GET /metrics   # Prometheus text of this process
```

**`/ready`**:
```json
{ "status": "ready", "role": "gateway", "reasons": [] }
```
or, with `503`:
```json
{ "status": "unready", "role": "gateway", "reasons": ["gateway: feed not applied yet"] }
```

**`/health`** runs the same check and names the process; `reasons` appears only
with `503`:
```json
{ "status": "healthy", "role": "api", "instance": "api-1", "version": "6.0.0" }
```

**`/live`** answers `{"status": "alive"}`. What ready means for each role is
in [Deployment & Scaling]({{ site.baseurl }}/deployment#health-endpoints), and
the metric families of `/metrics` in [Metrics]({{ site.baseurl }}/metrics#prometheus).

## Internal Endpoints

`/internal/v1` is for the cluster's own processes and is not part of the
public API. A gateway fetches the IP database files the routing feed lists
from the api:

```bash
GET /internal/v1/geo/databases/{database_id}/blob?sha256=<checksum>
Authorization: Bearer <internal token>
```

The token is derived from `OCTOPROX_INTERNAL_SECRET`; a sign-in token is
refused with `401`. The answer is the file, or `404` when the api has no file
of that database at that checksum. The bundled nginx config refuses
`/internal/` on its public port.
