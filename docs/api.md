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

`type` is any provider id from the catalog. The config is validated against the provider's field schema; descriptor providers with a validation call also verify the credential with the vendor and may store captured values (for example Bright Data's customer id). Names are unique per project, ignoring case: a duplicate returns `400`.

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

Connectors declare the countries their proxies exit from inside `config`, so clients can pick them with a `-cc-<code>` suffix on the proxy username (see [Country Routing]({{ site.baseurl }}/routing-strategies#country-routing)). Codes are ISO 3166-1 alpha-2 and are normalised to upper case; an empty list is dropped.

- Static and cloud connectors: `config.countries`, a list of codes.
- Provider connectors: the provider's country field (`config.country_code` for the built-in descriptors). It accepts one code, a list, or a comma-separated string and is always returned as a list. Listing several countries provisions `num_proxies` slots per country.

```json
{
  "name": "EU residential",
  "credential_id": "…",
  "config": { "num_proxies": 5, "country_code": ["DE", "FR", "NL"] }
}
```

Proxy responses include `country`, the exit country when the vendor reported it or the slot was provisioned for one. Connector responses include `target`: the intended pool size (`total`, or null when the connector has none) and how it is derived, `per_country` times the `countries` that have a slot group, with the ones created on demand listed in `on_demand`.

Manually added proxies (static connectors) get their exit location looked up right after they are created: one request goes through the proxy to the `proxy.geo_lookup` endpoint and the discovered IP becomes `display_host` while the country lands in `country`. Pass `country` on `POST .../proxies` to set it yourself and skip the lookup, set `country` to `""` on `PATCH .../proxies/{id}` to clear it, or call `POST .../proxies/{id}/locate` to re-run the lookup and get the updated proxy back (502 when the request through the proxy fails).

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

#### Unquarantine Proxy

Forcefully remove a proxy from quarantine:

```bash
POST /api/v1/projects/{project_id}/proxies/{proxy_id}/unquarantine
```

Returns `200` with `{"status": "ok", "proxy_id": "..."}` on success, `400` if the proxy is not quarantined.

### Get Connector Options

Get available regions, instance types, the country list and other static options:

```bash
GET /api/v1/connector-options
```

Provider-specific dynamic options (zones, entry nodes, sub-users) are served by the [provider options endpoint](#resolve-provider-options).

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

### Proxy location fields

Every proxy response carries the outcome of [IP attribution]({{ site.baseurl }}/ip-attribution): `country` (what routing uses), `country_source` (`database`, `vendor`, `endpoint` or `manual`), `vendor_country` (what the vendor claimed), `location_conflict` (the claim is contradicted) and `location` (region, city, coordinates, ASN and anonymity flags from the databases, when any covers the IP). Setting `country` by hand pins it as `manual`: attribution then verifies the exit against it instead of overwriting it.

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

**Import replaces all existing data** on the instance. The wipe and restore run
in a single transaction, so a failure leaves the existing data untouched.
Afterwards the live proxy cache is rebuilt, stale Redis state for the
replaced projects and proxies is purged, and IP attribution reloads its
settings and reopens whatever database files the backup carried. Other
instances pick the changes up on their periodic reload.

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

## System Statistics

Admin-only. One endpoint returning what exists in the install, what it costs in
Postgres and Redis, and which background workers are alive. This is what the
**Settings → System** page renders.

```bash
GET /api/v1/system/stats
```

Editors and viewers receive `403`.

**Response sections:**

| Section | Scope | Contents |
|---------|-------|----------|
| `runtime` | This instance | Version, instance id, role, uptime, ports, worker intervals. |
| `inventory` | Whole install | Exact counts of projects, credentials, connectors, proxies, users and providers, plus the live pool's health breakdown. |
| `projects` | Whole install | Per-project credential, connector and proxy counts, busiest first. |
| `database` | Whole install | Database size, per-table size (indexes included), server connections and this instance's pool usage. |
| `redis` | Whole install | Memory, throughput, hit rate and a breakdown of the keyspace by purpose. |
| `cache` | This instance | Entry counts of every in-memory cache the process holds. |
| `workers` | Mixed | `tasks` are this instance's background loops and their run counters; `leases` and `instances` are cluster-wide, and each entry in `instances` carries that instance's own `snapshot`. |

**Scope matters behind a load balancer.** `runtime`, `cache` and
`workers.tasks` describe only the instance that answered the request. With
several replicas, repeated calls may land on different instances and report
different numbers - that is accurate, not a bug. Everything Postgres- or
Redis-derived is the same from every instance.

**Every instance, from any instance.** Because a load balancer gives no way to
address a particular replica, each instance publishes those same three sections
about *itself* on its heartbeat, and they come back under
`workers.instances[].snapshot`:

| Field | Notes |
|-------|-------|
| `snapshot.runtime` / `snapshot.cache` / `snapshot.tasks` | The same shape as the top-level `runtime`, `cache` and `workers.tasks`, for that instance. |
| `snapshot.proxy_server_listening` / `proxy_server_connections` | That instance's proxy listener, as in the top-level `workers`. |
| `snapshot.geo_lookup_enabled` / `geo_lookups_in_flight` | That instance's exit-location lookups. |
| `age_seconds` | How long ago the snapshot was published - up to the 10s key TTL. Derived from the remaining TTL, so it does not depend on hosts agreeing on the time. `null` when there is no snapshot. |

A snapshot is republished every 5 seconds, so it is a recent reading rather
than a live one; the top-level sections remain live for the answering
instance, which is also listed (with its own, slightly older, snapshot).
`snapshot` is `null` for an instance running a version that predates
snapshots - during a rolling upgrade, for example. The **Settings → System**
page uses this to switch its per-instance cards between replicas.

**Cost.** Entity counts are exact. Table row counts are planner estimates
(`pg_class.reltuples`), so they stay cheap on metric tables with millions of
rows, drift until the next `ANALYZE`, and are `null` on a table autovacuum has
not reached yet. The Redis keyspace breakdown is a bounded `SCAN`; past 50,000
keys it reports a sample and sets `"truncated": true`.

**Example (abridged):**
```json
{
  "generated_at": "2026-09-19T21:52:35.740343",
  "runtime": { "version": "2.2.2", "role": "all", "uptime_seconds": 3841.2 },
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
        "table_bytes": 65536, "index_bytes": 229376 }
    ],
    "backends": 2, "pool_size": 5, "pool_checked_out": 1
  },
  "redis": {
    "used_memory_bytes": 1558312, "total_keys": 33, "ops_per_sec": 4,
    "groups": [ { "label": "Proxy health", "keys": 29 } ],
    "truncated": false
  },
  "workers": {
    "tasks": [
      { "name": "metrics_flusher", "scope": "singleton", "lease": "metrics_flusher",
        "state": "running", "error": null, "interval_seconds": 60,
        "runs": 412, "idle_runs": 0, "failures": 1, "consecutive_failures": 0,
        "overruns": 3, "consecutive_overruns": 0,
        "last_overrun_at": "2026-09-19T19:31:55.417002",
        "last_run_at": "2026-09-19T21:52:30.114221", "last_duration_ms": 84.2,
        "avg_duration_ms": 61.9, "max_duration_ms": 512.7,
        "last_error": "TimeoutError: ", "last_error_at": "2026-09-19T18:04:11.002913" }
    ],
    "leases": [
      { "name": "metrics_flusher", "kind": "Metrics flush to Postgres",
        "worker": "metrics_flusher", "target": null,
        "holder": "66eb615b-…", "held_by_self": true, "ttl_ms": 4027 }
    ],
    "instances": [ { "instance_id": "66eb615b-…", "role": "all", "is_self": true, "ttl_seconds": 9 } ]
  }
}
```

**Two kinds of worker health.** `state` and `error` describe the asyncio task:
not `running` means the loop on that instance has stopped, and `error` carries
the exception that ended it. The run counters describe the cycles *inside* the
loop - one health-check sweep, one metrics flush, one peer message applied - so
a loop that raises every cycle and recovers is still `running`, and only
`consecutive_failures` and `last_error` say it is broken. Counters are
per-process and reset with it; a cycle cancelled at shutdown is not counted.
Granularity is the cycle, not the item: the auto-scaler handles each connector
under its own `try`, so a cycle counts as a success even when one connector
failed.

**Runs that had nothing to do.** `idle_runs` is the subset of `runs` where the
loop ticked and returned early - the metric-delta publisher over an empty
buffer, a health-check sweep whose shard landed entirely on peers, a system
snapshot a peer already wrote, an auto-scaler cycle on an install with no cloud
connectors. Subtract it from `runs` for the cycles that did something. The
split matters because the two numbers answer different questions: an install
taking no traffic ticks the metric-delta publisher 17k times a day without a
single flush, and without `idle_runs` that reads as a busy worker. The
durations describe the working cycles only - an early return is not a
measurement of the work it skipped, and averaging it in would report a
publisher that spends 20ms on every real flush as taking 0.1ms. A worker with
no idle path (the heartbeat, the pub/sub subscribers) reports `0`.

**Cadence and overruns.** `interval_seconds` is the interval the loop was
started with, declared by the loop itself so it is the value actually slept on
rather than a config setting read elsewhere - worth knowing, because a worker
whose module was configured differently from the rest of the process will say
so here. It is `null` for the two pub/sub subscribers, which run when a peer
message arrives and so have no cadence to miss. `overruns` counts cycles that
took longer than that interval: a loop cannot start its next cycle until the
current one returns, so those cycles pushed the worker off its cadence - the
system snapshotter shows this as gaps in the trend charts, and the others as
work simply happening less often than configured.

`overruns` is a lifetime count, so read it with `consecutive_overruns`, which
the first cycle back inside the cadence resets: that pair distinguishes "slow
right now" from "hit one slow cycle during a restart and has been fine since",
and `last_overrun_at` says when the most recent one was. `failures` /
`consecutive_failures` work the same way. A climbing `consecutive_overruns`
with `failures` at zero is the signature of a worker that needs a longer
interval or less to do, rather than one that is broken.

**Which worker runs where.** `scope` is `singleton` for leader-elected loops
and `instance` for ones every instance runs. A singleton's `lease` matches a
`leases[].name` (for per-connector leases, the part before the `:`), and each
lease names its `worker` in return - so "which instance is doing this job right
now" (`leases`) and "how is it going on this instance" (`tasks`) line up. A
standby's singleton worker is legitimately at `"runs": 0`. Leader-elected work
is metrics flushing, compaction and system snapshots globally, auto-scaling and
provider sync (discovery and IP refresh) per connector.

**An absent lease means two different things**, which is what
`lease_per_resource` is for. A global singleton (`false`) holds its lease
continuously, so finding no holder in `leases` is a failover gap that closes
within seconds. A per-resource worker (`true` - the auto-scaler and provider
syncer) takes one lease per connector at the top of the work and releases it in
a `finally` a moment later, so between ticks **no instance holds one**, and
several instances can hold different ones at the same time. For those, an
absent lease is the resting state, not a standby; whether the loop is actually
working is answered by its run counters, not by `leases`.

### System Trends

```bash
GET /api/v1/system/stats/history?range=24h
```

Admin-only. Returns the gauge history behind the **Trends** charts on the
System page. Ranges: `1h`, `24h` (raw snapshots) and `7d`, `30d`, `90d`
(averaged into buckets).

Snapshots are written by the `system_snapshotter` worker on whichever instance
holds its lease, so unlike the per-instance sections of `/system/stats`, this
series reads the same from every instance.

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
  metrics_interval: 300        # seconds between snapshots; 0 disables the worker
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
| GET | `/geo/settings` | Install-wide settings (default source policy, echo endpoint, preflight, retention), whether an admin saved them, databases loaded on this instance |
| PUT | `/geo/settings` | Replace the settings (body: the `settings` object). Applies to every instance |
| GET | `/geo/databases` | Every database: stored rows plus config-file entries, with `loaded_here` and `load_error` |
| POST | `/geo/databases` | Upload a file (`multipart/form-data`: `file`, optional `name`, `priority`, `enabled`). Validated before it is stored; proxies are re-attributed |
| POST | `/geo/databases/from-url` | Register a vendor download URL (`name`, `update_url` which may carry `{YYYY}` and `{MM}` placeholders filled per run, `update_interval_hours`, `update_auth`, `priority`) and download it now. `update_auth` takes `{username, password}` for basic auth or `{token}` / `{header, token}` |
| POST | `/geo/databases/inspect` | Open an uploaded file and report its metadata without storing it |
| PATCH | `/geo/databases/{id}` | Change `name`, `enabled`, `priority`, `update_url`, `update_interval_hours`, `update_auth` |
| POST | `/geo/databases/{id}/refresh` | Download a scheduled database now |
| DELETE | `/geo/databases/{id}` | Remove a stored database everywhere |
| POST | `/geo/lookup` | `{ip, claimed_country?, project_id?}`: each database's answer and how the default policy, or the project's, resolves it |
| POST | `/geo/reattribute` | `{connector_id?}`: re-run attribution offline for every proxy with a known exit IP; returns `{updated}` |
| GET | `/geo/observations` | One page of observations, newest first, with `total` for paging. Filters apply on the server and mirror the table's columns: `connector_id`, `proxy_id`, `project_id`, `source`, `ip` (exact), `claimed_country`, `resolved_country`, `verdict` (`contradicted`, `uncertain`, `confirmed`, `no_claim`); paging with `limit` (max 1000) and `offset` |
| GET | `/geo/accuracy` | Per connector, over the distinct exit IPs last seen in the window: `exits`, `claimed`, `confirmed`, `contradicted`, `uncertain`, `accuracy` (confirmed over claimed) and the contradicted pairs with exit counts (`project_id`, `connector_id`, `days`) |
| GET | `/geo/exits` | Per connector: distinct exit IPs first seen in the window and ever, sightings, reuse (`project_id`, `connector_id`, `days`). Dynamic-sessions connectors carry `coverage` (`preflight_on`, `sampled_percent`): below 100 the figures for session-less traffic are undercounts |
| GET | `/geo/exits/ips` | One page of distinct exit IPs, most recently seen first, each with first and last sighting, hand-out count and the latest observation's state (proxy, source, vendor claim, resolved country and source, verdict flags). Filters: `project_id`, `connector_id`, `ip`, `proxy_id`, `country`, `claimed_country`, `verdict`; paging with `limit` (max 1000) and `offset`; `total` in the response |
| GET | `/geo/status` | Pipeline health: databases, buffered and stored observations, preflight counters |

Projects carry `location_policy` (`off`, `warn`, `strict`), `location_preflight` (`off`, `report`, `retry`, `reject`), and the optional overrides `location_sources` (ordered list of `database`, `vendor`, `endpoint`) and `location_conflict_rule` (`consensus`, `first`) on create, update and in responses. On update an empty list or empty string clears an override so the project inherits the install default again.

### Echo

```bash
GET /echo?nonce=<anything>
```

Public. Returns the caller's IP and, with a database loaded, its attribution:

```json
{"ip": "203.0.113.7", "country": "GB", "region": "England", "city": "London", "asn": 12345, "organization": "Example Ltd", "databases": ["<database id>"], "nonce": "…", "timestamp": "2026-09-21T12:00:00Z"}
```

Requested through a proxy it reports the proxy's exit. Behind a load balancer, list the balancer in `geo.echo.trusted_proxies` so the address comes from `X-Forwarded-For`.

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

