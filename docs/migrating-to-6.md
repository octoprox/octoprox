---
layout: docs
title: "Migrating to Octoprox 6.0"
nav_id: migrating-to-6
description: "Upgrade a 5.x install to Octoprox 6.0 with a short downtime: before you start, the switch on Docker Compose or Kubernetes, the checks, the way back to 5.0.11, and what changes."
---

# Migrating to 6.0

<p class="subtitle">6.0 splits Octoprox into roles that share Postgres and Redis and never talk to each other. A single instance stays one process; a cluster runs gateways, workers, the api and a controller pair, each scaled on its own. The upgrade from 5.x is a short downtime: stop 5.x, start 6.0. The two never serve at once, and the way back is 5.0.11 on the same database, once one SQL statement has set its schema version back.</p>

## Why the way back is short

- **Separate Redis keys.** Every key 6.0 writes starts with `o6:`, and 5.x's have no prefix: 6.0 never reads a key 5.x left, and 5.0.11 never reads one of 6.0's.
- **A migration that only adds.** Migration `048` adds the proxies' `status` and `status_changed_at` columns and the `metric_totals` and `metric_flushes` tables, and drops nothing: the `version` columns 5.x writes stay. 6.0 records it in `alembic_version`, as 5.x records its migrations, and 5.x does not start on a revision it does not know; since 048 only adds, setting the version back to 047 with one statement is all 5.0.11 needs (see [The way back](#the-way-back)). 048 runs online: each statement is a transaction of its own, waits at most half a second for a lock, and tries again after a pause when it has to wait (logged as `Migration statement waiting for a lock; retrying`), so 5.x processes still stopping at the switch can go on working on the database.

## Before you start

1. **Take a backup**, from **Settings → Backup & Migration** (tick *Include history* to keep the metrics, unless the install keeps a long observation log: 5.x builds the export whole in memory, every IP observation included), or a snapshot of the database, and keep it outside the install. The way back is one SQL statement and a redeploy (see [The way back](#the-way-back)), so the backup is the last resort.
2. **Choose an internal secret**: a long random string, different from `OCTOPROX_JWT_SECRET`. It goes on every 6.0 api, gateway and controller, the same value on each (on a single instance, its one service).
3. **Check Redis**, unless it is the `redis:7-alpine` the shipped compose files run: 6.0 needs Redis 7.2 or later (`redis-cli INFO server` shows it as `redis_version`), and opens every connection with `HELLO 3`, so a proxy between Octoprox and Redis must pass `HELLO` through.
4. **Fetch the 6.0 image ahead**, so its download is not part of the downtime: with Docker Compose, pull it while 5.x serves (below); on Kubernetes, pull it on the nodes the 6.0 pods will run on.
5. **For a cluster**, decide where each role runs and how many of each (see [Deployment & Scaling]({{ site.baseurl }}/deployment) or [Running on Kubernetes]({{ site.baseurl }}/kubernetes)). Gateways need `OCTOPROX_INTERNAL_API_URL`, the address they reach the api at; the api needs `OCTOPROX_PROXY_PUBLIC_URL` when clients reach the proxy at another host than the web UI.
6. **Releases before 5.0.6.** 6.0 brings their database through 047, the schema of 5.0.6 and later, to 048, and the way back sets it to 047, on which the release you came from cannot start. So the way back is 5.0.11, whatever release you came from.
7. **Plan the downtime.** The proxy is down from the stop of 5.x until 6.0 has migrated the database and published its first routing feed, and on Kubernetes until the balancer has the gateways: seconds, or minutes when 048 fills the totals from a long metrics history. On a Docker Compose cluster the proxy can take up to about 10 s more to answer after 6.0 is ready: the gateways refuse connections while they start, and nginx leaves out for 10 s a gateway that refused three (`max_fails=3 fail_timeout=10s`, as in 5.x). On a cluster with a small metrics history the switch took 25 to 28 s in all, from `down` to the first answered request.

## With Docker Compose

Your compose file is where your settings live: each password, secret, address and port mapping you changed from the shipped placeholders is written in it and nowhere else. Never download a 6.0 file over it. 6.0 would start on the placeholders: with the shipped database password it cannot sign in to Postgres, which keeps the password the database was created with, and restarts in a loop; with the shipped JWT secret, which is public, anyone who reaches the API can sign their own admin token. Instead, while 5.x still serves:

1. **Fetch the 6.0 files beside yours**, and list what you changed in yours by comparing them with the stock 5.x files, which are the same in every 5.x release. A single instance (`docker-compose.ghcr.yml`):

   ```bash
   curl -o docker-compose.ghcr.yml.new https://raw.githubusercontent.com/octoprox/octoprox/main/docker-compose.ghcr.yml
   curl -s https://raw.githubusercontent.com/octoprox/octoprox/v5.0.11/docker-compose.ghcr.yml | diff - docker-compose.ghcr.yml
   ```

   A cluster (`docker-compose.cluster.ghcr.yml`) takes the 6.0 nginx config too, since the role services have new names and the nginx upstreams changed with them:

   ```bash
   curl -o docker-compose.cluster.ghcr.yml.new https://raw.githubusercontent.com/octoprox/octoprox/main/docker-compose.cluster.ghcr.yml
   curl -o nginx/nginx.conf.new https://raw.githubusercontent.com/octoprox/octoprox/main/nginx/nginx.conf
   curl -s https://raw.githubusercontent.com/octoprox/octoprox/v5.0.11/docker-compose.cluster.ghcr.yml | diff - docker-compose.cluster.ghcr.yml
   curl -s https://raw.githubusercontent.com/octoprox/octoprox/v5.0.11/nginx/nginx.conf | diff - nginx/nginx.conf
   ```

2. **Carry every change into the `.new` files, and add the internal secret.** A single instance has them all in its one Octoprox service. The cluster file keeps shared settings in blocks at its top, named in brackets below: `x-octoprox-env` for every Octoprox service, `x-postgres-env` for the roles with Postgres, `x-api` and `x-octoprox`.
   - `OCTOPROX_INTERNAL_SECRET`: the secret you chose (`x-octoprox-env`).
   - Your database password, in `OCTOPROX_DB_PASSWORD` (`x-postgres-env`) and in the `postgres` service's `POSTGRES_PASSWORD`.
   - `OCTOPROX_JWT_SECRET`, `OCTOPROX_PUBLIC_URL` and your other api settings (`x-api`); a Redis password, in `OCTOPROX_REDIS_URL` (`x-octoprox-env`) and in the `redis` service; and any other setting you added, on the services of the roles that read it (see [Settings per Role]({{ site.baseurl }}/configuration#settings-per-role)).
   - The port mappings you removed, such as Postgres 5433 and Redis 6379, and an image pinned to a release: pin the 6.0 one (`x-octoprox`).
   - Your nginx changes, such as TLS or extra servers, into `nginx/nginx.conf.new`, keeping its upstreams (`api-1`, `api-2`, `gateway-1`, `gateway-2`).
   - Leave `OCTOPROX_ROLE` out if your 5.x file sets it: 5.x ignored it, and 6.0 runs only the role it names.

3. **Pull the 6.0 image while 5.x serves:**

   ```bash
   docker compose -f docker-compose.cluster.ghcr.yml.new pull
   ```

   For a single instance, the same with `docker-compose.ghcr.yml.new`. From here on `ghcr.io/octoprox/octoprox:latest` is 6.0 on this host, so a 5.x file you start again must pin `:5.0.11`.

### A single instance

One container owns the ports, so a single instance switches with one restart. Swap the files, keeping yours for the way back, and recreate the containers:

```bash
mv docker-compose.ghcr.yml docker-compose.ghcr.yml.5x
mv docker-compose.ghcr.yml.new docker-compose.ghcr.yml
docker compose -f docker-compose.ghcr.yml up -d
```

`up -d` puts the Octoprox container on 6.0, and Redis on 6.0's definition, without persistence: it starts empty, which 6.0 repairs on its own (see [Redis loss and recovery]({{ site.baseurl }}/architecture#redis-loss-and-recovery)). Postgres, the MITM CA and the IP databases keep their volumes. The `redis_data` volume 5.x kept Redis on is no longer used: once you stay on 6.0, remove it with `docker volume rm <project>_redis_data` (`docker volume ls` shows its name). In a checkout (`docker-compose.yml`), update the checkout, carry your edits of the compose file across the same way, then run `docker compose up -d --build`.

### A cluster

Stop 5.x with its own file: the replicas, nginx, Postgres and Redis stop, and the volumes stay. Then swap the compose file and the nginx config, keeping yours for the way back, and start 6.0:

```bash
docker compose -f docker-compose.cluster.ghcr.yml down
mv docker-compose.cluster.ghcr.yml docker-compose.cluster.ghcr.yml.5x
mv docker-compose.cluster.ghcr.yml.new docker-compose.cluster.ghcr.yml
mv nginx/nginx.conf nginx/nginx.conf.5x
mv nginx/nginx.conf.new nginx/nginx.conf
docker compose -f docker-compose.cluster.ghcr.yml up -d
```

6.0 runs on the same `postgres_data`: the first api, controller or worker to start migrates it while the others wait for the lock. Redis starts empty on 6.0's definition, without persistence, which 6.0 repairs on its own; `redis_data` is no longer used and can go once you stay on 6.0, as on a single instance. In a checkout (`docker-compose.cluster.yml`), stop 5.x with `make cluster-down` before you update the checkout, carry your edits across the same way, then start 6.0 with `make cluster-up`, which builds its image first.

## On Kubernetes

The 6.0 roles run as Deployments of their own, under new names (see [Running on Kubernetes]({{ site.baseurl }}/kubernetes)): the switch deletes the 5.x Deployment and creates them, rather than turning a 5.x pod into a role.

1. **Keep the MITM CA** your clients trust. The gateways sign with it and the api serves its certificate, both from a Secret (see [The MITM CA]({{ site.baseurl }}/kubernetes#the-mitm-ca)). If your 5.x pods mount it from a Secret, mount that one; if they keep it on a volume, copy it out of a 5.x pod before the switch, and create the Secret from the copy:

   ```bash
   kubectl -n octoprox cp <a 5.x pod>:/app/data/ca ./ca
   kubectl -n octoprox create secret generic octoprox-mitm-ca \
     --from-file=ca/octoprox-ca.crt --from-file=ca/octoprox-ca.key
   ```

2. **The switch.** One release deletes the 5.x Deployment and creates the api, gateway, controller and worker Deployments under their new names. In the same release the proxy Service selects the gateways (and a tunnel Service the tunnel gateways), and the web UI's Ingress or Service the api. The first api, controller or worker pod to start runs 048; give their startup probes room for it (see [Probes]({{ site.baseurl }}/kubernetes#probes)), and a `helm upgrade --wait` a `--timeout` that covers it. The proxy is down until the gateways are ready (once they have the first routing feed) and the balancer has them.
   - **Leave `--atomic` off.** Its automatic rollback would start 5.x on the migrated database, where 5.x does not start. If the release fails once 048 has run, run the [way back](#the-way-back)'s `UPDATE` first, then roll back.

**After the [checks](#checks), delete 5.x's keys.** 6.0 never reads them, but until they go, [the way back](#the-way-back) finds them: 5.0.11 resumes with its proxy statuses and flushes the counts it left unflushed. A Redis that keeps no data loses them in a restart, which 6.0 repairs on its own; otherwise, on a Redis that holds nothing but Octoprox:

```bash
redis-cli --scan --count 1000 | grep -v '^o6:' | tr '\n' '\0' | xargs -0 -r -n 500 redis-cli unlink
```

It deletes every key without the `o6:` prefix, including keys whose names hold a space or a quote, as sticky bindings' session IDs may. Until then the System page lists 5.x's keys under *Earlier releases* in its Redis breakdown.

## What migrates on its own

Migration `048` runs at the first start of a 6.0 api, controller or worker:

- **Proxy status.** A proxy 5.x was draining (it saved `draining_started_at` when the drain started) comes back as `draining` since then, so the drain finishes and the instance is terminated. Every other proxy starts `healthy`, and its first 6.0 health check, within one `health_check_interval` of the start, corrects it.
- **Running totals.** The `metric_totals` table is filled from the proxy, project and tunnel device history, so lifetime counters carry over exactly up to the migration.

## Checks

Once 6.0 serves:

1. Every 6.0 process answers `/ready` with `200`. A gateway turns ready once the controller has published the routing feed, a few seconds after the controller starts.
2. **Settings → System** shows a controller leading, every gateway's feed in sync, and the work streams draining: no stream whose queued entries stay above zero, and none with entries queued and no live worker taking its kind.
3. Within a minute or two every proxy's status reflects its first 6.0 health check. Until then a proxy has the status 048 gave it: `healthy`, or `draining` for a drain 5.x had started.
4. A request through the proxy is answered.
5. A cloud proxy you deleted through 5.x just before the switch may be back: 5.x marked it terminating in Redis only, so if 5.x stopped before it terminated the instance, 6.0 sees a healthy proxy. Delete it again.
6. A cloud instance 5.x was still creating when it stopped has neither a row nor the `octoprox-connector` tag 6.0 finds instances by, so 6.0 never sees it, and it runs until you terminate it by hand: look for an instance whose name starts with the connector's `instance_name` and that no proxy matches.
7. On the Connect page the proxy URL names the proxy's host; if it names the web UI's, set `OCTOPROX_PROXY_PUBLIC_URL` on the api (the api logs `The Connect page names the web UI's host as the proxy's` at start when it is unset in a cluster).
8. A few lines in the first start's logs are expected: `Database migrations complete` with `revision` 048 from every api, controller and worker, with `previous` 047 from the one that migrated (an older revision from a release before 5.0.6) and 048 from the rest, and `Waiting for the migration lock` from any that started meanwhile; `Routing feed head absent from Redis (a first start, or Redis lost its data); re-attributing every proxy`, once, from the leading controller, since Redis holds no 6.0 routing feed yet; and, at info, `Routing feed head missing; keeping the feed applied so far` from a gateway that starts before the first publish. Nothing was lost: the re-attribution re-judges every proxy once against the IP databases.
9. On Kubernetes, then delete 5.x's keys (see [On Kubernetes](#on-kubernetes)).

What 5.x kept in Redis starts afresh, since 6.0 does not read it:

- **Rate windows and quarantines:** a proxy quarantined under 5.x is routed until it trips again.
- **Sticky bindings:** a sticky session may change exit once.
- **Preflight verdicts:** 6.0 echoes each preflight session once more.
- **Failure examples and MITM captures:** the Failures page's examples and the captures start empty.
- **Traffic blocks:** a connector blocked under 5.x carries traffic again until the first metering window that carries its bytes asks for a decision, which blocks it again seconds later.
- **Scale-in:** nothing is scaled in for the first five minutes, while demand is measured again.
- **Sign-in:** a sign-in through an identity provider in progress at the switch fails; sign in again.
- **What 5.x had not written** is lost: the counts of its last minute, and the IP observations it had queued.

## The way back

Stop 6.0, set the schema version back to 047, then start 5.0.11 on the same database. 6.0 recorded 048 in `alembic_version`, and 5.0.11, which brings the schema to its head at every start, stops at its start while the version says 048 (`Can't locate revision identified by '048'`). Setting it back is safe, since 048 only adds:

```sql
UPDATE alembic_version SET version_num = '047' WHERE version_num = '048';
```

It changes only a version of 048, so it never labels an older schema 047: a 6.0 start that stopped before 048 left the version where it found it, and 5.0.11 brings that schema to 047 itself. No 6.0 api, controller or worker may start after the statement: one that starts runs 048 again.

- **Docker Compose:** stop 6.0 with its file, put the 5.x files back, pin their image to `ghcr.io/octoprox/octoprox:5.0.11` (the cluster file names it in `x-octoprox-base`, a single instance in its `octoprox` service), start Postgres alone, set the version, and start the rest. For a cluster:

  ```bash
  docker compose -f docker-compose.cluster.ghcr.yml down
  mv docker-compose.cluster.ghcr.yml docker-compose.cluster.ghcr.yml.new
  mv docker-compose.cluster.ghcr.yml.5x docker-compose.cluster.ghcr.yml
  mv nginx/nginx.conf nginx/nginx.conf.new
  mv nginx/nginx.conf.5x nginx/nginx.conf
  # Pin the image in docker-compose.cluster.ghcr.yml: ghcr.io/octoprox/octoprox:5.0.11
  docker compose -f docker-compose.cluster.ghcr.yml up -d --wait postgres
  docker compose -f docker-compose.cluster.ghcr.yml exec postgres psql -U octoprox octoprox -c "UPDATE alembic_version SET version_num = '047' WHERE version_num = '048';"
  docker compose -f docker-compose.cluster.ghcr.yml up -d
  ```

  Use your database's user and name in `psql`. A single instance does the same with `docker-compose.ghcr.yml`, without the nginx config. The 6.0 files are back as `.new`, ready for another try. In a checkout, stop 6.0 (`make cluster-down`, or `docker compose down` for a single instance), check out `v5.0.11` with your edits, start its Postgres and set the version as above (with `docker-compose.cluster.yml`, or `docker-compose.yml` for a single instance), and start it (`make cluster-up`, or `docker compose up -d --build`).
- **Kubernetes:** set the version with `psql` against the database, then redeploy your 5.x release with the image `ghcr.io/octoprox/octoprox:5.0.11`, by a `helm rollback` to the last revision before 6.0 when that one ran it. The rollback replaces the 6.0 pods; should 5.x's pods still stop at their start, a 6.0 pod started in between and ran 048 again: set the version again, and they start at their next restart.

5.x runs on the database as 6.0 left it, its version set back to 047: what 048 added is invisible to it. What 6.0 had not flushed is lost: the counts since its last metrics flush, up to a minute at the default `OCTOPROX_METRICS_FLUSH_INTERVAL`. 6.0's keys, in a Redis that keeps them, harm nothing; delete them if you will not try again soon:

```bash
redis-cli --scan --count 1000 --pattern 'o6:*' | tr '\n' '\0' | xargs -0 -r -n 500 redis-cli unlink
```

**Upgrading again.** First reset what the first attempt left, while 5.x runs and no 6.0 process does. The next 6.0 start runs 048 again, since the version says 047, but 048 keeps the totals and proxy statuses it finds: without the reset, `metric_totals` misses everything 5.x flushed in between, and a drain 5.x started in between is not carried over:

```sql
SET lock_timeout = '2s';
DROP TABLE IF EXISTS metric_flushes, metric_totals;
ALTER TABLE proxies DROP COLUMN IF EXISTS status, DROP COLUMN IF EXISTS status_changed_at;
```

Run it again if a statement gives up waiting for its lock. On Docker Compose, `docker compose -f <your file> exec postgres psql -U octoprox octoprox` opens a session, with your database's user and name.

**The backup is the last resort**, when the data itself went wrong. Find the release in the backup file (its envelope is plain JSON around the encrypted part, so `head -n 6 octoprox-backup-<date>.opbak` shows its `app_version` and `schema_version` without the passphrase), start that release on an empty database with the image pinned to it, sign in with the admin it seeds and import the backup from **Settings → Backup & Migration**, with *Keep my current account* unticked to get every user back as it was. The import refuses a backup whose `schema_version` differs from the install's, so it takes that release or another at the same schema, not 6.0. A database snapshot goes back the way your Postgres restores one, with 5.0.11 started on it.

## What changes

- **Roles instead of identical instances.** Every process runs the same image; `OCTOPROX_ROLE` picks what it runs: `api`, `gateway`, `controller`, `worker`, `echo`, or `all` (the default, everything in one process). See [Architecture]({{ site.baseurl }}/architecture).
- **Gateways have no database.** They route on a routing feed the controller compiles from Postgres and publishes to Redis, encrypted with a new shared secret, `OCTOPROX_INTERNAL_SECRET`.
- **Proxy status is a column.** Health, draining and terminating live on the proxy row in Postgres; 5.x kept them in Redis.
- **Background work is jobs on streams.** Health checks, syncs, cloud scaling, traffic decisions, metrics flushes, re-attribution and the rest are messages on a Redis stream per job kind: the controller publishes the periodic ones, any role asks for a run now, and workers take each message once through a consumer group. Nothing is sharded over a membership any more, and no process calls another.
- **Counters are kept as running totals.** Each proxy's, project's and tunnel device's all-time counters are a row in Postgres, updated with every metrics flush.

A single instance needs nothing new to run: the compose files keep their service and run role `all`. Set `OCTOPROX_INTERNAL_SECRET` anyway, since the feed in Redis is encrypted with it.

## Behaviour changes

**Routing**

- `least_used` picks the proxy with the fewest transfers open on the gateway serving the request, and the connector with the fewest open transfers per unit of weight; ties are broken at random. It no longer reads cumulative request counts. See [Routing Strategies]({{ site.baseurl }}/routing-strategies).
- `health_based` scores proxies on each gateway's own outcomes, and keeps every proxy with fewer than ten outcomes on that gateway in the draw, so a new or restarted gateway tries every proxy before it prefers some.
- The first request for a new country of an *All countries* pool waits up to 5 seconds while a worker creates the country's sessions and the gateways receive them; 5.x created them inline.
- A connector whose custom or operator-installed provider was deleted or disabled is no longer routed; 5.x went on routing its stored proxies.
- A project whose stored routing strategy no router has is routed with `OCTOPROX_DEFAULT_STRATEGY` (`round_robin` unless set) and logged; saving an unknown strategy is refused with `400`.

**Health, attribution and rotation**

- A health check's verdict reaches every gateway within moments of the check.
- A pooled session slot that preflight refuses leaves the gateway that saw it at once, and every other gateway's pool within seconds: a worker removes it, and the provider sync provisions a replacement.
- Re-attribution is queued. `POST /api/v1/geo/reattribute` answers `202` with the job's key, and a worker re-judges. Saving the attribution settings re-judges only when the default conflict rule or city radius changed, and an IP database change only when it changes what is consulted (an enabled database added or removed, enabled or disabled, or its priority moved).
- **Detect** runs on the api process, so the api needs network access to the proxies.
- A syncing connector is refreshed and reconciled when it is created or edited, when one of its proxies is deleted, and every `ip_refresh_interval`; session pools are reconciled periodically too.
- Cloud connectors: rotation times are drawn afresh at the first 6.0 run, and nothing is scaled in for the first five minutes after the switch (or after Redis is lost) while demand is measured again. Every instance 6.0 creates carries the tag `octoprox-connector`, and a tagged instance that has no row is terminated within the hour; proxy credentials stay in the start-up script alone, as in 5.x (see [AWS]({{ site.baseurl }}/aws-setup), [GCP]({{ site.baseurl }}/gcp-setup) and [Azure]({{ site.baseurl }}/azure-setup)).
- A failing proxy is logged once, when it turns unhealthy, rather than at every check.

**Limits and quarantine**

- Traffic-limit blocks are decided by a worker's traffic job, asked for by each gateway metering window that carried a limited connector's bytes, and decided again every minute while a connector is blocked. A connector can overshoot its limit by a few seconds of its traffic: a 5-second metering window, then the decision and the feed's publish. After a usage reset or a limit edit the connector shows *Pending* until a worker has decided. See [Traffic Limits]({{ site.baseurl }}/traffic-limits#multi-gateway-behaviour).
- A rate-limit quarantine excludes the proxy at once on the gateway whose request tripped it, and on every other gateway within a fraction of a second.

**API**

- `POST /api/v1/projects/{project_id}/proxies/strategy` is gone (`405`); set the project's `routing_strategy`.
- Clearing a project's MITM captures (`DELETE /api/v1/projects/{project_id}/mitm/requests`) and downloading a tunnel device's config or profile need an editor; viewers get `403`.
- `/health` answers `{"status", "role", "instance", "version"}`, plus `reasons` when it answers `503`, which it now does while the process is not ready; `proxy_count` and `healthy_proxy_count` are gone. `/ready` checks the process instead of always answering `200`.
- `GET /api/v1/projects/{project_id}/metrics/prometheus` is Prometheus text (`text/plain; version=0.0.4`) rather than a JSON-encoded string.
- `GET /api/v1/geo/databases` no longer returns a scheduled download's credentials (`update_auth`); `has_update_auth` says whether it has any.
- `GET /api/v1/system/stats` describes the cluster: processes grouped by role, the controller, the routing feed and each gateway's copy of it, and the work streams. `runtime`, `cache` and `workers` are gone. See [the API reference]({{ site.baseurl }}/api#system-statistics).
- A connector's `traffic_usage` gains `pending`, true from a usage reset or a limit edit until a worker has decided, and its `blocked` is the block the gateways enforce.
- The WireGuard and OpenVPN settings report every gateway carrying the tunnel instead of the process that answered; the IP database list and `/geo/status` report every gateway.
- A duplicate project name and an unknown strategy are `400` (were `500`); a by-id route answers `404` for another project's id; a refused edit leaves the stored row unchanged; a proxy upload adds its readable lines in one transaction, all of them or none.
- Proxy and project counters are the all-time totals plus what the gateways counted since the last flush, and read the same on every api process.
- A JSON body must come with `Content-Type: application/json`. A body sent without a `Content-Type` is refused with `422`; 5.x read it as JSON. The web UI and the `curl` examples in these docs send the header; with Python's requests, pass the body as `json=` rather than `data=json.dumps(...)`.
- Responses from the api, and from a single instance, carry `Vary: Origin`.
- A number in a response that is not finite (NaN or infinity) is returned as `null`; 5.x answered `500`.

**TLS interception**

- The curl_cffi engine impersonates Chrome 150 and Firefox 147 (5.x: Chrome 142 and Firefox 144), so sites see those browsers' fingerprints; Safari and Edge are unchanged.
- The curl_cffi engine verifies sites and `https://` proxies against certifi's CA bundle plus the OS certificate directory, as 5.x did, but `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` no longer replace certifi's bundle. A private CA added to the OS store is still trusted.

**Tunnels**

- A WireGuard subnet change is applied live on the gateways; 5.x needed a restart.
- Device changes (disable, rotate, remove) reach the gateways through the routing feed. With the controller or Redis down, a gateway keeps the devices it last applied until they return.
- Gateways compare their tunnel peers and sessions with the device list every 30 seconds and remove strays.

**Elsewhere**

- Every log line carries `role` next to `instance`, and the Redis URL is logged with its password masked. Credentials are never logged, at any level.
- Postgres errors in the logs no longer carry the `<class 'asyncpg.exceptions...'>` prefix or the `DETAIL` line; change any log search or alert keyed on them.
- A SOCKS5 upstream that cuts its refusal short, closing the connection after the reply code and the reserved byte, is counted as `upstream_rejected` on the Failures page, as a complete refusal already was; 5.x counted it as `upstream_error`.
- A process that cannot bind its port, or whose app module cannot be found, exits with code 3 like any other failed startup (5.x exited with 1 for those two). An invalid setting, or a failed import inside the app, still exits with 1.
- Octoprox needs Redis 7.2 or later. Connections speak RESP3 (`HELLO 3`; on Redis 5 every command fails) and introduce the client library with `CLIENT SETINFO`, which Redis before 7.2 refuses on every connection; the rate-limit quarantine and the work streams use commands Redis 6.2 added (`ZADD GT`, `ZRANGE BYSCORE`, `XAUTOCLAIM`). The shipped compose files and the tests run `redis:7-alpine`.
- The web UI's file serving no longer follows a path out of the build directory.

## Settings and environment variables

**New**

| Variable | YAML | Default | Read by | Meaning |
|----------|------|---------|---------|---------|
| `OCTOPROX_ROLE` | | `all` | every process | What the process runs: `all`, `api`, `gateway`, `controller`, `worker` or `echo` |
| `OCTOPROX_INTERNAL_SECRET` | | placeholder | api, gateway, controller | Encrypts and authenticates the routing feed, and signs gateway calls to the api. The same value on each |
| `OCTOPROX_INTERNAL_API_URL` | | `http://localhost:8000` | gateway | Where gateways reach the api, such as `http://octoprox-api:8000` |
| `OCTOPROX_WORKER_JOBS` | | every kind | worker | Comma-separated job kinds this worker takes: `health`, `sync`, `scale`, `provision`, `reattribute`, `enrich`, `traffic`, `metrics_flush`, `compaction`, `snapshot`, `geo_update`, `tunnel_last_seen`, `index_hygiene` |
| `OCTOPROX_FEED_COMPILE_INTERVAL` | `feed.compile_interval_seconds` | `1` | controller | The least time between two compiles of the routing feed: a change after a quiet spell is compiled at once, and changes that arrive sooner are compiled together |
| `OCTOPROX_RECONCILE_INTERVAL` | `feed.reconcile_interval_seconds` | `3600` | api, gateway, controller, worker | How often every process reads in full what it follows, whatever the change stream announced: the net for a write that announced nothing |
| `OCTOPROX_HEALTH_CHECK_CONCURRENCY` | `proxy.health_check.concurrency` | `50` | worker | Health checks one worker runs at once |
| `OCTOPROX_REDIS_REQUEST_TIMEOUT` | `redis.request_timeout_seconds` | `2` | every role but echo | Seconds one Redis connect may take, and one call on a request's path or right after a save, before it is given up (the request path then fails open); other replies may take up to 30 seconds |

**Changed**

- `OCTOPROX_INSTANCE_ID` defaults to the host name and a random suffix per boot (a pod or container name you recognise) instead of a random UUID.
- `OCTOPROX_DB_APPLICATION_NAME` defaults to `octoprox-<role>`, so each role's sessions can be told apart in `pg_stat_activity`. The shipped config files no longer set it.
- `OCTOPROX_ROLE` had no effect in 5.x: every process ran everything, whatever it said. 6.0 refuses its old placeholder values `control` and `data`, and `worker` now runs the job runners alone, with no API and no proxy listener. Remove it from a 5.x setup before the switch, or set the role you mean.
- `metrics.hosts.max_hosts` and `failure_log.max_per_window` are per gateway rather than per instance.
- `proxy.health_check.interval_seconds`, `proxy.ip_refresh_interval`, `database.metrics_flush_interval` and `geo.updater.check_interval_seconds` are the periods at which the controller publishes the health, sync, metrics flush and IP database update jobs; set them on the controller. `system.metrics_interval` is the snapshot job's period: set it alike on the controller, the workers and the api, which charts the snapshots.

**Removed: `proxy.connection.max_retries`** (`OCTOPROX_MAX_RETRIES`), which nothing read. A config file that still sets it, or the long-unread `proxy.health_check.enabled`, `unhealthy_threshold`, `healthy_threshold` and `proxy.connection.retry_delay_seconds`, starts as before: unknown keys are ignored.

**Removed: `geo.observations.flush_interval_seconds`** (`OCTOPROX_GEO_OBSERVATION_FLUSH_INTERVAL`): workers ingest IP observations as they arrive. A config file or an environment that still sets it starts as before: unknown settings are ignored.

**Removed: `OCTOPROX_PROXY_PUBLIC_HOST` and `OCTOPROX_PROXY_PUBLIC_PORT`** (`server.proxy_public_host`, `server.proxy_public_port`), superseded by `OCTOPROX_PROXY_PUBLIC_URL`; 5.x read them while that was unset, 6.0 ignores them. Set `OCTOPROX_PROXY_PUBLIC_URL=http://<host>:<port>` on the api instead.

**Removed: the standalone echo service.** The `octoprox-echo` command and its `OCTOPROX_ECHO_DATABASES`, `OCTOPROX_ECHO_TRUSTED_PROXIES`, `OCTOPROX_ECHO_HOST`, `OCTOPROX_ECHO_PORT` and `OCTOPROX_ECHO_CACHE_DIR` are gone, as is port 8090. Run the image with `OCTOPROX_ROLE=echo` instead: it serves `/echo` on port 8000 and reads the same settings the api's `/echo` reads:

```bash
docker run -d -p 8000:8000 \
  -e OCTOPROX_ROLE=echo \
  -e OCTOPROX_GEO_DATABASES='[{"path": "/data/GeoLite2-City.mmdb"}]' \
  -e OCTOPROX_GEO_ECHO_TRUSTED_PROXIES='["10.0.0.0/8"]' \
  -v /srv/geoip:/data:ro \
  ghcr.io/octoprox/octoprox:latest
```

It needs no Postgres, Redis or secret, and it answers `/live`, `/ready`, `/health` and `/metrics` like every role.
