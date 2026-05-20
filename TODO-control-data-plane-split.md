# Control Plane / Data Plane Split — Future Work

Today Octoprox runs as N identical processes behind an L4 load balancer:
each one boots the FastAPI management API, the raw TCP proxy server, and
all the background workers (HealthChecker, MetricsFlusher,
MetricsCompactor, AutoScaler, ProviderSyncer) in a single Python process.
They coordinate via Redis (leases, heartbeat-based HRW sharding, Pub/Sub
cache invalidation, batched metric deltas) and share a single Postgres for
definitions and historical metrics. See [`docs/deployment.md`](docs/deployment.md)
for the current model.

This document describes a successor topology where those concerns are
split into three deployables — control plane, data plane, and workers —
with durable messaging (NATS JetStream) between them. None of it is
implemented yet. A contributor should be able to read this top-to-bottom
and pick up the work without prior context.

## When this becomes worth doing

The current "every process runs everything" topology scales cleanly to
roughly 10 instances and ~100k proxies. The clear signals to move beyond
it:

- Admin queries (large listings, exports, bulk imports) are hurting
  proxy-tunnel latency because they share the same event loop and
  Postgres connection pool with the data plane.
- Releases of the management API force a TCP-server restart that drops
  in-flight tunnels — independent rollout of the proxy plane is needed.
- Background workers (HealthChecker, AutoScaler) need to scale
  independently of API or data-plane CPU.
- Cross-instance cache divergence shows up in observability traces — the
  Redis Pub/Sub safety net (60 s `full_reload`) is leaving stale state
  for too long, and you want NATS JetStream's at-least-once delivery
  instead.

If none of those bite, don't bother. The three-deployable operational
tax is real for a small team.

## Summary

Three deployables (control plane, data plane, workers), config pushed
xDS-style from CP to DP, durable messaging for control events.

## Target architecture

```
                         ┌─── Ingress (HTTPS) ───┐
                         │                        │
                   octoprox-control  (FastAPI, CRUD, dashboard, auth)
                         │   ▲
              writes     │   │ snapshot pull on bell
                         ▼   │
                      Postgres + Redis
                         ▲        ▲
              durable    │        │ pub/sub bell
              events     │        │
                         │        │
                       NATS JetStream
                         │
       ┌─────────────────┼────────────────┐
       │                 │                │
   octoprox-data     octoprox-data    octoprox-data   (TCP 8080, stateless)
       │                                  │
       └───── L4 LB (NLB / Envoy TCP) ◄───┘

       ┌─── octoprox-worker pods ───┐
       │  HealthChecker (sharded)   │
       │  MetricsFlusher (leader)   │
       │  MetricsCompactor (leader) │
       │  AutoScaler (per-connector)│
       │  ProviderSyncer (per-conn) │
       └────────────────────────────┘
```

## Concrete changes

1. **Split the binary by role** ([api/main.py](api/main.py)): one image,
   three entrypoints driven by `OCTOPROX_ROLE=control|data|worker`. The
   existing default `role=all` stays as the local-dev convenience.
   - `control`: FastAPI + all routes + write side of `ProxyManager`. No
     proxy listener, no workers.
   - `data`: `ProxyServer` + a new `RoutingTable` class (read-only mirror
     of `ProxyManager`'s state) + Redis subscriber for status changes.
     No FastAPI listener, no Postgres connection.
   - `worker`: HealthChecker + MetricsFlusher + MetricsCompactor +
     AutoScaler + ProviderSyncer. No FastAPI listener, no proxy server.
     Uses the same leases (per-connector for AS/syncer, global for
     flusher/compactor) and HRW sharding for HealthChecker as today —
     just in their own tier.

2. **xDS-style config push** (new `api/routes/internal_config.py` and
   `api/core/routing_table.py`):
   - Control plane maintains `octoprox:config:version` (monotonic
     counter, bumped on every mutation).
   - On every mutation, CP publishes `(version)` on NATS subject
     `octoprox.config.changed` — just the version number, not the
     payload, to keep messages tiny.
   - Data plane subscribes; on message it
     `GET /internal/v1/config?since=<my_version>` against CP to fetch
     the delta as a normalized JSON snapshot of
     projects/connectors/credentials/proxies.
   - Fallback: DP polls the same endpoint every 30 s in case of dropped
     messages, plus a full-snapshot fetch on boot before opening port
     8080.
   - **Why HTTP delta over a binary streaming protocol:** the data model
     is relational and you want a single transaction-consistent view per
     refresh. HTTP-from-Postgres gives you that out of the box; gRPC
     streaming is yak-shaving for the message volume here.

3. **Durable control-event bus on NATS JetStream**:
   - Subjects: `octoprox.proxy.added` / `.removed` / `.status_changed` /
     `.updated`, `octoprox.connector.changed`,
     `octoprox.health.result.<shard_id>`.
   - At-least-once delivery with consumer cursors per data-plane
     instance — fixes the existing Redis Pub/Sub fire-and-forget gap
     without inventing new safety nets.
   - Per-request signals (`request_completed`) stay local to the data
     plane and aggregate through the batched-delta path that exists
     today.

4. **Health-check ownership moves to the worker tier**:
   - Same rendezvous-hashing scheme as today, but workers are their own
     deployment so health-check load doesn't compete with proxy-serving
     CPU. Health results published to JetStream → MetricsWorker (for
     Postgres flush) + DP (for status cache invalidation).

5. **Data-plane state model**:
   - **Cold (definitions):** full snapshot of
     projects/connectors/credentials/proxies in RAM, refreshed via §2.
     Stale up to ~2 s on push, ~30 s on poll fallback — acceptable for
     definitions.
   - **Hot (status, quarantine):** Redis lookup with a 1 s local TTL
     cache, invalidated by NATS messages. Worst case: 1 s of routing to
     a just-failed proxy, recoverable via connection-error retry.
     Per-request routing cost: zero Redis RTT in steady state.

6. **MITM cert distribution**:
   [api/core/tls_cert_manager.py](api/core/tls_cert_manager.py) currently
   reads a CA from a local file (shared via the `ca_data` Docker volume
   in the cluster compose). Across N DP pods in production, mount the CA
   as a K8s Secret (or equivalent). Don't generate the CA per-pod.

## Coordination summary

| Concern | Mechanism |
|---|---|
| Config push CP→DP | NATS bell + HTTP delta |
| Durable control events | NATS JetStream consumer groups |
| Singleton workers | Redis leases (carried over from today) |
| Health-check sharding | HRW on `proxy_id`, in worker tier |
| Hot status propagation | NATS pub of status changes → 1 s local cache invalidate |

## Trade-offs

- **Pros:** independent scaling per tier — bump data plane on connection
  load, workers on proxy count, control plane stays small. Admin queries
  can never starve proxy traffic. NATS gives durable at-least-once
  delivery, so a brief disconnect doesn't desync caches. Deploys per-tier
  mean DP releases don't restart the API and vice versa.
- **Cons:** three deployables to operate, NATS to run (one extra
  cluster), three log streams to grep. Local dev keeps `role=all` mode.
  Eventual consistency window: 1–2 s for hot state, 2–30 s for
  definitions. Bulk operations (importing 100k proxies) need debouncing
  on the CP side to avoid notification storms.
- **Ceiling:** roughly 50 instances and a few hundred thousand proxies.
  Past that, single-Redis throughput and single-Postgres write rate for
  metrics start to bite.

## Implementation breakdown — ~3–4 weeks of focused work

Each phase is independently shippable: at every step the cluster still
runs, just with progressively more separated tiers.

### Phase 1 — Wire up the role flag (1 day)

- In [api/main.py](api/main.py)'s `lifespan`, branch on `OCTOPROX_ROLE`:
  - `control`: register routes, run write-side `ProxyManager`, no proxy
    listener, no background workers.
  - `data`: build a `RoutingTable` (Phase 2), start `ProxyServer`,
    subscribe to config bell. Skip migrations, skip seeding, skip
    routes, skip Postgres entirely.
  - `worker`: run `HealthChecker` + `MetricsFlusher` +
    `MetricsCompactor` + `AutoScaler` + `ProviderSyncer`. No proxy
    listener, no routes.
  - `all`: current behaviour, kept for local dev.
- Add per-role health endpoints (`/healthz`, `/readyz`) for k8s probes.

### Phase 2 — Extract `RoutingTable` (3–5 days)

- New `api/core/routing_table.py`: read-only view of `_projects`,
  `_proxies`, `_credentials`, `_connectors`, `_project_strategies`. Used
  exclusively by [api/core/proxy_server.py](api/core/proxy_server.py) on
  the request path.
- `ProxyManager` keeps the mutation methods (used by
  [api/routes/](api/routes/) handlers) and remains the cache for
  control-plane processes. Data-plane processes only build a
  `RoutingTable`, never a `ProxyManager`.
- Move the request-side helpers (`select_proxy_for_project`, credential
  placeholder resolution, domain filtering) onto `RoutingTable`.
  Strategies stay where they are — they're pure.
- Ensure DP has no Postgres connection: factor `_load_from_database` to
  also support `_load_from_snapshot(json)` (used by Phase 3).
- Tests: a DP process should be able to start without `DATABASE_URL`
  set, given a config snapshot.

### Phase 3 — Internal config endpoint and snapshot push (2–3 days)

- New `api/routes/internal_config.py`:
  `GET /internal/v1/config?since=<version>` returns a JSON snapshot of
  all entities (or just the delta if `since > 0`). Wire it behind an
  internal-only auth header (`X-Internal-Token`) so it isn't exposed
  publicly.
- CP increments `octoprox:config:version` (single Redis `INCR`) on every
  successful mutation in `ProxyManager.add_*/update_*/remove_*`. Bus
  publish on `octoprox.config.changed` carrying just `{version}`.
- DP boot: fetch full snapshot (`since=0`) into `RoutingTable` *before*
  opening port 8080.
- DP runtime: subscribe to bell, on each message
  `GET /internal/v1/config?since=<own_version>` and apply. Plus a 30 s
  polling fallback for missed bells.
- Tests: mutate on CP, assert DP picks it up via bell within ~1 s and
  via poll within ~30 s if the bell drops.

### Phase 4 — NATS JetStream substrate (3–5 days)

This is where new infra enters.

- Add NATS to `docker-compose.yml`, `docker-compose.ghcr.yml`,
  `docker-compose.cluster.yml`, and `docker-compose.cluster.ghcr.yml`.
  One node for dev, three for prod.
- Add `nats-py` to `pyproject.toml`.
- New transport `NATSJetStreamTransport` in
  [api/core/event_bus.py](api/core/event_bus.py). Same interface as the
  existing `RedisPubSubTransport`. Subjects per
  [api/core/signals.py](api/core/signals.py): `octoprox.proxy.added`,
  `.removed`, `.updated`, `.status_changed`,
  `octoprox.connector.changed`, `octoprox.config.changed`, etc.
- Migrate cross-instance signals from Redis Pub/Sub to NATS. Local-only
  signals stay local.
- Per-consumer cursors mean a reconnecting DP catches up on missed
  events instead of relying purely on the 30 s poll. This is the
  durability improvement over the current Redis Pub/Sub fire-and-forget
  setup.
- Tests: kill NATS, mutate on CP, restart NATS, assert DP eventually
  receives the missed events through the consumer cursor (within
  seconds, not the 30 s poll fallback).

### Phase 5 — Move workers to their own role (3–5 days)

- Today's background tasks are launched inside `ProxyManager.start()`.
  Refactor so they can be started independently of `ProxyManager`
  (worker role uses a minimal `WorkerHost` that wires Redis + Postgres
  + leases without the cache).
- HealthChecker, MetricsFlusher, MetricsCompactor, AutoScaler,
  ProviderSyncer now run *only* in `OCTOPROX_ROLE=worker` pods.
- CP no longer runs HealthChecker etc. — it just exposes CRUD.
- DP never ran them. It now also stops emitting `request_completed`
  through blinker; instead it writes directly to Redis counters via the
  existing batched-delta pipeline (which MetricsWorker reads in the
  next flush).
- Workers keep all the lease + HRW logic unchanged. Just the host
  process is different.

### Phase 6 — Deployment plumbing (2–3 days)

- `docker-compose.yml`: define three services `octoprox-control`,
  `octoprox-data`, `octoprox-worker` from the same image, different
  `OCTOPROX_ROLE`. NATS + Redis + Postgres as deps.
- Kubernetes manifests (or your equivalent): three Deployments. Data
  plane behind an NLB on TCP 8080. Control plane behind Ingress on 443.
  Workers as Deployment with `replicas: 2` (one active per lease).
- TLS CA distribution: mount the MITM CA from a K8s Secret on DP pods
  so [api/core/tls_cert_manager.py](api/core/tls_cert_manager.py)
  doesn't try to generate one per pod.
- Documentation: update [README.md](README.md) and
  [docs/deployment.md](docs/deployment.md) with the role architecture
  and the local "monolith mode" instructions for dev.

## Definition of done

- Three deployables run in production.
- DP pods restart without API downtime; CP pods restart without
  dropping proxy traffic.
- Worker pods can be killed mid-flush and the next leader resumes
  within ~5 s.
- NATS can be down for 60 s without losing any control event.
- Importing 100k proxies on CP propagates to DP within seconds.

## Reuse from the current implementation

These pieces are already in place and carry through unchanged:

- `OCTOPROX_INSTANCE_ID` and `OCTOPROX_ROLE` env vars in
  [api/core/config.py](api/core/config.py) (`role` flag is finally
  used).
- [api/core/event_bus.py](api/core/event_bus.py) `Transport` interface —
  NATS becomes a third implementation alongside `LocalTransport` and
  `RedisPubSubTransport`.
- [api/core/leadership.py](api/core/leadership.py) `Lease` primitive —
  used by the worker tier unchanged.
- Rendezvous-hashing sharding logic in
  [api/core/health_checker.py](api/core/health_checker.py).
- `version` columns on `projects`, `proxies`, `connectors`,
  `credentials` for optimistic-concurrency cache reloads.
- `proxy_manager.reload_<entity>(id)` methods — consumed via the
  snapshot endpoint rather than direct Postgres reads on DP.
- Redis-backed sticky sessions
  ([api/strategies/sticky.py](api/strategies/sticky.py)) and rate
  limiter ([api/core/rate_limiter.py](api/core/rate_limiter.py)).
- AutoScaler state externalised to Redis hashes.
- The batched metric-delta pipeline (in-memory pending dicts, periodic
  flush, Pub/Sub fan-out) — moves wholesale into the data plane.

## What to watch for in the current implementation

These are the signals that the existing topology has hit its ceiling
and it's time to pick this up:

- **Redis Pub/Sub message loss.** The 60 s safety reload covers it, but
  if you see cache divergence in observability traces, bring NATS in
  earlier.
- **Per-request Redis round-trips on the rate limiter.** The current
  implementation adds one Redis hop per proxied request that's subject
  to a rate limit. At >10k req/s this is noticeable. If it becomes the
  bottleneck, the tier split lets you co-locate Redis with the data
  plane specifically.
- **API/proxy contention.** If admin work measurably degrades proxy
  latency, that's the cleanest signal that the CP/DP split is justified.
