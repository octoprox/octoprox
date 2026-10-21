---
layout: docs
title: "Configuration Reference"
nav_id: configuration
description: "Every Octoprox setting: roles, environment variables, per-environment YAML files, authentication, logging, database and Redis connections, which role reads what, and production recommendations."
---

# Configuration

<p class="subtitle">Configure Octoprox using environment variables and YAML configuration files.</p>

## Overview

Settings are read, highest priority first, from environment variables (`OCTOPROX_*`), a `.env` file in the working directory, the YAML file for the environment, and built-in defaults. The YAML file is chosen by `OCTOPROX_ENV`: `development` (the default) reads `config/dev.yaml`, `production` reads `config/prod.yaml`, and any other value `config/<value>.yaml`. The image ships both files under `/app/config`.

Every process reads its own settings when it starts. In a cluster, each [role]({{ site.baseurl }}/architecture) reads only the settings it uses; [Settings per Role](#settings-per-role) lists which.

## Roles

| Variable | Description | Default |
|----------|-------------|---------|
| `OCTOPROX_ROLE` | What this process runs: `all` (everything in one process), `api`, `gateway`, `controller`, `worker` or `echo`. See [Architecture]({{ site.baseurl }}/architecture) | all |
| `OCTOPROX_INTERNAL_SECRET` | The secret the api, gateways and controllers share (and role `all`): it encrypts and authenticates the routing feed in Redis, and signs the gateways' calls to the api's internal endpoint. The same value on each of them, and different from `OCTOPROX_JWT_SECRET`. Workers and echo never read it. Keep it in the environment (or a secret store), like the JWT secret, not in a config file | change-me-in-production |
| `OCTOPROX_INTERNAL_API_URL` | Where gateways reach the api, such as `http://octoprox-internal-api:8000`: they fetch IP database files from it. A gateway left at the default logs `Gateway reaches the api at the default internal_api_url` at start; the default suits only role `all` | http://localhost:8000 |
| `OCTOPROX_WORKER_JOBS` | The job kinds this worker takes, comma-separated: `health`, `sync`, `scale`, `provision`, `reattribute`, `enrich`, `traffic`, `metrics_flush`, `compaction`, `snapshot`, `geo_update`, `tunnel_last_seen`, `index_hygiene`. Lets some workers run health checks only, so a large pool's checks never hold up syncs and scaling, or the IP database downloads (`geo_update`) run on workers sized for them. Every kind needs at least one worker taking it | every kind |
| `OCTOPROX_INSTANCE_ID` | Names this process in log lines, on the System page, as the holder of the controller lease and in the tunnel statuses it publishes | host name and a random suffix per boot |

The routing feed and the change stream every process follows have two intervals, in `config/*.yaml` under `feed:` or as environment variables:

| Option | Variable | Description | Default |
|--------|----------|-------------|---------|
| `feed.compile_interval_seconds` | `OCTOPROX_FEED_COMPILE_INTERVAL` | The least time between two compiles of the routing feed, on the controller: a change after a quiet spell is compiled at once, and changes that arrive sooner are compiled together | 1 |
| `feed.reconcile_interval_seconds` | `OCTOPROX_RECONCILE_INTERVAL` | How often every process reads in full what it follows, whatever the change stream announced: the controller compiles everything, gateways read the feed, the api and workers their definitions, and configured IP database files are looked at on disk. The net for a write that announced nothing (a crash between a commit and its entry, manual SQL) | 3600 |

A saved change reaches the gateways within moments: the controller compiles it as soon as its change entry arrives (later by up to one compile interval during a burst of writes), and every gateway is woken by the publish.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OCTOPROX_ENV` | Environment: picks `config/dev.yaml` (`development`), `config/prod.yaml` (`production`) or `config/<value>.yaml` | development |
| `OCTOPROX_REDIS_URL` | Redis connection URL; `rediss://` for TLS | redis://localhost:6379/0 |
| `OCTOPROX_REDIS_REQUEST_TIMEOUT` | Seconds one Redis connect may take, and one call on a request's path or right after a save, before it is given up (`redis.request_timeout_seconds`); the request path then fails open, and a save stands. Other replies may take up to 30 seconds | 2 |
| `OCTOPROX_LOG_LEVEL` | Logging level | INFO |
| `OCTOPROX_LOG_FORMAT` | Log output format: `console` or `json` | console |
| `OCTOPROX_AUTH_USERNAME` | Initial admin username (used to seed admin on first startup) | admin |
| `OCTOPROX_AUTH_PASSWORD` | Initial admin password (required) | (empty) |
| `OCTOPROX_JWT_SECRET` | Secret key for JWT token signing. Read only by the api (and role `all`) | change-me-in-production |
| `OCTOPROX_JWT_EXPIRY_HOURS` | JWT token expiry in hours | 24 |
| `OCTOPROX_INVITE_TOKEN_EXPIRY_HOURS` | How long an invite link stays valid, in hours | 168 |
| `OCTOPROX_PUBLIC_URL` | The URL users reach Octoprox at (scheme and host, no trailing slash): written into invite links and registered with sign-in providers as the redirect URI. Needed behind a load balancer or reverse proxy, where the request's own address is the wrong one | (the request's base URL) |
| `OCTOPROX_CORS_ORIGINS` | Origins the API accepts browser calls from besides its own, as a JSON list: only needed when the web UI is served from another origin, as by its development server | `["http://localhost:3000", "http://localhost:5173"]` |
| `OCTOPROX_PROXY_PORT` | The gateway's proxy listener port (`server.proxy_port`) | 8080 |
| `OCTOPROX_PROXY_PUBLIC_URL` | The URL clients reach the proxy at: `http://` or `https://`, a host and an optional port, such as `https://proxy.example.com` behind a load balancer that terminates TLS. Read by the api, which shows it on the [Connect page]({{ site.baseurl }}/connecting) and in its examples as given. Without a port clients use the scheme's default: `https://` URLs leave it out (443), `http://` ones name `80`, since curl reads a bare `http://` proxy as port 1080. Set it in a cluster: the api's own host is rarely the proxy's, and an api without it logs `The Connect page names the web UI's host as the proxy's` at start | (`http://` on the host the browser reached the web UI at, port `server.proxy_port`) |
| `OCTOPROX_DEFAULT_STRATEGY` | The strategy a project is routed with when no router has the one it stores (saved before names were checked, or since removed): the controller serves it in the feed in that one's place, and the api reports and previews it alike (`proxy.default_strategy`). A name no router has stops every role at start. A new project's strategy is set on the project itself | round_robin (`health_based` in `config/prod.yaml`) |
| `OCTOPROX_HEALTH_CHECK_INTERVAL` | Seconds between two health checks of a proxy (`proxy.health_check.interval_seconds`): the controller publishes each proxy's check once per interval, spread over it | 60 |
| `OCTOPROX_HEALTH_CHECK_TIMEOUT` | Seconds one health check may take (`proxy.health_check.timeout_seconds`) | 30 |
| `OCTOPROX_HEALTH_CHECK_CONCURRENCY` | Health checks one worker runs at once (`proxy.health_check.concurrency`). Each holds a connection through its proxy for up to the timeout | 50 |
| `OCTOPROX_IP_REFRESH_INTERVAL` | Seconds between two syncs of a provider connector: IP refresh and reconciliation (`proxy.ip_refresh_interval`), published by the controller | 3600 |
| `OCTOPROX_CONNECTION_TIMEOUT` | Seconds the gateway waits for an upstream proxy (`proxy.connection.timeout_seconds`) | 30 |
| `OCTOPROX_METRICS_FLUSH_INTERVAL` | Seconds between two flushes of the request counters into the metrics history (`database.metrics_flush_interval`): the controller publishes the flush job, and a worker runs it | 60 |
| `OCTOPROX_SYSTEM_METRICS_INTERVAL`, `OCTOPROX_SYSTEM_METRICS_RETENTION_DAYS` | The System page's trend snapshots (`system.metrics_interval`, `system.metrics_retention_days`); set the interval alike on the controller, which publishes the snapshot job, the workers, which take the snapshots and apply the retention, and the api, which charts them | 300, 90 |
| `OCTOPROX_TLS_MITM_CA_CERT_PATH` | Path to the MITM CA certificate | data/ca/octoprox-ca.crt |
| `OCTOPROX_TLS_MITM_CA_KEY_PATH` | Path to the MITM CA private key | data/ca/octoprox-ca.key |
| `OCTOPROX_PROVIDERS_DIR` | Directory of operator-supplied provider descriptor YAML files | (unset) |
| `OCTOPROX_PROVIDER_EGRESS_ALLOW_HTTP` | Allow descriptors to call plain-http vendor APIs (development only) | false |
| `OCTOPROX_PROVIDER_EGRESS_ALLOW_PRIVATE` | Allow descriptor API calls to private addresses (development only) | false |
| `OCTOPROX_PROVIDER_HTTP_TIMEOUT_SECONDS` | Timeout for a vendor API request made for a descriptor | 60 |
| `OCTOPROX_PROVIDER_HTTP_MAX_RESPONSE_BYTES` | Largest vendor API response accepted (0 disables) | 52428800 |

Every option in the sections below can be set from the environment as well: the variable is `OCTOPROX_` followed by the setting's name in upper case, which the sections give where it differs from the YAML key (`server.api_port` is `OCTOPROX_API_PORT`, `metrics.hosts.max_hosts` is `OCTOPROX_HOST_METRICS_MAX_HOSTS`). List and mapping settings take JSON, for example `OCTOPROX_GEO_ECHO_TRUSTED_PROXIES='["10.0.0.0/8"]'`.

## Authentication

Octoprox requires authentication for all web UI and API access. On first startup, an admin user is automatically created from `OCTOPROX_AUTH_USERNAME` and `OCTOPROX_AUTH_PASSWORD`.

There are three roles:
- **Admin** - Full access including user management
- **Editor** - Can manage projects, proxies, credentials, and connectors (no user management)
- **Viewer** - Read-only access to all data

Besides a password, a user can sign in with a Google, Microsoft or GitHub account once an admin has set the provider up, and scripts can authenticate with a personal access token instead of a password. See [Single Sign-On and API Tokens](sso).

### Configuration

Set the following environment variables:

```bash
export OCTOPROX_AUTH_USERNAME=admin
export OCTOPROX_AUTH_PASSWORD=your-secure-password
export OCTOPROX_JWT_SECRET=your-random-secret-key
export OCTOPROX_INTERNAL_SECRET=another-random-secret
```

Or create a `.env` file in the project root:

```env
OCTOPROX_AUTH_USERNAME=admin
OCTOPROX_AUTH_PASSWORD=your-secure-password
OCTOPROX_JWT_SECRET=your-random-secret-key
OCTOPROX_INTERNAL_SECRET=another-random-secret
```

In a cluster the first three go to the api alone, which signs users in; `OCTOPROX_INTERNAL_SECRET` goes to the api, gateways and controllers.

### Security Notes

- **Always set a strong `OCTOPROX_JWT_SECRET` and `OCTOPROX_INTERNAL_SECRET`** in production, two different values. The defaults are insecure. Gateways never get the JWT secret, so a gateway, the role that faces the internet, cannot mint a sign-in.
- **Never commit credentials** to version control. Use environment variables or `.env` files (which should be gitignored).
- The `/api/v1/auth/login` and `/api/v1/auth/status` endpoints are always public, as are the `/api/v1/auth/sso/*` endpoints the browser's sign-in round trip uses.
- The `/live`, `/ready`, `/health` and `/metrics` endpoints are always public, on every role, for probes, load balancer health checks and Prometheus. The bundled cluster's nginx refuses `/metrics` on its public port, where it would reach whichever api it picked: scrape each container's port 8000 instead.
- The internal endpoints under `/internal/v1` take a token derived from `OCTOPROX_INTERNAL_SECRET`, not a sign-in; only gateways call them, and the bundled nginx config refuses them on its public port.
- All other API endpoints require authentication.

## YAML Configuration

### Example Configuration File

The shipped `config/dev.yaml` and `config/prod.yaml` hold the options most installs touch, with comments. A minimal file, saved as `config/<env>.yaml` and selected with `OCTOPROX_ENV=<env>`:

```yaml
server:
  host: "0.0.0.0"
  api_port: 8000
  proxy_port: 8080

proxy:
  default_strategy: round_robin
  health_check:
    interval_seconds: 60
    timeout_seconds: 30
    concurrency: 50

tls_mitm:
  ca_cert_path: "data/ca/octoprox-ca.crt"
  ca_key_path: "data/ca/octoprox-ca.key"
```

### Server Configuration

| Option | Description | Default |
|--------|-------------|---------|
| `server.host` | Host address to bind to | 0.0.0.0 |
| `server.api_port` | Port the `octoprox` command serves on. The image, the compose files and `make run` serve port 8000 | 8000 |
| `server.proxy_port` | Port of the gateway's proxy listener | 8080 |
| `server.proxy_public_url` | URL clients reach the proxy at, as shown on the Connect page (`OCTOPROX_PROXY_PUBLIC_URL`) | (empty) |

### Proxy Configuration

| Option | Description | Default |
|--------|-------------|---------|
| `proxy.default_strategy` | The strategy for a project whose stored strategy no router has | round_robin |
| `proxy.health_check.interval_seconds` | Interval between two health checks of a proxy | 60 |
| `proxy.health_check.timeout_seconds` | Timeout for health check requests | 30 |
| `proxy.health_check.concurrency` | Health checks one worker runs at once | 50 |
| `proxy.ip_refresh_interval` | Interval between two syncs of a provider connector | 3600 |
| `proxy.connection.timeout_seconds` | How long the gateway waits for an upstream proxy | 30 |
| `proxy.geo_lookup.enabled` | Look up the exit IP and country of static proxies when they are added (one request through the proxy) | true |
| `proxy.geo_lookup.url` | JSON endpoint requested through the proxy. Also seeds the IP attribution echo endpoint; point it at an Octoprox `/echo` in production. | https://httpbin.org/ip |
| `proxy.geo_lookup.ip_path` | JMESPath to the IP in the response | origin |
| `proxy.geo_lookup.country_path` | JMESPath to the ISO country code in the response; empty when the endpoint reports none | (empty) |
| `proxy.geo_lookup.timeout_seconds` | Timeout for one lookup request | 15 |

The `proxy.geo_lookup` endpoint seeds the echo endpoint of the [IP attribution]({{ site.baseurl }}/ip-attribution) policy on a fresh install; once an admin saves the policy in the UI, the policy's `echo_url` is what is used.

A health check marks a proxy degraded at its first failure and unhealthy at the third in a row; only healthy proxies take requests. The controller publishes each proxy's check once per interval, spread over it; the probe and the status write are a worker's job; see [Architecture]({{ site.baseurl }}/architecture#jobs).

### IP Attribution

The install-wide settings (default conflict rule and city radius, echo endpoint, preflight, retention) are edited in Settings → IP attribution and stored in the database; projects may override the conflict rule and the city radius. The config file describes this process and seeds a fresh install:

| Option | Description | Default |
|--------|-------------|---------|
| `geo.cache_dir` | Where this process caches IP database files: copied from Postgres by the api and workers, fetched from the api by a gateway. A volume shared by every role saves the copies | data/geo |
| `geo.databases` | Operator-managed database files: list of `{path, name, priority, enabled}` entries loaded at startup; a GeoNames table (zipped or `.txt`) may be listed too. The echo role serves from these alone | [] |
| `geo.defaults` | Initial settings for a fresh install: `default_conflict_rule`, `default_city_radius_km`, `echo_url`, `echo_ip_path`, `echo_country_path`, `echo_state_path`, `echo_city_path`, `echo_timeout_seconds`, `health_check_attribution`, `preflight_session_ttl_seconds`, `preflight_max_attempts`, `observation_retention_days`, `exit_ip_retention_days` | built-in |
| `geo.echo.enabled` | Serve the public `/echo` endpoint on the api | true |
| `geo.echo.trusted_proxies` | CIDRs of load balancers whose `X-Forwarded-For` the `/echo` endpoint trusts for the client IP | [] |
| `geo.observations.publish_interval_seconds` | How often buffered IP observations and preflight sightings are appended to their Redis streams; the workers write them to Postgres as they arrive | 5 |
| `geo.observations.max_buffer` | Observations (and sightings) one process keeps in memory before dropping the oldest | 5000 |
| `geo.updater.check_interval_seconds` | How often the controller publishes the `geo_update` job, which downloads on a worker the scheduled databases that are due | 3600 |

Environment variables: `OCTOPROX_GEO_CACHE_DIR`, `OCTOPROX_GEO_DATABASES`, `OCTOPROX_GEO_POLICY_DEFAULTS`, `OCTOPROX_GEO_ECHO_ENABLED`, `OCTOPROX_GEO_ECHO_TRUSTED_PROXIES`, `OCTOPROX_GEO_OBSERVATION_PUBLISH_INTERVAL`, `OCTOPROX_GEO_OBSERVATION_MAX_BUFFER`, `OCTOPROX_GEO_UPDATER_INTERVAL`; the lookup ones are `OCTOPROX_GEO_LOOKUP_ENABLED`, `OCTOPROX_GEO_LOOKUP_URL`, `OCTOPROX_GEO_LOOKUP_IP_PATH`, `OCTOPROX_GEO_LOOKUP_COUNTRY_PATH` and `OCTOPROX_GEO_LOOKUP_TIMEOUT_SECONDS`.

Connector config keys read by the health check: `healthcheck_url` (custom check URL; without it the attribution echo endpoint is checked), `healthcheck_ip_path` (JMESPath, or `@text`, of the exit IP in that URL's response; unset means the response carries no IP unless the URL is the echo or httpbin endpoint) and `healthcheck_country_path`.

### WireGuard

```yaml
tunnel:                    # the data plane every tunnel protocol shares
  transparent_port: 8081   # tunnel TCP is redirected here (bound on the tunnel gateway addresses)
  dns_port: 5353           # tunnel DNS is redirected here
  fake_ip_range: 198.18.0.0/15
  sniff_timeout_seconds: 2
  block_encrypted_dns: true  # close DoT and known DoH resolvers so devices keep the tunnel resolver

wireguard:
  enabled: false           # terminate the tunnel on this gateway (CAP_NET_ADMIN, wireguard-tools, nftables)
  interface: wg0
  # listen_port: 51820     # only when it differs from the install's endpoint port
  mtu: 1420
  defaults:                # seeds the install-wide row on a fresh install
    endpoint_host: vpn.example.com
    endpoint_port: 51820
    subnet: 10.66.0.0/16
```

Environment variables: `OCTOPROX_TUNNEL_TRANSPARENT_PORT`, `OCTOPROX_TUNNEL_DNS_PORT`, `OCTOPROX_TUNNEL_FAKE_IP_RANGE`, `OCTOPROX_TUNNEL_SNIFF_TIMEOUT_SECONDS`, `OCTOPROX_TUNNEL_BLOCK_ENCRYPTED_DNS`, `OCTOPROX_WIREGUARD_ENABLED`, `OCTOPROX_WIREGUARD_INTERFACE`, `OCTOPROX_WIREGUARD_LISTEN_PORT`, `OCTOPROX_WIREGUARD_MTU`.

The key pair, public endpoint and tunnel subnet are install-wide, stored in Postgres and edited under **Settings → WireGuard**. The `tunnel:` and `wireguard:` blocks are read by the gateways that terminate the tunnel; `defaults` by the api, which seeds the install-wide row. See [WireGuard Devices](wireguard) for what each setting does and what a terminating gateway needs.

## OpenVPN

```yaml
openvpn:
  enabled: false           # run the daemon on this gateway (CAP_NET_ADMIN, /dev/net/tun, openvpn, nftables)
  interface: ovpn0
  # listen_port: 1194      # only when it differs from the install's endpoint port
  mtu: 1500                # tun-mtu of the daemon's interface
  defaults:                # seeds the install-wide row on a fresh install
    endpoint_host: vpn.example.com
    endpoint_port: 1194
    protocol: udp          # or tcp
    subnet: 10.67.0.0/16
```

Environment variables: `OCTOPROX_OPENVPN_ENABLED`, `OCTOPROX_OPENVPN_INTERFACE`, `OCTOPROX_OPENVPN_LISTEN_PORT`, `OCTOPROX_OPENVPN_MTU`.

The CA, server certificate, public endpoint, transport and subnet are install-wide, stored in Postgres and edited under **Settings → OpenVPN**. The `tunnel:` block above is shared with WireGuard. See [OpenVPN Devices](openvpn).

## Logging

```yaml
logging:
  level: INFO
  format: json
```

| Option | Description | Default |
|--------|-------------|---------|
| `logging.level` | Minimum level to emit: DEBUG, INFO, WARNING, ERROR, CRITICAL | INFO |
| `logging.format` | `console` for human-readable output, `json` for one JSON object per line | console |

Application, uvicorn and SQLAlchemy log lines all render through the same formatter, so a log shipper sees one shape. Use `json` in production and `console` in development. Both formats stamp each line in UTC: `json` as ISO 8601 ending in `Z`, `console` as `YYYY-MM-DD HH:MM:SS`.

Every line carries an `instance` field holding `OCTOPROX_INSTANCE_ID` and a `role` field holding the process's role, so the output of every process can be collected in one place and still be told apart. The default instance id is the host name with a random suffix per boot (a container or pod name you recognise); the cluster compose files set a stable one per service. The Redis URL is logged with its password masked, and probe requests (`/live`, `/ready`, `/health`, `/metrics`) are left out of the access log below `DEBUG`.

### Request IDs

Every API request gets a request ID. The API accepts one from the caller in the `X-Request-ID` header (up to 128 characters of letters, digits, `.`, `_`, `:` and `-`) and generates one otherwise. The ID used is always returned in the `X-Request-ID` response header, including on error responses.

While a request is being handled, every log line carries `request_id`, and once the bearer token is verified, `user_id` and `username` as well. A user report of a failed action can therefore be matched to the server logs by the ID shown in the response, and log lines can be filtered to one person without any per-call-site plumbing.

The web UI sends its own ID on every call, so browser-side failures are traceable the same way.

## Host Metrics

Every request is counted against the destination host it was for, under the connector that carried it, for the project's Hosts page (see [Metrics]({{ site.baseurl }}/metrics#hosts-where-the-traffic-goes)). Every host is kept by default. It is the one history table whose size depends on what clients request rather than on how the pool is set up, so an install that crawls the open web has two dials:

```yaml
metrics:
  hosts:
    enabled: true   # false records nothing per host; the Hosts page then stays empty
    max_hosts: 0    # 0 keeps every host; N names the N busiest per project per 5-second window
```

| Setting | Description | Default |
|---------|-------------|---------|
| `metrics.hosts.enabled` | Count requests and bytes per destination host and connector | `true` |
| `metrics.hosts.max_hosts` | A ceiling on how many hosts each gateway names per project in each 5-second metrics window, busiest by requests first; the rest are folded into a single `(other)` row, so their requests and bytes stay in the totals. `0` keeps every host | `0` |

A ceiling is for pools that crawl: a project that touches a million new domains a day writes a row per domain, and the Hosts page then has to aggregate tens of millions of rows per load. For a pool aimed at a few to a few thousand target sites the table stays small with no ceiling at all. The ceiling applies per gateway, so with more gateways more hosts are named in a window.

Both can also be set as `OCTOPROX_HOST_METRICS_ENABLED` and `OCTOPROX_HOST_METRICS_MAX_HOSTS`. Both go on the gateways, which do the counting; the api reads `enabled` too, to tell the Hosts page whether counting is on, so set that one alike on both. Rows follow the project's metrics retention and the same compaction tiers as the other history tables.

## Failure Log

Why requests failed or were refused, per project, for the Failures page (see [Metrics]({{ site.baseurl }}/metrics#failures-why-requests-failed)). Every failure is counted into the `failure_metrics` history table, which follows the project's metrics retention and compaction like the other history tables. Examples of failures are kept in Redis, one capped stream per project and kind; identical failures within a 5-second window are folded into one example with a count.

```yaml
failure_log:
  enabled: true
  max_entries: 1000      # examples per project and kind, newest kept
  retention_hours: 168
  max_per_window: 100    # distinct examples per project and kind per 5-second window, per gateway
```

| Setting | Description | Default |
|---------|-------------|---------|
| `failure_log.enabled` | Count failures and keep examples. Off records neither | `true` |
| `failure_log.max_entries` | How many examples each project keeps of each kind of failure, newest first (at least 10) | `1000` |
| `failure_log.retention_hours` | How long examples are kept; a kind that stops failing loses its examples this long after the last one. The counts follow the metrics retention instead | `168` |
| `failure_log.max_per_window` | How many distinct examples of each kind one gateway keeps per project in each 5-second window. Beyond that, failures are only counted | `100` |

An example is a few hundred bytes, so a kind that fills its stream at the default takes a few hundred kilobytes of Redis, and a project takes that for each kind it actually sees. Lower `max_entries` on an install with many projects: the counts stay complete whatever it is set to. The settings can also be set as `OCTOPROX_FAILURE_LOG_ENABLED`, `OCTOPROX_FAILURE_LOG_MAX_ENTRIES`, `OCTOPROX_FAILURE_LOG_RETENTION_HOURS` and `OCTOPROX_FAILURE_LOG_MAX_PER_WINDOW`; gateways record the failures and the api shows them, so set them alike on both.

## Database Configuration

Octoprox uses PostgreSQL for persistent storage. The api, controller and worker connect to it (and role `all`); gateways and echo never do.

| Variable | Description | Default |
|----------|-------------|---------|
| `OCTOPROX_DB_HOST` | PostgreSQL host | localhost |
| `OCTOPROX_DB_PORT` | PostgreSQL port | 5432 |
| `OCTOPROX_DB_NAME` | Database name | octoprox |
| `OCTOPROX_DB_USER` | Database user | postgres (`octoprox` in the shipped config files) |
| `OCTOPROX_DB_PASSWORD` | Database password | (empty; `octoprox` in the shipped config files) |
| `OCTOPROX_DB_APPLICATION_NAME` | The name of this process's sessions in `pg_stat_activity` (`database.application_name`) | octoprox-&lt;role&gt; |

The standard libpq variables `PGSSLMODE` and `PGSSLROOTCERT` set TLS for every connection, migrations included.

Each process keeps a pool of up to 15 connections (5, plus 10 under load). Every process that connects migrates the schema to the current version before it serves; several starting together take turns through a lock, the others logging `Waiting for the migration lock` meanwhile.

## Redis Configuration

Redis carries what the roles hand each other: the routing feed (encrypted with `OCTOPROX_INTERNAL_SECRET`) and the change and runtime streams it is built on, request counters until the metrics flush job takes them, the work in hand (a stream per job kind, the preflight sightings and the IP observations), per-key request state (sticky sessions, rate-limit windows, quarantines, preflight verdicts, tunnel fake IPs), traffic blocks, failure examples, MITM captures, sign-in state and each process's heartbeat. Every key starts with `o6:`. Postgres stays the source of truth: losing Redis loses soft state only (see [Architecture]({{ site.baseurl }}/architecture#redis-loss-and-recovery)).

| Variable | Description | Default |
|----------|-------------|---------|
| `OCTOPROX_REDIS_URL` | Full Redis connection URL, with the password when Redis requires one (`redis://:password@host:6379/0`), or `rediss://` for TLS | redis://localhost:6379/0 |
| `OCTOPROX_REDIS_REQUEST_TIMEOUT` | Seconds one Redis connect may take, and one call on a request's path or right after a save, before it is given up (`redis.request_timeout_seconds`); other replies may take up to 30 seconds, since what they carry is sent again on a failure | 2 |

Every role but echo connects to Redis. Give it a password and keep it on a private network: whoever can write to it can disturb routing. It must be Redis 7.2 or later: connections speak RESP3 (`HELLO 3`) and introduce the client library with `CLIENT SETINFO`, which older servers refuse, and some commands are 6.2's (`XAUTOCLAIM`, `ZADD GT`).

## Settings per Role

In a cluster each role reads only part of the settings. Give each what it reads, and nothing else: a gateway, which faces the internet, never needs database credentials or the JWT secret.

| Setting or mount | api | gateway | controller | worker | echo |
|------------------|-----|---------|------------|--------|------|
| `OCTOPROX_ROLE` | `api` | `gateway` | `controller` | `worker` | `echo` |
| Database (`OCTOPROX_DB_*`, `PGSSLMODE`, `PGSSLROOTCERT`) | yes | | yes | yes | |
| `OCTOPROX_REDIS_URL`, `OCTOPROX_REDIS_REQUEST_TIMEOUT` | yes | yes | yes | yes | |
| `OCTOPROX_INTERNAL_SECRET` | yes | yes | yes | | |
| `OCTOPROX_JWT_SECRET`, `OCTOPROX_JWT_EXPIRY_HOURS`, `OCTOPROX_AUTH_*`, `OCTOPROX_PUBLIC_URL`, `OCTOPROX_INVITE_TOKEN_EXPIRY_HOURS`, `OCTOPROX_CORS_ORIGINS` | yes | | | | |
| `OCTOPROX_PROXY_PUBLIC_URL` | yes | | | | |
| `OCTOPROX_INTERNAL_API_URL` | | yes | | | |
| `server.proxy_port` | yes (the Connect page's default) | yes | | | |
| `proxy.connection.timeout_seconds` | | yes | | | |
| MITM CA files (`data/ca`) | certificate | certificate and key | | | |
| `tunnel.*`, `wireguard.*`, `openvpn.*` (and `NET_ADMIN`, `/dev/net/tun`) | | gateways that terminate a tunnel | | | |
| `wireguard.defaults`, `openvpn.defaults` | yes | | | | |
| `geo.cache_dir`, `geo.databases` | yes | yes | | yes | yes |
| `geo.echo.*` | yes | | | | yes |
| `geo.defaults`, `proxy.geo_lookup.*` | yes | | yes | yes | |
| `geo.observations.*` | yes | yes | | yes | |
| `geo.updater.check_interval_seconds` | | | yes | | |
| `proxy.default_strategy` | yes | yes | yes | | |
| `proxy.health_check.interval_seconds`, `proxy.ip_refresh_interval` | | | yes | | |
| `proxy.health_check.timeout_seconds`, `proxy.health_check.concurrency` | | | | yes | |
| `feed.reconcile_interval_seconds` | yes | yes | yes | yes | |
| `feed.compile_interval_seconds` | | | yes | | |
| `database.metrics_flush_interval` | | | yes | | |
| `system.metrics_interval` | yes | | yes | yes | |
| `system.metrics_retention_days` | | | | yes | |
| `metrics.hosts.*`, `failure_log.*` | yes | yes | | | |
| `OCTOPROX_WORKER_JOBS` | | | | yes | |
| Provider descriptors (`providers.*`, plugins) | yes | yes | yes | yes | |
| Cloud identity (instance role, IRSA, Workload Identity), `config/squid_setup.sh` | | | | yes | |

Role `all` reads everything. `OCTOPROX_ENV`, `OCTOPROX_LOG_*` and `OCTOPROX_INSTANCE_ID` apply to every role. [Running on Kubernetes]({{ site.baseurl }}/kubernetes#configuration-and-secrets-per-role) shows the same split as Secrets and mounts.

## Production Recommendations

1. **Set strong secrets**: Always use secure, random values for `OCTOPROX_JWT_SECRET`, `OCTOPROX_INTERNAL_SECRET` and `OCTOPROX_AUTH_PASSWORD`.

2. **Use environment variables**: Don't hardcode sensitive values in configuration files.

3. **Give each role only its settings**: Database credentials and the JWT secret never go to gateways (see [Settings per Role](#settings-per-role)).

4. **Use HTTPS**: Deploy behind a reverse proxy (nginx, Caddy) with TLS termination, and set `OCTOPROX_PUBLIC_URL` and `OCTOPROX_PROXY_PUBLIC_URL` to the addresses users and clients reach.

5. **Monitor health**: Point load balancer health checks at `/ready` (or `/health`, which answers the same with the process's identity) on port 8000 of the role behind them, the gateways for the proxy port, and scrape `/metrics` (see [Deployment & Scaling]({{ site.baseurl }}/deployment#health-endpoints)).
