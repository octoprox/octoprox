---
layout: docs
title: "Rate Limiting and Proxy Quarantine"
nav_id: rate-limiting
description: "Throttle requests per proxy with a sliding window and automatically quarantine proxies that exceed the limit for a randomised cooldown."
---

# Rate Limiting & Quarantine

<p class="subtitle">Throttle per-proxy request rates and temporarily quarantine proxies that exceed their limit.</p>

Connectors support optional per-proxy rate limiting via the `rate_limit_config` field. When enabled, Octoprox tracks the number of requests each proxy handles within a sliding time window. If a proxy exceeds the configured limit, it is placed into **quarantine** for a randomized duration and excluded from proxy selection until the quarantine expires.

Rate limiting is about request pace per proxy. To cap the **bytes** a connector carries per billing period, and to price them, see [Traffic Limits & Billing]({{ site.baseurl }}/traffic-limits).

## How It Works

1. Each request through a proxy is recorded in a sliding time window.
2. When a proxy reaches `max_requests` within `window_seconds`, it enters quarantine.
3. The quarantine duration is randomly chosen between `quarantine_seconds_min` and `quarantine_seconds_max` to avoid all proxies coming out of quarantine simultaneously.
4. Quarantined proxies are excluded from proxy selection - they are still healthy, just temporarily resting.
5. If **all** healthy proxies for a project are quarantined, the client receives **HTTP 429 Too Many Requests** instead of the usual 502.

## Configuration

Rate limiting is configured per-connector in the **Rate Limiting** tab (UI) or via the `rate_limit_config` field (API):

| Field | Type | Description |
|-------|------|-------------|
| `max_requests` | integer | Maximum requests per proxy within the time window |
| `window_seconds` | integer | Sliding window duration in seconds (1-86400) |
| `quarantine_seconds_min` | integer | Minimum quarantine duration in seconds (1-86400) |
| `quarantine_seconds_max` | integer | Maximum quarantine duration in seconds (1-86400) |
| `sticky_quarantine` | boolean | Block sticky session fallback when quarantined (default: `false`) |

**Example** - limit each proxy to 100 requests per 60 seconds, quarantine for 2-5 minutes:

```json
{
  "rate_limit_config": {
    "max_requests": 100,
    "window_seconds": 60,
    "quarantine_seconds_min": 120,
    "quarantine_seconds_max": 300
  }
}
```

To disable rate limiting, set `rate_limit_config` to `{}` (the default).

## Sticky Session Quarantine

By default, when a proxy is quarantined and the project uses `sticky` routing, the sticky strategy falls back to a different proxy for the affected session. This means the client transparently gets a new IP.

When `sticky_quarantine` is set to `true`, this fallback is disabled: a session whose assigned proxy is quarantined will receive **HTTP 429** instead of being reassigned. The client must start a new session (with a different `-sessid-` value) to get a different proxy.

This is useful when you need strict IP-session affinity and want the client to explicitly control when to switch IPs.

## With Several Gateways

The window and the quarantine are shared by every gateway, through Redis:

- Every gateway counts its completed requests into the same per-proxy window, so the limit applies to the proxy's traffic across the install, not per gateway.
- The gateway whose request fills the window takes the proxy out of its own selection at once and adds it to one sorted set of quarantines in Redis, `o6:proxy:quarantined`. The controller carries the quarantine to every other gateway in the [routing feed]({{ site.baseurl }}/architecture#per-key-request-state), so they stop selecting the proxy within a fraction of a second; a request or two may reach it on another gateway meanwhile. The tripping gateway keeps the proxy out on its own for at most ten seconds; after that the routing feed decides there too.
- Releasing a proxy by hand removes it from the set; every gateway, the one that tripped included, routes to it again within moments.
- Changing a connector's rate limit releases its quarantined proxies.

If Redis cannot be reached, a gateway lets requests through rather than refuse them for want of a count. If Redis is emptied, the windows and quarantines in it are gone, so every proxy starts with a fresh window.

## Monitoring

- **Proxy list** - Quarantined proxies show an orange "quarantined" badge with a countdown showing when the proxy becomes available again.
- **Dashboard** - The pool metrics section includes a "Quarantined" counter.
- **Prometheus** - The `octoprox_proxies_quarantined` gauge tracks the number of quarantined proxies per project.
- **Manual override** - Quarantined proxies can be forcefully released from the proxy list using the unquarantine action.

## API Reference

See the [API Reference]({{ site.baseurl }}/api#rate-limiting-rate_limit_config) for configuration details and the unquarantine endpoint.
