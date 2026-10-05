---
layout: docs
title: Deployment & Scaling
nav_id: deployment
---

# Deployment & Scaling

<p class="subtitle">Octoprox runs as a single instance for development or as a horizontally-scaled cluster fronted by an L4 load balancer. The same binary serves both - you only change the topology.</p>

## Two Deployment Shapes

### Single instance (default)

One Octoprox process, one Postgres, one Redis. Fine for development, demos,
and small production workloads (one host, tens of thousands of proxies, a
few hundred concurrent tunnels). The WireGuard port is optional: see
[WireGuard Devices](wireguard) for enabling it.

```
        client ──┐
                 ▼
        ┌─────────────────────────┐         ┌──────────┐
        │ Octoprox                │ ──────▶ │ Postgres │
        │  :8000 API + UI         │         │  Redis   │
        │  :8080 proxy port       │         └──────────┘
        │  :51820/udp WireGuard   │
        └─────────────────────────┘
                 │
                 ▼
        upstream proxy pool
```

Compose files: [`docker-compose.yml`](https://github.com/octoprox/octoprox/blob/main/docker-compose.yml) (build from source) or [`docker-compose.ghcr.yml`](https://github.com/octoprox/octoprox/blob/main/docker-compose.ghcr.yml) (pre-built image).

### Multi-instance cluster (HA + horizontal scaling)

Multiple identical Octoprox processes behind a load balancer (nginx in the
bundled compose; any balancer that forwards HTTP, TCP and UDP works). All
instances share one Postgres and one Redis, and every instance terminates
the WireGuard tunnel.

```
   client ──▶ ┌────────────┐     ┌── Octoprox-1 ──┐
   device ──▶ │  nginx     │     │  :8000 :8080   │ ──┐
              │  :8000     │ ───▶├── Octoprox-2 ──┤   │
              │  :8080     │     │  :8000 :8080   │ ──┼──▶  Postgres
              │  :51820/udp│     ├── Octoprox-3 ──┤   │     Redis
              └────────────┘     │  :8000 :8080   │ ──┘
                                 └────────────────┘
                                          │
                                          ▼
                                   upstream proxy pool
```

Compose files: [`docker-compose.cluster.yml`](https://github.com/octoprox/octoprox/blob/main/docker-compose.cluster.yml) (build from source) or [`docker-compose.cluster.ghcr.yml`](https://github.com/octoprox/octoprox/blob/main/docker-compose.cluster.ghcr.yml) (pre-built image). nginx config: [`nginx/nginx.conf`](https://github.com/octoprox/octoprox/blob/main/nginx/nginx.conf).

```bash
# Local-build cluster (Makefile targets, fast iteration)
make cluster-up
make cluster-logs
make cluster-down

# Production-ready cluster (pre-built GHCR image) - invoke docker compose directly
docker compose -f docker-compose.cluster.ghcr.yml up -d
docker compose -f docker-compose.cluster.ghcr.yml logs -f
docker compose -f docker-compose.cluster.ghcr.yml down
```

## What you gain by running N instances

- **High availability.** Any instance can die - clients keep being served by
  the others. Background workers (metrics flusher, autoscaler, etc.) fail
  over to a surviving instance within ~5 seconds.
- **Request throughput.** Tunnel termination, TLS/MITM relay, credential
  resolution, and routing decisions all run per-request on the receiving
  instance - N instances ≈ N× concurrent connections handled in parallel.
- **Aggregate bandwidth + file descriptors.** Each host contributes its own
  NIC and `ulimit`.
- **Sharded health-check capacity.** Each proxy is checked by exactly one
  instance at a time (rendezvous-hashed by `proxy_id` across the live
  membership). Adding instances divides the workload.
- **Zero-downtime deploys.** Rolling-restart one instance at a time.


### The WireGuard endpoint in a cluster

Every replica terminates the tunnel, and nginx forwards the UDP port with
consistent hashing by source address, so a device keeps landing on the
same replica while its address holds and the others carry other devices.
The key pair, endpoint and device list are install-wide, so every replica
accepts every device's config, and the fake-IP mapping the tunnel DNS hands
out is shared through Redis, so a connection that lands on a different
replica from the one that answered the device's DNS query still knows the
name. Two things to know:

- **A flow that moves stalls briefly.** A device that changes source
  address (a phone switching networks) or whose replica disappears is
  hashed to another replica, which has no session keys for it yet. The
  device's next handshake attempt (a few seconds, or at the next rekey)
  establishes a session there and traffic resumes. Open TCP connections
  from before the move are gone, as with any L4 balancer.
- **nginx notices a dead container, not a dead host.** Open source nginx
  has no active UDP health check. A replica whose container is down is
  skipped (the host answers with ICMP unreachable); one whose host is
  gone keeps its share of devices until it returns or the server list is
  edited. For a hands-off alternative run keepalived with a floating IP
  in front of the replicas instead of balancing the UDP port, or use a
  cloud L4 balancer (AWS NLB, GCP passthrough NLB, Azure LB) which checks
  health actively.

Each carrying replica publishes its peer status to Redis and the admin
views merge them, so the device list is correct whichever replica a device
is currently talking to; the newest handshake is also written to the
device's row, so where a device was last seen survives the replica
restarting. A device's traffic is metered on the proxy path of whichever
replica relays it and flows into the shared metrics pipeline, so its totals
and history are cluster-wide like a connector's. The endpoint a device
appears to connect from is nginx's address, since nginx proxies the
datagrams. See [WireGuard Devices](wireguard).

## What does *not* scale by adding instances

- **In-memory cache.** Every instance still holds the full
  projects/credentials/connectors/proxies cache in RAM. 5 instances =
  5× the same memory footprint. (See the
  [TODO-control-data-plane-split.md](https://github.com/octoprox/octoprox/blob/main/TODO-control-data-plane-split.md)
  plan for the future tier-split that fixes this.)
- **Postgres write throughput.** Definitions and historical metrics still
  live in one DB; the metrics flusher is leader-elected so only one
  instance writes at a time.
- **Redis throughput.** All instances hit the same Redis for sticky
  sessions, rate-limit windows, quarantine state, metrics counters,
  heartbeats, and leases.
- **Cloud-provider API quotas.** Per-connector lease means only one
  instance calls AWS/GCP/Azure for a given connector at a time -
  intentional, so you don't get throttled by the cloud.

Rule of thumb: a cluster scales *concurrent request handling* and gives you
HA. To scale beyond what a single Redis or a single host's memory can take,
see [TODO-control-data-plane-split.md](https://github.com/octoprox/octoprox/blob/main/TODO-control-data-plane-split.md)
for the planned tier split.

## How it works under the hood

A few small mechanisms keep N instances in sync without a coordinator.

### Heartbeat (instance discovery)

Every instance writes a Redis key `instance_registry:<instance_id>` with a
10-second TTL, refreshed every 5 seconds. On graceful shutdown it deletes
its key; on hard kill, Redis expires it. The set of live keys is the
membership snapshot used by everything below.

The same beat writes `instance_stats:<instance_id>`, holding what only that
process can see: its runtime, its in-memory cache sizes and its
background-worker run counters. It costs nothing to collect - all three are
in-memory reads - and it is what lets the admin **Settings → System** page
show the workers of *every* instance, rather than only whichever one the load
balancer routed the request to. Both keys are written in one pipeline and
deleted together, so a peer never reports on an instance it thinks is gone.

### Cross-instance event bus

Mutations on one instance reach the others over Redis Pub/Sub on the
`octoprox:events` channel. Messages carry only
`(signal_name, instance_id, entity_id, op)` - receivers re-read the entity
from Postgres or Redis and update their cache. The instance that
published drops its own echo so no infinite loops.

Cross-instance signals: `project_changed`, `credential_changed`,
`connector_changed`, `proxy_changed`, `proxy_quarantine_changed`.

`op` is normally `added` / `updated` / `removed`. `proxy_changed` adds one
more: `status`, published when a health check flips a proxy between healthy,
degraded and unhealthy. Those fields live in Redis and never in the proxies
table, so peers refresh them with a single Redis read instead of reloading the
row from Postgres. Health flips are the most frequent event on the channel -
on an idle install with a flapping pool they are the *only* traffic on it - and
each one reaches every other instance, so the read they used to cost was
multiplied by the cluster size.

A 60-second full-reload from Postgres runs in the background as a
safety net for any messages dropped by Redis Pub/Sub (which is
fire-and-forget).

### Leader election for singleton workers

Some background work must run on exactly one instance at a time. Octoprox
uses Redis leases (`SET NX PX` with refresh + owner-checked release) for
this. If the leader dies, its lease expires within ~5 seconds and a
standby takes over on its next poll.

| Worker            | Scope              | Why leader-elected                          |
|-------------------|--------------------|----------------------------------------------|
| Metrics flusher   | Global             | Two writers would double-count Postgres rows |
| Metrics compactor | Global             | Compaction races on the same source rows     |
| System snapshotter| Global             | N instances would store N copies of one reading |
| Autoscaler        | Per-connector      | Two scalers would double-provision cloud VMs |
| Provider syncer   | Per-connector      | Two syncers would call provider APIs twice   |

Per-connector leases mean different connectors can be served by different
instances in parallel - only the same connector is single-writer.

### Sharded health checks

Health-checking is the highest-volume background task. Each proxy is
assigned to exactly one instance at a time via rendezvous hashing (HRW)
over the live `instance_registry:*` membership. When an instance joins
or leaves, only ~1/N of proxies move owner; the rest stay put.

### Per-request side effects

Sticky-session bindings live in Redis (key
`sticky:<project_id>:<session_id>`) and are read-through on every
selection, so a session opened on one instance keeps its upstream proxy
even if the next request lands on a different one. The rate-limiter
sliding window lives in a Redis sorted set written atomically via a Lua
script, so N instances see one combined request rate per proxy.
Quarantine is a TTL'd Redis key, with a `proxy_quarantine_changed` Pub/Sub
event so peers refresh their local quarantine cache the moment one
instance trips a limit.

## Operating the cluster

### Endpoints

| Port      | Served by | What                                                  |
|-----------|-----------|-------------------------------------------------------|
| 8000      | nginx     | API + Web UI (HTTP, sticky per client, see below)     |
| 8080      | nginx     | Proxy traffic (TCP, least-conn across replicas)       |
| 51820/udp | nginx     | WireGuard (UDP, each device pinned to one replica)    |

Health is passive, as open source nginx does it: a replica that refuses or
times out is skipped for ten seconds and the request goes to the next one.
For HTTP and TCP that is immediate. For UDP a dead container is noticed
through the ICMP unreachable its host answers with; a dead host is not,
and its devices reconnect once their replica is back or they are moved
(see the WireGuard section above). If you need active health checks on the
UDP port, Envoy's UDP proxy with cluster health checks, or a cloud L4
balancer, provides them.

### Echo endpoint

Every instance serves `GET /echo` on port 8000 for [IP attribution]({{ site.baseurl }}/ip-attribution):
health checks, discovery and preflight request it *through* a proxy to learn
the exit IP. The bundled nginx config adds `X-Forwarded-For` on the API
listener; set `geo.echo.trusted_proxies` to the nginx address range so the
instances report the real client instead of the balancer.

The request travels out through the vendor and back in from the public
internet, so the echo URL in the attribution policy must be reachable from
there. When the cluster sits on a private network, run the standalone echo
service (`octoprox-echo`, same image, port 8090, no Postgres or Redis) on a
public host and point the policy at it.

### Read-your-writes on the API

Each instance serves reads from its in-memory copy of projects,
credentials, connectors and proxies and learns about writes made on other
instances through Redis a moment later. A client that creates a credential
on one replica and immediately lists credentials on another can therefore
miss its own write for a few milliseconds, which in the UI looked like a
saved item not appearing in the table.

The bundled nginx config avoids this by pinning each bearer token to one
replica (`hash $octoprox_api_key consistent` in `nginx/nginx.conf`). The
UI and API clients send the token on every call and it stays the same for
the life of a login, so each signed-in user lands on one replica, and a
whole company behind one office address or VPN is spread across them.
Calls without a token (login, the SPA assets, `/health`, `/echo`) hash on
the client address instead; none of them need the pin. If the pinned
replica is down the request is retried on another (`proxy_next_upstream`).
If you front Octoprox with a different load balancer, configure the
equivalent affinity on the `Authorization` header, or a cookie-based one.

A client that logs in again between a write and a read gets a new token
and possibly another replica, and may see the pre-write state for a few
milliseconds; retry the read if that matters.

### Inspecting cluster state

```bash
# Live membership
docker compose -f docker-compose.cluster.yml exec redis \
  redis-cli KEYS 'instance_registry:*'

# Active leases (and who holds them)
docker compose -f docker-compose.cluster.yml exec redis \
  redis-cli --scan --pattern 'lease:*' | while read k; do
    echo "$k held by $(docker compose -f docker-compose.cluster.yml exec -T redis redis-cli GET "$k")"
  done

# Which replica served what (nginx logs the upstream address on every line)
docker compose -f docker-compose.cluster.yml logs --tail=50 nginx
```

### Failover smoke test

```bash
# Note who's holding the metrics flusher lease
docker compose -f docker-compose.cluster.yml exec redis \
  redis-cli GET lease:metrics_flusher

# Kill that instance
docker stop octoprox-1   # (or whichever holds it)

# Within ~5s a different instance owns the lease
docker compose -f docker-compose.cluster.yml exec redis \
  redis-cli GET lease:metrics_flusher
```

### Production checklist

Before pointing real traffic at the cluster, edit either compose file:

- Set `OCTOPROX_AUTH_PASSWORD` to something strong (not `admin`).
- Set `OCTOPROX_JWT_SECRET` to a long random string.
- Set `OCTOPROX_DB_PASSWORD` (and the matching `POSTGRES_PASSWORD`).
- Decide whether to expose Postgres (5433) and Redis (6379) on the host -
  for an internet-facing host you almost certainly want to remove those
  port mappings and keep them on the internal Docker network only.
- Mount the MITM CA from durable storage (or a Secret manager) so all
  instances trust the same root and CA rotations propagate cleanly.
- Take a passphrase-encrypted backup from **Settings → Backup & Migration**
  (or `POST /api/v1/backup/export`) before upgrades, and store it outside the
  cluster. Importing on a fresh instance with "Keep my current account" ticked
  is the supported way to migrate between deployments - see
  [api.md](api.md#backup--migration).

## When to outgrow this

A fleet of identical instances sharing one Redis + Postgres scales
request handling and gives you HA. It hits a ceiling when one of the
shared resources saturates - typically Redis throughput at a few tens of
thousands of proxied requests per second, or per-host memory when the
proxy pool grows past ~100k entries.

The next step is a tiered topology that splits the control plane (CRUD,
config) from the data plane (request termination) and replaces the
"every instance loads everything" cache with a snapshot pushed from the
control plane. The design is documented in
[TODO-control-data-plane-split.md](https://github.com/octoprox/octoprox/blob/main/TODO-control-data-plane-split.md)
in the repo - read that when you genuinely need it, not before.
