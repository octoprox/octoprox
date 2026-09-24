# User Action Tracking - Tracked Follow-ups

Landed in Sep 2026: `users.last_login_at` (stamped when a token is issued)
and request correlation (`X-Request-ID` on every request and response,
`request_id`, `user_id` and `username` bound into every log line, `instance`
on every line so replicas can be told apart, JSON log format for
production). Not done: carrying the originating request id through the
Redis pub/sub fan-out, so reload log lines on other replicas do not link
back to the request that caused them. The options below were assessed at the same time
and deferred. Decision at the time: do not store client IP addresses.
Revisit that decision explicitly before any of the items below records one.

## Last seen and login history

`last_login_at` only moves when a token is minted, so an active user on a
long-lived token looks idle. Add `last_seen_at`, updated from
`get_current_user` in `api/core/auth.py`, throttled through a per-user
Redis key so the database sees at most one write every few minutes per
user. Add a `login_events` table (user id, username as typed, success or
failure, reason, user agent, timestamp) written from the login and
set-password handlers. Failed attempts are currently only a log line.
Surface both on the Users page. Retention via the lease-guarded worker
pattern in `api/core/metrics_compactor.py`, configured next to
`system.metrics_retention_days`.

## Audit log of mutating actions

Generalise `provider_audit_log` (`api/db/models.py`, `_audit` in
`api/routes/providers.py`) into an `audit_log` table: actor id and
username, action, entity type and id, project id, request id, and a JSON
before/after diff. Populate it two ways:

- A middleware in the request-context layer records the envelope of every
  non-GET request under `/api/v1`: method, path, status, actor, request id.
  Bodies are not stored; if they ever are, credential secrets and passwords
  must be redacted first.
- Explicit calls in the routes where the diff matters: users, projects,
  connectors, credentials, providers, MITM, backup, geo.

No foreign keys, so history survives deleting the entity (same reasoning as
the provider audit). Include the table in backups the way
`provider_audit_log` is in `api/core/backup.py`. Retention as above. UI: an
admin Audit page plus a History tab on each entity inspector. Prerequisite
already in place: the request id in each row links the audit entry to the
log lines for that request.

## UI event tracking

Which pages and controls are actually used. Keep it first party: a small
events endpoint, a hook in `web/src/api/client.ts`, opt-in via
configuration, same retention worker as the audit log. Octoprox is
self-hosted, so no third-party analytics SDK. Value depends on someone
reviewing the data; build only when there is a concrete question to answer.

## Full tracing with OpenTelemetry

Auto-instrumentation covers FastAPI, SQLAlchemy, httpx, aiohttp and Redis,
exporting OTLP to Jaeger or Tempo with a collector added to the compose
files. The request id from `api/core/request_context.py` should become a
span attribute so logs and traces join. The hard part is the proxy data
plane: `api/core/proxy_server.py`, curl_cffi and rnet are not
auto-instrumented, so those spans are hand-written, and request volume
forces sampling. This answers "why was this slow" rather than "what did
this user do"; do it when performance debugging becomes the goal.
