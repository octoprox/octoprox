---
layout: docs
title: Rate Limiting & Quarantine
nav_id: rate-limiting
---

# Rate Limiting & Quarantine

<p class="subtitle">Throttle per-proxy request rates and temporarily quarantine proxies that exceed their limit.</p>

Connectors support optional per-proxy rate limiting via the `rate_limit_config` field. When enabled, Octoprox tracks the number of requests each proxy handles within a sliding time window. If a proxy exceeds the configured limit, it is placed into **quarantine** for a randomized duration and excluded from proxy selection until the quarantine expires.

## How It Works

1. Each request through a proxy is recorded in a sliding time window.
2. When a proxy reaches `max_requests` within `window_seconds`, it enters quarantine.
3. The quarantine duration is randomly chosen between `quarantine_seconds_min` and `quarantine_seconds_max` to avoid all proxies coming out of quarantine simultaneously.
4. Quarantined proxies are excluded from proxy selection — they are still healthy, just temporarily resting.
5. If **all** healthy proxies for a project are quarantined, the client receives **HTTP 429 Too Many Requests** instead of the usual 502.

## Configuration

Rate limiting is configured per-connector in the **Rate Limiting** tab (UI) or via the `rate_limit_config` field (API):

| Field | Type | Description |
|-------|------|-------------|
| `max_requests` | integer | Maximum requests per proxy within the time window |
| `window_seconds` | integer | Sliding window duration in seconds (1–86400) |
| `quarantine_seconds_min` | integer | Minimum quarantine duration in seconds (1–86400) |
| `quarantine_seconds_max` | integer | Maximum quarantine duration in seconds (1–86400) |
| `sticky_quarantine` | boolean | Block sticky session fallback when quarantined (default: `false`) |

**Example** — limit each proxy to 100 requests per 60 seconds, quarantine for 2–5 minutes:

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

## Monitoring

- **Proxy list** — Quarantined proxies show an orange "quarantined" badge with a countdown showing when the proxy becomes available again.
- **Dashboard** — The pool metrics section includes a "Quarantined" counter.
- **Prometheus** — The `octoprox_proxies_quarantined` gauge tracks the number of quarantined proxies per project.
- **Manual override** — Quarantined proxies can be forcefully released from the proxy list using the unquarantine action.

## API Reference

See the [API Reference]({{ site.baseurl }}/api#rate-limiting-rate_limit_config) for configuration details and the unquarantine endpoint.
