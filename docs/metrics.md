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

The **Projects** page shows every project's load side by side, for anyone signed in: its requests per minute now (over the last five minutes, as on its Overview), and over the chosen range its requests, success rate, bytes and a trend line. The table view sorts by any of the numbers, to rank projects by load. The API serves the same through [`GET /projects/load`]({{ site.baseurl }}/api#project-load).

In the web UI one time range (1h to 90d) covers every view of this history: the project list, the Overview charts, the traffic split, the Hosts page, the connector and device charts, provider accuracy and the system trends. Pick it on any page and the others follow; the choice is remembered in the browser. A view that cannot show the exact window (claim statistics are daily, project history stops at 30 days) shows the nearest one it has.

## Where the Numbers Come From

Requests are counted by the [gateway]({{ site.baseurl }}/architecture) that carries them, in memory, and nothing on the request path waits for the counting:

1. Every 5 seconds each gateway adds what it counted to counters in Redis, one set per proxy, project, connector, tunnel device and host, and in a project that records [exit reputation]({{ site.baseurl }}/exit-reputation) one per dynamic connector, site and requested place (the site windows), in one round trip. A write Redis did not confirm is kept and sent again, and Redis applies it once.
2. Once a minute (`database.metrics_flush_interval`) the controller publishes a metrics flush job, and a worker runs it: it moves the counters out of Redis into a staging hash named after the job, writes them as history rows and adds them to each proxy's, project's and device's running totals in one transaction that also records the job, then deletes the staging hash. The same transaction applies the minute's [exit reputation]({{ site.baseurl }}/exit-reputation#how-fast-a-dynamic-connector-reacts) evidence to the connectors' records and decides each record once: the requests a block rate is measured against, from the site windows (a connector with IPs takes them from its host counts), and the exit reports about connectors, which the workers add to the site windows as they take them. A flush that fails is run again and commits once; one given up before its commit puts the counters back for the next flush, so a Postgres outage loses no counts.
3. The totals the UI and API show (a proxy's or a project's requests, success rate, latency, bytes) are those running totals plus the counters in Redis not flushed yet, so they are within about 5 seconds of live traffic and read the same on every api process. For the few seconds a flush holds the counters between Redis and Postgres they read low by those counts, never double. History charts trail by up to one flush interval.

Proxy history goes with its proxy; the running totals of a removed proxy, project or device are dropped by the next hourly compaction job.

## Needs attention

A project's **Overview** opens with a **Needs attention** card: what in the project needs someone now, one line each, with a link to the page that fixes it. A **problem** (a red mark) means requests fail now, such as every connector turned off, no proxy that can take requests, a country nothing can serve, or a vendor rejecting a connector's login. A **warning** (an amber dot) means requests are degraded or about to be, such as a vendor refusing part of a connector's requests, a vendor proxy list that keeps failing to sync, a connector near its traffic limit, many proxies failing their health checks, or a [low success rate](#thresholds).

The lines are what Octoprox's background checks found. They check every project once a minute, and a project again within seconds of a change made in Octoprox: to a connector, its countries, the project's settings, its proxies or its [alert thresholds](#thresholds), or a connector's traffic limit starting or stopping to turn requests away. While such a check is under way the card shows a small spinning icon beside its title, so a fix clears its line within a couple of seconds of the save. Lines about requests cover the last 15 minutes, a minute or so behind live traffic since they read the recorded history, and go five minutes after the last failure they count.

The card always shows something and never changes size on its own: the first two lines, problems first, with the number of problems and warnings beside its title; **Checking for problems...** while it loads, and until the project has been checked for the first time; **Nothing needs attention right now.** when all is well; and **Could not check for problems right now.** with a way to try again. A line without a time of its own (a last failure, a last failed sync, a reset) says since when it has been there. When the last check is more than three minutes old, the card says when it was, since what it shows may be out of date. **Show all** opens the full list beside the page, where each line says since when and how many times it came back, with links to the project's [alerts](#alerts) and, for editors and admins, its alert thresholds.

A wider line stands for the lines that follow from it: every connector turned off, or no proxy that can take requests, covers lines such as connectors with no healthy proxy, countries nothing can serve, requests turned away for want of a proxy and a low success rate; and once more than three connectors have no healthy proxy, one line says how many instead of a line each. A covered line is not shown on the card or counted, but it stays open in the project's alerts, marked **Covered**.

Problems also show in the sidebar, as a red count beside **Overview**, and on the **Projects** page and in the project switcher, as a red mark with the count beside each project's name. Warnings show on the card only.

### Muting a line

Editors and admins can mute a line from its row or from the full list: for **1 day**, **1 week**, **1 month**, or **until it clears**. A mute is shared by everyone in the project: a muted line leaves the card's rows and counts, the sidebar and the project list, and the full list shows it under **Muted** with who muted it and until when. Viewers see mutes and cannot change them.

- A line that gets worse comes back: a warning muted that becomes a problem shows again while it is a problem, and is hidden again if it falls back to a warning before the mute ends.
- **Until it clears** ends once the line has stayed gone for 15 minutes, so a line that comes and goes stays muted, and the next time it happens after that it shows again.
- A covered line keeps its mute while it is covered.
- Each mute and unmute is recorded in the project's [History]({{ site.baseurl }}/audit-log), with the line and until when. A mute that ends because its line cleared or its time ran out records nothing, since no one ended it.

### The Projects page

Above the projects, everyone signed in sees what needs attention in the install itself, in plain words: no gateway accepting proxy connections, changes that have not taken effect yet, background work that is behind (proxy health checks, vendor proxy lists, cloud scaling, new countries, traffic limits, charts, location checks, exit reports), IP databases that could not be loaded or updated, and the database or the live data store out of reach. The background checks look for these once a minute, and a line shows once two checks about a minute apart have found it, so it shows 1-2 minutes after it starts, and a failover or an update that passes within a minute leaves no line. Each line says since when.

A few lines come from the server answering the page rather than from the checks: the database or the live data store out of its reach, and the checks themselves stopped (**Octoprox is not checking for problems**, when no worker runs the `alerts` job) or behind (**Octoprox last checked for problems at...**, when the last check is more than three minutes old). These show once two reads in a row have found them, so a passing moment never flashes up.

Admins get a link to **System** or **IP attribution** for the details; everyone else is told that an admin can see more there. These lines cannot be muted.

### Alerts

Every line the checks find is kept as an alert, with when it opened and when it resolved, so a problem that cleared before anyone looked still leaves a trace. A project's **Alerts** page, after **Failures** in the sidebar, lists them newest first for anyone signed in:

- **Open**, **Resolved** or **All**, with how many of each; a severity (an open alert's now, the worst a resolved one reached), a kind, and a search of the subject, such as a connector's name, applied on Enter or **Apply**.
- The shared time range, up to 90 days: the alerts open at any point in it.
- Each alert's sentence, when it opened, how long it lasted, how many times it came back, and its status: **Open**, **Muted**, **Covered** (open, but a wider line stands for it on the card) or **Resolved**.
- Alerts that open while the page is shown wait behind a button above the list, such as **2 new, show**, so the rows never move under the reader.
- A row opens its details: the sentence and the counts behind it, when it opened, came back, was last seen and resolved, its severity now and the worst it reached, its mute (which editors and admins can change there), and the link to the page that fixes it.

An alert resolves as soon as a check no longer finds its line, and the card clears at once. A line back within 15 minutes of resolving reopens the same alert instead of adding one, so a condition that comes and goes is one alert with a count of returns, and the card's "since" is its latest return.

Resolved alerts are kept as long as the project's metrics (**Metrics retention** on the **General** tab of Project settings), or for the days set under **Keep resolved alerts** on its **Alert settings** tab; 0 keeps them forever. Open alerts are never deleted. The hourly compaction deletes the older ones, and a [backup]({{ site.baseurl }}/api#backup--migration) carries them with the history.

Admins also see the install's alerts under **Administration → Alerts**: the install-wide lines the checks found, over time, with the same filters but the search, the same details, and a link to **System** or **IP attribution**. Resolved ones are kept for 90 days until an admin changes it on that page's *Retention* card (0 keeps them forever); a change is recorded in its History.

### Thresholds

Where each line starts is a project setting. Editors and admins change it on the **Alert settings** tab of Project settings, which **Show all** on the card links to as well. Each field shows its default and can be reset to it; only the values that differ from the defaults are kept. A save takes effect at once, since the project is checked again within seconds, and is recorded in the project's [History]({{ site.baseurl }}/audit-log), each setting under **Alerts**. A warning cut-off cannot be above its problem cut-off, and the success rate's problem cut-off must be below its warning one.

| Group | Setting | Default |
|---|---|---|
| Success rate | Warn about a low success rate | on |
| | Warning when below | 80% |
| | Problem when below (0 never makes it a problem) | 50% |
| | Only with at least | 100 requests |
| Requests turned away | Only with at least | 10 requests |
| | Warning from | 5% |
| | Problem from | 25% |
| | Locations that could not be read: warning from | 10 requests |
| Vendor refusals | Only with at least | 10 refused |
| | Login rejected (407): problem from | 20% |
| | Too many requests (429): warning from, problem from | 5%, 50% |
| | Not allowed (403): warning from, problem from | 5%, 50% |
| | Cannot serve (400, 502, 503): warning from | 30% |
| | Any other refusal: warning from | 30% |
| Connectors | Failing requests: only with at least | 50 requests |
| | Failing requests: warning from, problem from | 30%, 80% |
| | Sync or cloud errors: show after | 1 in a row |
| | Still deleting: show after | 30 minutes |
| Proxies and exits | Unhealthy proxies: from, and at least | 25%, 3 proxies |
| | Exits outside the listed countries: from, and at least | 10%, 3 proxies |
| | Exits not where the vendor says: from, and at least | 25%, 3 proxies |
| | A site blocking every connector: from | 50% blocked |
| Keep resolved alerts | Keep resolved alerts for | the metrics retention |

Requests turned away and vendor refusals are shares of the last 15 minutes' requests (a connector's own, for a vendor line). A connector near its [traffic limit]({{ site.baseurl }}/traffic-limits) is flagged from the connector's own warning share (`warn_percent`), not from these settings.

**Low success rate** is on for every project until it is turned off: a warning when fewer than 80% of the requests that reached a proxy succeeded in the last 15 minutes, a problem below 50%, once there were at least 100 of them. It is the Overview's success rate; requests turned away before a proxy have lines of their own. It shows beside the lines that name a cause, such as a vendor refusing a connector's requests, rather than being hidden by them, so only every connector turned off or no proxy able to take requests covers it. Its percentage is rounded down: 79.6% reads 79%, never 80% under an 80% cut-off.

The API serves all of it; see [Needs attention]({{ site.baseurl }}/api#needs-attention) and [Alerts]({{ site.baseurl }}/api#alerts).

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
| No proxy available | No healthy proxy matched the host's domain filters and the requested location, or the proxy a request named with [`-prxy-`]({{ site.baseurl }}/routing-strategies#pinning-a-proxy) cannot serve it | 502 |
| All proxies quarantined | Every proxy that could serve the request is resting after its rate limit, or the one named with `-prxy-` is | 429 |
| Exits avoided | [Exit reputation]({{ site.baseurl }}/exit-reputation#routing) keeps the IP of every proxy that could serve the request away from its site, or of the one named with `-prxy-`, or of a `sticky` session's proxy while the project does not move refused sessions | 502, with the site in the reason phrase and a `Proxy-Status` header |
| Traffic limit reached | The connectors that could serve the request are over their period's traffic, or the connector of the proxy named with `-prxy-` is | 509 (or as configured) |
| Invalid location | The username named a state or city without a country | 400 |
| Intercepted request failed | With [TLS interception]({{ site.baseurl }}/tls-interception), one request inside the connection could not be relayed upstream. When the upstream proxy refused the engine's tunnel, the vendor's status is kept as for a refusal | 502 |

The first seven are exactly the requests Overview counts as failures, so the summary's upstream failures match Overview's failure count for the same window, apart from traffic limits that cut a request already under way: Overview counts those too, and the summary lists them with the refusals. The next five are requests Octoprox refused before using any proxy, which the request metrics do not count at all (a traffic limit that cuts a request already under way is the exception). The last is a request inside an intercepted connection; the connection itself is counted, as a success. A connection from a tunnel device is closed rather than answered, so for one the status is what a proxy client would have been told.

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
| worker | per job kind (`kind`): `octoprox_worker_job_runs_total`, `octoprox_worker_job_failures_total` (batches whose run raised: delivered again, and given up after repeated deliveries), `octoprox_worker_job_duration_seconds_total` and `octoprox_worker_jobs_in_flight`; `octoprox_worker_sightings_consumed_total`, `octoprox_worker_sightings_dropped_total`, `octoprox_worker_observations_consumed_total` and `octoprox_worker_observations_dropped_total` (dropped: given up after repeated deliveries); `octoprox_worker_exit_reports_consumed_total`; `octoprox_worker_exit_reports_dropped_total`, the [exit reports]({{ site.baseurl }}/exit-reputation#reporting-exits) the workers dropped without applying them, by `reason` (every reason from the first scrape): `project` (its project gone, or its exit reputation `off`), `connector` (its connector gone, or another project's), `ip_needed` (exit headers naming a connector with IPs without its exit IP), `ip_not_seen` (exit headers naming an exit IP no row of the connector was last seen exiting from) and `given_up` (given up after repeated deliveries); `octoprox_worker_exit_reports_total`, the [exit reports]({{ site.baseurl }}/exit-reputation#reporting-exits) the workers took, by `connector` (its id), `name` (its name) and `verdict` (`good`, `bad`): an exit IP's record has them at once, a connector's at the next metrics flush |

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
- a growing `octoprox_worker_job_failures_total`, `octoprox_worker_sightings_dropped_total`, `octoprox_worker_observations_dropped_total` or `octoprox_worker_exit_reports_dropped_total{reason="given_up"}`: batches failing, or given up after repeated deliveries (Postgres refusing them);
- a growing `octoprox_worker_exit_reports_dropped_total{reason!="given_up"}`: clients send exit reports that name no exit of their project, mostly wrong exit headers ([which ones]({{ site.baseurl }}/exit-reputation#reporting-exits));
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
- **Exit reputation** - a dynamic connector's [block rate]({{ site.baseurl }}/exit-reputation#a-connectors-reputation-for-a-site) is bad reports per tunnel, so report at most one block per connection.

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
