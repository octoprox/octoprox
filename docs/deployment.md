---
layout: docs
title: Deployment & Scaling
nav_id: deployment
---

# Deployment & Scaling

<p class="subtitle">Octoprox runs as a single instance for development or as a horizontally-scaled cluster fronted by an L4 load balancer. The same binary serves both — you only change the topology.</p>

## Two Deployment Shapes

### Single instance (default)

One Octoprox process, one Postgres, one Redis. Fine for development, demos,
and small production workloads (one host, tens of thousands of proxies, a
few hundred concurrent tunnels).

```
        client ──┐
                 ▼
        ┌────────────────────┐         ┌──────────┐
        │ Octoprox           │ ──────▶ │ Postgres │
        │  :8000 API + UI    │         │  Redis   │
        │  :8080 proxy port  │         └──────────┘
        └────────────────────┘
                 │
                 ▼
        upstream proxy pool
```

Compose files: [`docker-compose.yml`](https://github.com/octoprox/octoprox/blob/main/docker-compose.yml) (build from source) or [`docker-compose.ghcr.yml`](https://github.com/octoprox/octoprox/blob/main/docker-compose.ghcr.yml) (pre-built image).

### Multi-instance cluster (HA + horizontal scaling)

Multiple identical Octoprox processes behind an L4 load balancer (HAProxy
in the bundled compose; any TCP/HTTP-capable LB works). All instances
share one Postgres and one Redis.

```
   client ──▶ ┌────────────┐     ┌── Octoprox-1 ──┐
              │  HAProxy   │     │  :8000 :8080   │ ──┐
              │  :8000     │ ───▶├── Octoprox-2 ──┤   │
              │  :8080     │     │  :8000 :8080   │ ──┼──▶  Postgres
              │  :8404 UI  │     ├── Octoprox-3 ──┤   │     Redis
              └────────────┘     │  :8000 :8080   │ ──┘
                                 └────────────────┘
                                          │
                                          ▼
                                   upstream proxy pool
```

Compose files: [`docker-compose.cluster.yml`](https://github.com/octoprox/octoprox/blob/main/docker-compose.cluster.yml) (build from source) or [`docker-compose.cluster.ghcr.yml`](https://github.com/octoprox/octoprox/blob/main/docker-compose.cluster.ghcr.yml) (pre-built image). HAProxy config: [`haproxy/haproxy.cfg`](https://github.com/octoprox/octoprox/blob/main/haproxy/haproxy.cfg).

```bash
# Local-build cluster (Makefile targets, fast iteration)
make cluster-up
make cluster-logs
make cluster-down

# Production-ready cluster (pre-built GHCR image) — invoke docker compose directly
docker compose -f docker-compose.cluster.ghcr.yml up -d
docker compose -f docker-compose.cluster.ghcr.yml logs -f
docker compose -f docker-compose.cluster.ghcr.yml down
```

## What you gain by running N instances

- **High availability.** Any instance can die — clients keep being served by
  the others. Background workers (metrics flusher, autoscaler, etc.) fail
  over to a surviving instance within ~5 seconds.
- **Request throughput.** Tunnel termination, TLS/MITM relay, credential
  resolution, and routing decisions all run per-request on the receiving
  instance — N instances ≈ N× concurrent connections handled in parallel.
- **Aggregate bandwidth + file descriptors.** Each host contributes its own
  NIC and `ulimit`.
- **Sharded health-check capacity.** Each proxy is checked by exactly one
  instance at a time (rendezvous-hashed by `proxy_id` across the live
  membership). Adding instances divides the workload.
- **Zero-downtime deploys.** Rolling-restart one instance at a time.

## What does *not* scale by adding instances

- **In-memory cache.** Every instance still holds the full
  projects/credentials/connectors/proxies cache in RAM. 5 instances =
  5× the same memory footprint. (See the
  [DISTRIBUTION-OPTION-B.md](https://github.com/octoprox/octoprox/blob/main/DISTRIBUTION-OPTION-B.md)
  plan for the future tier-split that fixes this.)
- **Postgres write throughput.** Definitions and historical metrics still
  live in one DB; the metrics flusher is leader-elected so only one
  instance writes at a time.
- **Redis throughput.** All instances hit the same Redis for sticky
  sessions, rate-limit windows, quarantine state, metrics counters,
  heartbeats, and leases.
- **Cloud-provider API quotas.** Per-connector lease means only one
  instance calls AWS/GCP/Azure for a given connector at a time —
  intentional, so you don't get throttled by the cloud.

Rule of thumb: a cluster scales *concurrent request handling* and gives you
HA. To scale beyond what a single Redis or a single host's memory can take,
see Option B in the distribution plan.

## How it works under the hood

A few small mechanisms keep N instances in sync without a coordinator.

### Heartbeat (instance discovery)

Every instance writes a Redis key `instance_registry:<instance_id>` with a
10-second TTL, refreshed every 5 seconds. On graceful shutdown it deletes
its key; on hard kill, Redis expires it. The set of live keys is the
membership snapshot used by everything below.

### Cross-instance event bus

Mutations on one instance reach the others over Redis Pub/Sub on the
`octoprox:events` channel. Messages carry only
`(signal_name, instance_id, entity_id, op)` — receivers re-read the entity
from Postgres or Redis and update their cache. The instance that
published drops its own echo so no infinite loops.

Cross-instance signals: `project_changed`, `credential_changed`,
`connector_changed`, `proxy_changed`, `proxy_quarantine_changed`.

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
| Autoscaler        | Per-connector      | Two scalers would double-provision cloud VMs |
| Provider syncer   | Per-connector      | Two syncers would call provider APIs twice   |

Per-connector leases mean different connectors can be served by different
instances in parallel — only the same connector is single-writer.

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

| Port | Served by | What                                             |
|------|-----------|--------------------------------------------------|
| 8000 | HAProxy   | API + Web UI (HTTP, round-robin across replicas) |
| 8080 | HAProxy   | Proxy traffic (TCP, least-conn across replicas)  |
| 8404 | HAProxy   | HAProxy stats UI                                 |

Both 8000 and 8080 use the same HTTP `/health` probe (on port 8000) to
decide whether a backend is fit. A container with a crashed API server
gets pulled out of the proxy-traffic backend automatically.

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

# HAProxy backend status (CSV)
curl -s 'http://localhost:8404/;csv' | awk -F, '/^api_http|^proxy_tcp/{print $1"/"$2,$18}'
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
- Decide whether to expose Postgres (5433) and Redis (6379) on the host —
  for an internet-facing host you almost certainly want to remove those
  port mappings and keep them on the internal Docker network only.
- Mount the MITM CA from durable storage (or a Secret manager) so all
  instances trust the same root and CA rotations propagate cleanly.

## When to outgrow this

Option A (a fleet of identical instances sharing one Redis + Postgres)
scales request handling and gives you HA. It hits a ceiling when one of
the shared resources saturates — typically Redis throughput at a few tens
of thousands of proxied requests per second, or per-host memory when the
proxy pool grows past ~100k entries.

The next step is a tiered topology that splits the control plane (CRUD,
config) from the data plane (request termination) and replaces the
"every instance loads everything" cache with a snapshot pushed from the
control plane. The design is documented in
[DISTRIBUTION-OPTION-B.md](https://github.com/octoprox/octoprox/blob/main/DISTRIBUTION-OPTION-B.md)
in the repo — read that when you genuinely need it, not before.
