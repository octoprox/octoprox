# Probe Traffic Metering - Tracked Follow-ups

Octoprox sends its own requests through upstream proxies: health checks,
location preflight echoes, attribution echoes and discovery echoes. None of
them is metered. Every traffic figure (per-proxy stats, `connector_metrics`,
tunnel devices, the Hosts page, traffic limits) counts client traffic only,
while the vendor bills all of it.

Found in Oct 2026 on the busy dynamic-gateway project (no TLS interception,
about 300 req/s): the vendor dashboard showed 2-3x octoprox's requests,
bytes sent and bytes received. Requirement: show probe traffic both
combined with client traffic and separately, labelled so users always know
which one they are looking at.

## What is not metered today

Only the proxy server's request path counts bytes: `_relay` and the plain
HTTP path in `api/core/proxy_server.py` get a `TrafficMeter` from
`ProxyManager.traffic_meter` and publish `request_completed`. Each of these
opens its own httpx client to the upstream instead:

- Health checks, `_check_proxy` in `api/core/health_checker.py`. One per
  proxy per `health_check_interval` (60s in production), unhealthy rows
  included. Dynamic gateways are skipped.
- Preflight, `PreflightChecker.check` in `api/geo/preflight.py` through
  `IpDiscoverer.discover_with_place` in `api/providers/sdk/sources.py`. On
  a dynamic gateway, a rotating request with a location the project's rules
  can refuse is echoed every time, uncached. Each mismatch retry renders a
  new vendor session and echoes again, up to `preflight_max_attempts` (3),
  see `api/geo/verifier.py`. A refused request leaves only echoes at the
  vendor and is not counted as a request by octoprox.
- Attribution echoes, `api/geo/attributor.py`, and strategy discovery,
  `api/providers/sdk/strategies.py`, through the same discoverer.

Measured cost of one echo (Oct 2026): the TLS handshake to
`octoprox.adthena.com` reads 5.8 KB and writes 1.6 KB, and `/echo` adds
174 B of headers and a 248 B body. That is about 6 KB down and 2 KB up per
echo, about 90% of it the handshake (`httpbin.org`: 4.5 KB / 1.7 KB). A
search request through a tunnel is about 2-4 KB up, so one or two echoes
per request roughly double or triple bytes sent. A successful results page
is roughly 100 KB down, so echoes only triple bytes received when the
average response is small (failures, captcha pages, refusals).

## Confirm the current gap first

Before building, check that echoes account for the whole difference:

1. Octoprox received bytes divided by requests for the connector. About
   6-15 KB per request means echoes explain it. About 100 KB means
   something else is adding traffic at the vendor: another consumer of the
   same credentials (another environment, Scrapoxy, scripts) or a time
   window or timezone mismatch.
2. The vendor's breakdown by target domain, if it has one: the echo host's
   share (whatever `echo_url` is set to) is probe traffic.
3. `preflight_checks` on `GET /api/geo/status` against requests per pod.
   It is in memory per pod since that pod started, not a cluster total.

## Meter probe traffic

- Purposes: `health_check`, `preflight`, `attribution` (attributor and
  discovery echoes). Health checks count on the proxy and connector.
  Preflight counts on the connector and on the project whose request
  triggered it. Attribution counts on the connector.
- Count wire bytes, not response sizes: the TLS handshake is most of an
  echo. httpx exposes no wire byte count, so this needs counting at the
  socket, for example a counting network backend under the transport.
  Check what httpx, httpcore and httpx-socks allow on the pinned versions.
- Keep probe counters next to the client ones rather than folding them in,
  so existing figures keep their meaning and history stays comparable.
  Either probe columns per purpose on `connector_metrics` and
  `project_metrics` (requests, bytes sent, bytes received), or a
  `probe_metrics` table keyed by connector, project and purpose. Same
  pipeline as today: pending deltas, `flush_metric_deltas` in
  `api/db/redis.py`, `MetricsFlusher`, compactor, backup.
- The Hosts page stays client traffic only: probes all go to the echo host
  and would crowd out real destinations.

## Show it so users know what they see

- Three named figures, the same words everywhere: "Client traffic"
  (requests your clients sent), "Probe traffic" (health checks and exit
  checks octoprox sent, broken down by purpose) and "Total" (what the
  vendor bills; compare this one with the vendor's dashboard).
- Client traffic stays the default on existing charts and tables. Where a
  view can show more, a visible toggle and a legend name what is
  included, never a silent change of meaning.
- Connector pages and traffic limit usage show Total with the split
  underneath, since that is the number that matches the invoice.
- Update `docs/metrics.md` and `docs/traffic-limits.md` with the
  definitions and a note on why vendor dashboards read higher.

Open decision: should a connector's traffic limit count Total or client
traffic? Total matches what the vendor charges, so it is the suggested
default. If chosen, probe bytes must also reach `TrafficLimiter.record` in
`api/core/traffic_limiter.py`.

## TLS interception accounting

Found during the same investigation, affecting only projects with TLS
interception. Fix before mixing in probe bytes, or Total stops adding up.

- With the curl_cffi and rnet engines, `_relay` opens an upstream tunnel
  through the vendor, then `create_relay` in `api/core/mitm/__init__.py`
  closes it unused and the engine opens its own. Each client connection
  costs the vendor two requests and adds a round trip before the client's
  `200 Connection Established`. The plain relay does reuse the tunnel.
- The engines decompress responses (`cffi_relay.py`, `rnet_relay.py`), and
  `api/core/mitm/handler.py` meters the decoded bytes written to the
  client. Results pages are often 3-6x larger decoded, so intercepted
  projects over-count bytes received relative to the vendor. Find what each
  engine reports on the wire, or label these figures as decoded size.

## Cheaper echoes (optional)

The handshake is about 90% of an echo, so on busy dynamic-gateway projects:

- An ECDSA certificate on the echo host roughly halves the certificate
  chain.
- TLS session resumption across echoes skips the certificate chain
  entirely. Resumption is between octoprox and the echo server, so it
  works across vendor exits.
- Plain HTTP echoes would be smallest, but the exit node could read or
  rewrite the answer, which preflight relies on. Not recommended.
