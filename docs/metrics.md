---
layout: docs
title: "Metrics and Request Counting"
nav_id: metrics
description: "How Octoprox counts requests, success rates, latency and bytes per proxy and project, and why CONNECT tunnels are counted rather than HTTP requests."
---

# Metrics & Request Counting

<p class="subtitle">How Octoprox tracks request metrics, and the important distinction between connection counting and request counting.</p>

Octoprox tracks per-proxy and per-project metrics including request counts, success/failure rates, average latency, and bytes transferred. These metrics are visible in the web UI and available via the API.

## How Counting Works

Octoprox counts at the **CONNECT tunnel level**, not at the individual HTTP request level. When a client opens an HTTPS tunnel via `CONNECT`, that tunnel is counted as a single request in the metrics - regardless of how many HTTP requests flow through it.

This distinction matters because most HTTP clients use **connection pooling** (keep-alive). A single CONNECT tunnel can carry dozens or hundreds of sequential HTTP requests without closing the connection.

### Example

A client creates one `BlockingClient` and makes 42 requests to `https://httpbin.org/ip` through Octoprox. Because the client reuses the same CONNECT tunnel for all 42 requests via keep-alive, Octoprox records:

- **`request_count`: 1** (one CONNECT tunnel was opened)
- **Not 42** (the actual number of HTTP requests)

## What Is Accurate

While `request_count` reflects the number of CONNECT tunnels rather than individual HTTP requests, **bytes transferred (`bytes_sent` and `bytes_received`) are accurately reported**. The proxy measures all bytes that flow through the tunnel for its entire lifetime, so the total bandwidth consumed by all HTTP requests within a keep-alive connection is correctly captured.

In the example above, even though `request_count` shows 1, the `bytes_sent` and `bytes_received` values reflect the full payload of all 42 HTTP requests and responses.

Bytes are counted while the tunnel is open, not only when it closes: every 1 MiB or 5 seconds a running transfer reports its progress, so a long-lived tunnel shows up in the charts as it goes and counts against a connector's [traffic limit]({{ site.baseurl }}/traffic-limits) in time. The request itself is counted once, when the tunnel ends.

## Five Levels of History

Metrics are kept per **proxy**, per **project**, per **connector**, per **tunnel device** and per **destination host**, in five history tables with the same shape and the same compaction tiers (raw for a day, hourly for a week, 6-hourly for a month, daily until the project's retention limit). Proxy history goes with its proxy when it is removed, which cloud rotation and provider re-syncs do routinely. Project and connector history survive that, so the traffic split and traffic limits sum connector history, and `GET /projects/{id}/connectors/{id}/metrics/history` charts a connector across rotations.

A connection from a [WireGuard device]({{ site.baseurl }}/wireguard) is counted against the device as well, in the same pipeline, so a device has totals and a history of its own (`GET /projects/{id}/wireguard/peers/{id}/metrics/history`) that are install-wide and survive restarts. Device rows carry two extra counters the transparent listener reports per device: connections relayed by address because no destination name could be recovered, and encrypted-DNS connections closed. The table is shared by every tunnel protocol; a device's rows go when the device is removed, and the project's retention applies.

In the web UI one time range (1h to 90d) covers every view of this history: the Overview charts, the traffic split, the Hosts page, the connector and device charts, provider accuracy and the system trends. Pick it on any page and the others follow; the choice is remembered in the browser. A view that cannot show the exact window (claim statistics are daily, project history stops at 30 days) shows the nearest one it has.

## Where the Numbers Come From

Requests are counted by the [gateway]({{ site.baseurl }}/architecture) that carries them, in memory, and nothing on the request path waits for the counting:

1. Every 5 seconds each gateway adds what it counted to counters in Redis, one set per proxy, project, connector, tunnel device and host, in one round trip. A write Redis did not confirm is kept and sent again, and Redis applies it once.
2. Once a minute (`database.metrics_flush_interval`) the controller publishes a metrics flush job, and a worker runs it: it moves the counters out of Redis into a staging hash named after the job, writes them as history rows and adds them to each proxy's, project's and device's running totals in one transaction that also records the job, then deletes the staging hash. A flush that fails is run again and commits once; one given up before its commit puts the counters back for the next flush, so a Postgres outage loses no counts.
3. The totals the UI and API show (a proxy's or a project's requests, success rate, latency, bytes) are those running totals plus the counters in Redis not flushed yet, so they are within about 5 seconds of live traffic and read the same on every api process. For the few seconds a flush holds the counters between Redis and Postgres they read low by those counts, never double. History charts trail by up to one flush interval.

Proxy history goes with its proxy; the running totals of a removed proxy, project or device are dropped by the next hourly compaction job.

## Latency: Where the Time Goes

A request's latency runs from Octoprox taking it up until its tunnel through the upstream proxy is up (for plain HTTP, until the response's status line arrives). It is split into the steps the request went through, so time spent in Octoprox can be told from time spent at the vendor:

| Step | What it measures | Whose time |
|------|------------------|------------|
| Routing (`route`) | Choosing the proxy: in memory, apart from a sticky session's binding in Redis and a `-cc-` request waiting for an on-demand country group | Octoprox |
| Preflight (`preflight`) | [Exit verification]({{ site.baseurl }}/location-routing#preflight), only for the requests it applies to: a cached verdict is one Redis read, anything else an echo through the proxy, and through other proxies when it is retried | The vendor's, spent on Octoprox's check |
| Connect (`connect`) | Opening the connection to the vendor's gateway | The network to the vendor |
| Handshake (`handshake`) | From the tunnel request until the vendor answers it: the vendor picks an exit and reaches the target | The vendor |
| Response (`response`) | Plain HTTP only: from sending the request upstream until its status line | The vendor and the target site |

The steps add up to the latency. A SOCKS upstream opens its connection and does its handshake in one call, so all of its time is under Handshake. A [dynamic-sessions]({{ site.baseurl }}/providers#descriptor-reference) gateway used without `-sessid-` gets a new vendor session for every request, so there is no verdict to reuse: such a request is echoed whenever the project's rules could refuse it for its location, and otherwise only a sample of them is.

Latency is averaged over the **successful** requests only. A failed request's time is how long it took to fail, a timeout's the whole timeout, so averaging failures in let a few timeouts move the line; the Failures page shows the time to fail instead.

- The **Overview**'s Latency chart stacks the steps over time, and its legend gives each step's average over the range. Each connector's panel on the **Connectors** page has the same chart for that connector, to compare vendors.
- Project and connector history (`GET /projects/{id}/metrics/history`, `GET /projects/{id}/connectors/{id}/metrics/history`) carry each step's average per point in `step_latency_ms`.
- Each gateway's `/metrics` has histograms of the latency and of each step, by connector and outcome, for percentiles (see [Prometheus](#prometheus)).
- A failure example on the **Failures** page names the step it failed in and how long each step took.

History recorded before steps were kept has no split: the charts show it as one **Not split** band, and its averages were taken over every request, failures included. The all-time averages (a project's or a proxy's) keep the value they had at the upgrade and move toward the successes-only average as new requests come in.

## Hosts: Where the Traffic Goes

Every request is also counted against the **destination host** it was for, under the connector that carried it: the `CONNECT` target for an HTTPS tunnel, the URL host for a plain HTTP request, and the recovered name (or the address, when no name could be recovered) for a connection from a tunnel device. Host names are folded to lower case without a trailing dot. The project's **Hosts** page in the web UI lists the hosts busiest first for a time range, with requests, success and failure counts, bytes each way, average latency and share, splits each host by connector, and charts the busiest hosts over time against the remainder. The API serves the same through [`GET /projects/{id}/metrics/hosts`]({{ site.baseurl }}/api#hosts) and `/metrics/hosts/history`.

This is the fifth history table (`host_metrics`), with the same flush, compaction tiers and retention as the other four, so the numbers lag live traffic by up to one flush interval (60 seconds by default). Rows go with their connector, as connector history does. Every host is kept by default: a host requested once costs one row, and a host requested constantly costs one row per minute, compacted to one per hour after a day. It is the one metrics table whose size depends on what clients request rather than on the pool, so an install that crawls the open web has two dials (see [Configuration]({{ site.baseurl }}/configuration#host-metrics)): `metrics.hosts.enabled` switches per-host counting off entirely, and `metrics.hosts.max_hosts` sets a ceiling on how many hosts each gateway names per project in each 5-second metrics window, keeping the busiest ones; the rest are folded into a single `(other)` row, so their requests and bytes stay in the totals, just not by name.

The same counting rules apply here as above: a keep-alive tunnel is one request against its host however many HTTP requests it carried, while its bytes are exact.

## Failures: Why Requests Failed

The **Failures** page (under Overview in the web UI) says what the failure counts are made of. It has two parts, kept apart on purpose:

- **What failed** counts every failure in the selected time range (1h to 30d, the same range as the other pages), by kind, by the vendor's status when it refused, and by connector, with a chart of the failures over time by kind. The counts come from the metrics history, so they trail live traffic by up to one flush interval (a minute by default) and are complete however many failures there are.
- **Examples** list each distinct failure once, the ones seen last first: what went wrong, what the client was answered, the vendor's own answer, the target, the session and location asked for, the tunnel device it came from, the connector and proxy that carried it, and the [step](#latency-where-the-time-goes) it failed in with how long each step took, with how many times it happened between the first and the last time. Click a kind in the summary to see only its examples, and a row to open the example in full with a note on what that kind usually means. The table does not refresh while you read it: when examples are new or have happened again, a button says how many and loads them when clicked, and an opened example stays open.

| Kind | What happened | Client answered |
|------|---------------|-----------------|
| Upstream timeout | The upstream proxy did not answer within `proxy.connection.timeout_seconds` | 504 |
| Proxy unreachable | The connection to the upstream proxy was refused, or its host could not be reached | 502 |
| Refused by the vendor | The upstream proxy answered the CONNECT (or SOCKS handshake) with an error, the request's own or its exit verification echo's. For an HTTP upstream the vendor's status is kept (407 means it rejected the connector's credentials), and so is what it said about it: its error headers, such as `x-brd-error`, and the text of its error page | 502, with the vendor's answer in the reason phrase and a `Proxy-Status` header |
| Upstream error | The upstream leg failed another way, for example a reset mid-handshake | 502 |
| Client went away | The client closed the connection, or stopped sending, before sending the whole request body | 502 (504 when it stopped) |
| Interrupted | Octoprox stopped while the request was being set up | - |
| Wrong exit location | Exit verification found the proxy exiting somewhere other than the requested location, or got no answer that placed it (`Exit location not checked`), and the request was refused | 502 |
| No proxy available | No healthy proxy matched the host's domain filters and the requested location | 502 |
| All proxies quarantined | Every proxy that could serve the request is resting after its rate limit | 429 |
| Traffic limit reached | The connectors that could serve the request are over their period's traffic | 509 (or as configured) |
| Invalid location | The username named a state or city without a country | 400 |
| Intercepted request failed | With [TLS interception]({{ site.baseurl }}/tls-interception), one request inside the connection could not be relayed upstream. When the upstream proxy refused the engine's tunnel, the vendor's status is kept as for a refusal | 502 |

The first seven are exactly the requests Overview counts as failures, so the summary's upstream failures match Overview's failure count for the same window, apart from traffic limits that cut a request already under way: Overview counts those too, and the summary lists them with the refusals. The next four are requests Octoprox refused before using any proxy, which the request metrics do not count at all (a traffic limit that cuts a request already under way is the exception). The last is a request inside an intercepted connection; the connection itself is counted, as a success. A connection from a tunnel device is closed rather than answered, so for one the status is what a proxy client would have been told.

Two things are not recorded: a request with wrong credentials, which belongs to no project, and a request that went through and got an error status from the target site, which is the site's answer rather than a failure of the pool.

Vendor credentials never reach the log: they are scrubbed from error texts, and the proxy is recorded by host and port. For an intercepted request the URL is kept without its query string, and any URL an error text quotes loses its query too.

Both are built to survive a broken upstream failing everything sent to it. Each gateway collects failures for 5 seconds and writes them to Redis in one round trip, so nothing on the request path waits for it:

- The **counts** go to hashes that the metrics flush job turns into rows of a sixth history table (`failure_metrics`), with the same compaction tiers and retention as the other five. One row per project, connector, kind and vendor status per flush, whatever the volume.
- The **examples** are kept per project and kind, so a flood of one kind never pushes out the examples of a rare one. Failures that differ only in time, session and step timings are one example with a count, whichever gateway saw them (the step timings shown are the first one's, a failure in another step is another example), so ten thousand identical timeouts are one row reading ×10K, and the row moves to the top each time the failure happens again. Each kind of a project gets at most `failure_log.max_per_window` distinct examples per window on each gateway; beyond that, failures are only counted. Each kind keeps the `failure_log.max_entries` examples seen last, each until it has not happened for `failure_log.retention_hours` (7 days).

See [Configuration]({{ site.baseurl }}/configuration#failure-log) for the settings and [`GET /projects/{id}/failures`]({{ site.baseurl }}/api#failure-log) for the API. Clearing the examples on the page does not touch the counts.

## Prometheus

Two kinds of Prometheus export exist.

**Per project**, `GET /api/v1/projects/{project_id}/metrics/prometheus` (signed in) exports what the Overview shows: the pool by status, the project's request, success, failure and byte counters, and each connector's traffic this period against its limit and price (`octoprox_proxies_total`, `octoprox_proxies_healthy`, `octoprox_proxies_quarantined`, `octoprox_requests_total`, `octoprox_requests_success_total`, `octoprox_requests_failure_total`, `octoprox_bytes_sent_total`, `octoprox_bytes_received_total`, and the `octoprox_connector_traffic_*` gauges of [Traffic Limits]({{ site.baseurl }}/traffic-limits#monitoring)). See the [API reference]({{ site.baseurl }}/api#prometheus-metrics).

**Per process**, `GET /metrics` on port 8000 of every process, without sign-in, exports how that process is doing. Every series carries a `role` label, and each process exports the families of the roles it runs, so a scrape of every pod or container covers the install. Values a process does not have yet (a gateway before its first feed, a controller before its first compile) are left out rather than reported as zero. The bundled cluster's nginx refuses `/metrics` (`403`) on its public port, where a scrape would reach whichever api it picked: scrape each container's port 8000 on the compose network instead (see [Deployment]({{ site.baseurl }}/deployment#inspecting-the-cluster)).

| Role | Families |
|------|----------|
| every | `octoprox_build_info` (labels `version`, `instance_id`), `octoprox_uptime_seconds`, `octoprox_ready` (1 while `/ready` answers 200), `octoprox_event_loop_lag_max_seconds`, `octoprox_event_loop_lag_mean_seconds`; per background loop (`loop` label) `octoprox_loop_running`, `octoprox_loop_runs_total`, `octoprox_loop_failures_total`, `octoprox_loop_consecutive_failures`, the job producers and job runners included |
| every process that follows a stream | per stream (`stream`: `changes`, `runtime`, `feed`): `octoprox_stream_lag_seconds`, how far behind the stream's newest entry the process's last read found it (0 when that read found nothing new or read in full; absent before the first read), `octoprox_stream_entries_total` and `octoprox_stream_full_reads_total` (`reason`: `start`, `gap`, `reconnect`, `reconcile`, each from the first scrape) |
| api | `octoprox_api_db_pool_size`, `octoprox_api_db_pool_checked_out`, `octoprox_api_db_pool_overflow` |
| gateway | `octoprox_gateway_requests_total` (`outcome`), `octoprox_gateway_bytes_total` (`direction`), the histograms `octoprox_gateway_request_duration_seconds` and `octoprox_gateway_request_step_duration_seconds` (`step`: `route`, `preflight`, `connect`, `handshake`, `response`), both by `connector` (its id), `name` (its name) and `outcome`, `octoprox_gateway_failures_total` (`kind`, every failure kind from the first scrape), `octoprox_gateway_in_flight`, `octoprox_gateway_connections`, `octoprox_gateway_feed_age_seconds`, `octoprox_gateway_preflight_checks_total`, `octoprox_gateway_preflight_rejections_total`, `octoprox_gateway_exclusions` (`kind`: `retired`, `quarantined`), `octoprox_gateway_pending` and `octoprox_gateway_dropped_total` (`buffer`), `octoprox_gateway_metric_flush_failures_total`, `octoprox_gateway_rate_limit_errors_total`, `octoprox_gateway_ip_databases` (`state`: `loaded`, `missing`); with tunnels, `octoprox_gateway_tunnel_up`, `octoprox_gateway_tunnel_restarts_total`, `octoprox_gateway_tunnel_denied_total` (`protocol`) and `octoprox_gateway_tunnel_connections` |
| controller | `octoprox_controller_leader`, `octoprox_controller_lease_terms_total`, `octoprox_controller_compiles_total`, `octoprox_controller_publishes_total`, `octoprox_controller_sections_written_total`, `octoprox_controller_sections_removed_total`, `octoprox_controller_compile_duration_seconds`, `octoprox_controller_publish_duration_seconds`; from the leader only, `octoprox_controller_failing` (`step`: `compile`, `publish`), `octoprox_controller_feed_sections`, `octoprox_controller_quarantined_proxies` and `octoprox_controller_blocked_connectors` |
| worker | per job kind (`kind`): `octoprox_worker_job_runs_total`, `octoprox_worker_job_failures_total` (batches whose run raised: delivered again, and given up after repeated deliveries), `octoprox_worker_job_duration_seconds_total` and `octoprox_worker_jobs_in_flight`; `octoprox_worker_sightings_consumed_total`, `octoprox_worker_sightings_dropped_total`, `octoprox_worker_observations_consumed_total` and `octoprox_worker_observations_dropped_total` (dropped: given up after repeated deliveries) |

Counters run from the process's start. The controller's install-wide gauges come only from the leader, so summing them over every controller counts each once. A metrics flush's time is the `metrics_flush` kind's `octoprox_worker_job_duration_seconds_total` over its runs.

The latency histograms count each finished request once, on the gateway that carried it: its whole [latency](#latency-where-the-time-goes), failures included under `outcome="failure"`, and each step it went through. Their buckets run from 1 ms to 60 s. For example, the 95th percentile of each step per connector, and the share of the latency each step takes:

```promql
histogram_quantile(0.95, sum by (le, name, step) (rate(octoprox_gateway_request_step_duration_seconds_bucket{outcome="success"}[5m])))

sum by (step) (rate(octoprox_gateway_request_step_duration_seconds_sum{outcome="success"}[5m]))
  / scalar(sum(rate(octoprox_gateway_request_duration_seconds_sum{outcome="success"}[5m])))
```

No process exports the depth of the work streams, since a scrape reads only the process's memory. The workers' autoscaler reads the streams from Redis itself (see [Running on Kubernetes]({{ site.baseurl }}/kubernetes#scaling)), and KEDA exports what it read as its own scaler metric, `keda_scaler_metrics_value`, one series per trigger: that is the gauge to chart and alert on for a backlog. The System page shows the same per stream: length, pending, queued, consumers and the oldest entry's age.

Useful alerts:

- `octoprox_ready == 0` for a while;
- `octoprox_controller_leader` summing to anything but 1, and `octoprox_controller_failing == 1`;
- a stream whose KEDA scaler metric stays above its trigger's threshold times the Deployment's maximum replicas: the workers cannot keep up with that kind;
- a stream whose KEDA scaler metric stays above zero while no Deployment takes its kind: nothing ever runs it;
- a growing `octoprox_worker_job_failures_total`, `octoprox_worker_sightings_dropped_total` or `octoprox_worker_observations_dropped_total`: batches failing, or given up after repeated deliveries (Postgres refusing them);
- a growing `octoprox_gateway_dropped_total` or `octoprox_gateway_metric_flush_failures_total`: Redis not keeping up.

Whether each gateway runs the latest feed is on the System page.

## Why This Limitation Exists

When HTTPS tunneling is used without TLS interception (MITM disabled), Octoprox creates a raw TCP tunnel between the client and the upstream server. The traffic inside this tunnel is encrypted - the proxy sees only opaque ciphertext and has no way to distinguish where one HTTP request ends and another begins.

This is by design: it preserves the client's TLS fingerprint (JA3/JA4), which is critical for anti-detection when scraping.

## MITM Mode and Accurate Counting

When [TLS interception](tls-interception) is enabled, Octoprox terminates the client's TLS session and parses each HTTP request individually using h11. In this mode, the proxy *can* see individual requests - however, the current metrics implementation still counts at the tunnel level.

> **Note:** Even with MITM enabled, request counting currently reflects tunnel count, not individual HTTP request count. Accurate per-request counting in MITM mode is planned for a future release.

## Impact on Other Features

This counting behavior also affects:

- **Rate limiting** - Rate limits are evaluated per CONNECT tunnel, not per HTTP request within a tunnel. A client sending 100 requests over a single keep-alive connection only triggers one rate limit check. [Traffic limits]({{ site.baseurl }}/traffic-limits) are unaffected: they count bytes, which are metered inside the tunnel.
- **Latency tracking** - `avg_latency_ms` is the average, over the successful requests, of the time from Octoprox taking a request until its tunnel through the upstream proxy is up (the first response line for plain HTTP). Choosing the proxy and the exit verification echo are part of it, as they are part of the client's wait, and [its steps](#latency-where-the-time-goes) say how much. It is not the latency of individual HTTP requests within the tunnel, nor how long the tunnel stays open.

## Workarounds

If accurate request counting is important for your use case, you have several options:

### Disable client-side connection pooling

Configure your HTTP client to not reuse connections. This forces a new CONNECT tunnel per request, making each request individually countable. For example, with the `rnet` Python library:

```python
from rnet import BlockingClient, Proxy, Impersonate

client = BlockingClient(
    impersonate=Impersonate.Firefox133,
    proxies=[Proxy.all("http://user:pass@proxy:8080")],
    timeout=15,
    pool_max_idle_per_host=0,  # Disable connection reuse
)
```

This keeps the client object reusable (no need to recreate it per request) while forcing a fresh CONNECT tunnel for each request.

Other HTTP libraries have similar options:

| Library | Setting |
|---------|---------|
| `rnet` (Python) | `pool_max_idle_per_host=0` |
| `requests` (Python) | Use `Session()` with a custom adapter setting `pool_maxsize=0` |
| `httpx` (Python) | `limits=httpx.Limits(max_keepalive_connections=0)` |
| `curl` | `--no-keepalive` |

### Use the MITM request log

When TLS interception is enabled, Octoprox records each individual HTTP request in the request log (visible in the web UI under the project's request inspector). This log contains accurate per-request data including method, URL, status code, and latency - even though the top-level metrics count at the tunnel level.

### Track requests client-side

If you control the client, maintain your own request counter alongside Octoprox metrics. Use Octoprox metrics for proxy health and bandwidth tracking, and your own counter for request volume.
