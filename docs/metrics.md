---
layout: docs
title: Metrics & Request Counting
nav_id: metrics
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

A connection from a [WireGuard device]({{ site.baseurl }}/wireguard) is counted against the device as well, in the same pipeline, so a device has totals and a history of its own (`GET /projects/{id}/wireguard/peers/{id}/metrics/history`) that are cluster-wide and survive restarts. Device rows carry two extra counters the transparent listener reports per device: connections relayed by address because no destination name could be recovered, and encrypted-DNS connections closed. The table is shared by every tunnel protocol; a device's rows go when the device is removed, and the project's retention applies.

## Hosts: Where the Traffic Goes

Every request is also counted against the **destination host** it was for, under the connector that carried it: the `CONNECT` target for an HTTPS tunnel, the URL host for a plain HTTP request, and the recovered name (or the address, when no name could be recovered) for a connection from a tunnel device. Host names are folded to lower case without a trailing dot. The project's **Hosts** page in the web UI lists the hosts busiest first for a time range, with requests, success and failure counts, bytes each way, average latency and share, splits each host by connector, and charts the busiest hosts over time against the remainder. The API serves the same through [`GET /projects/{id}/metrics/hosts`]({{ site.baseurl }}/api#hosts) and `/metrics/hosts/history`.

This is the fifth history table (`host_metrics`), with the same flush, compaction tiers and retention as the other four, so the numbers lag live traffic by up to one flush interval (60 seconds by default). Rows go with their connector, as connector history does. Every host is kept by default: a host requested once costs one row, and a host requested constantly costs one row per minute, compacted to one per hour after a day. It is the one metrics table whose size depends on what clients request rather than on the pool, so an install that crawls the open web has two dials (see [Configuration]({{ site.baseurl }}/configuration#host-metrics)): `metrics.hosts.enabled` switches per-host counting off entirely, and `metrics.hosts.max_hosts` sets a ceiling on how many hosts each instance names per project in each 5-second metrics window, keeping the busiest ones; the rest are folded into a single `(other)` row, so their requests and bytes stay in the totals, just not by name.

The same counting rules apply here as above: a keep-alive tunnel is one request against its host however many HTTP requests it carried, while its bytes are exact.

## Why This Limitation Exists

When HTTPS tunneling is used without TLS interception (MITM disabled), Octoprox creates a raw TCP tunnel between the client and the upstream server. The traffic inside this tunnel is encrypted - the proxy sees only opaque ciphertext and has no way to distinguish where one HTTP request ends and another begins.

This is by design: it preserves the client's TLS fingerprint (JA3/JA4), which is critical for anti-detection when scraping.

## MITM Mode and Accurate Counting

When [TLS interception](tls-interception) is enabled, Octoprox terminates the client's TLS session and parses each HTTP request individually using h11. In this mode, the proxy *can* see individual requests - however, the current metrics implementation still counts at the tunnel level.

> **Note:** Even with MITM enabled, request counting currently reflects tunnel count, not individual HTTP request count. Accurate per-request counting in MITM mode is planned for a future release.

## Impact on Other Features

This counting behavior also affects:

- **Rate limiting** - Rate limits are evaluated per CONNECT tunnel, not per HTTP request within a tunnel. A client sending 100 requests over a single keep-alive connection only triggers one rate limit check. [Traffic limits]({{ site.baseurl }}/traffic-limits) are unaffected: they count bytes, which are metered inside the tunnel.
- **Latency tracking** - `avg_latency_ms` reflects the time to establish the CONNECT tunnel to the upstream proxy, not the latency of individual HTTP requests within the tunnel.

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
