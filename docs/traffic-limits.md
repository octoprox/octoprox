---
layout: docs
title: Traffic Limits & Billing
nav_id: traffic-limits
---

# Traffic Limits & Billing

<p class="subtitle">Cap the bytes a connector carries per billing period, decide what happens at the cap, and see what the traffic costs.</p>

Every connector meters the traffic through its proxies: bytes in both directions, summed over a period that matches the vendor's billing cycle. On top of that you can set a **limit** for the period, choose what Octoprox does when it is reached, and enter the **price per GB** you pay the vendor so usage shows up as spend.

Traffic limits are per connector because that is where the vendor bill lands: one Bright Data zone, one Oxylabs plan, one cloud region.

## How It Works

1. Bytes are counted while a transfer runs, not only when it ends. A keep-alive tunnel open for an hour is charged against the limit as it goes (every 1 MiB or 5 seconds, whichever comes first), so the number in the UI moves in real time and a long download cannot slip past the cap.
2. Usage is summed over the current **period**: a calendar day, a week starting on a weekday you pick, or a month starting on a day of the month you pick. Periods are in UTC. Each connector keeps its own history table (`connector_metrics`), so the total survives its proxies being rotated or re-synced.
3. At `warn_percent` of the limit the connector is flagged as *near limit* in the connector list, the Overview and the logs.
4. At the limit the connector's **action** applies:

| Action | What happens |
|--------|--------------|
| `alert` (default) | Keeps routing. The connector shows *Over limit · alert only*, a warning is logged, the Prometheus gauge moves. |
| `block` | The connector stops taking new requests and the other connectors share its weight. Transfers already running finish. |
| `interrupt` | Like `block`, and the transfers still running on the connector are closed too, on every Octoprox instance. |

5. A blocked connector is released when its period rolls over, when the limit is raised, when the action is changed to `alert`, or when usage is reset by hand.

When every connector that could serve a request is blocked, the client gets the connector's `limit_status`: **509 Bandwidth Limit Exceeded** by default, or 429 for clients hard-wired to it. Either way the response carries `Proxy-Status: octoprox; error=bandwidth_limit_exceeded` ([RFC 9209](https://www.rfc-editor.org/rfc/rfc9209)). 509 is the default because it cannot be mistaken for the [rate limiter's 429]({{ site.baseurl }}/rate-limiting) or for a misconfiguration; vendors themselves do not agree (Oxylabs answers 407, Bright Data 502, Decodo 429). Under TLS interception the next request on a cut connection gets the same status as an HTTP response instead of a dropped socket.

Cloud connectors over their limit are not scaled up by the auto-scaler; rotation and scale-down still run. Sticky sessions bound to a blocked connector fall back to another connector, as they do for quarantine.

## Configuration

Traffic settings live in the **Traffic** tab of the connector editor (UI) or the `traffic_config` field (API). Only explicit choices are stored; a key at its default is dropped, so an unconfigured connector shows `{}` and is still metered.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `limit_bytes` | integer | none | Bytes per period before the action applies. Omit to meter without a limit. |
| `period` | `day`, `week`, `month` | `month` | How often usage starts over. |
| `reset_day` | integer | `1` | Day of month (1-28) for `month`; weekday (1 = Monday, 7 = Sunday) for `week`. |
| `action` | `alert`, `block`, `interrupt` | `alert` | What happens at the limit. |
| `warn_percent` | integer | `80` | Share of the limit at which the connector is flagged (1-100). |
| `limit_status` | `509` or `429` | `509` | Status sent when no connector is left to serve a request. |
| `price_per_gb` | number | none | What you pay the vendor per decimal GB. |
| `currency` | string | `USD` | ISO 4217 code for the price. |

**Example** - a 500 GB monthly plan renewing on the 15th at 8 USD/GB, stop routing at the cap:

```json
{
  "traffic_config": {
    "limit_bytes": 500000000000,
    "period": "month",
    "reset_day": 15,
    "action": "block",
    "warn_percent": 90,
    "price_per_gb": 8
  }
}
```

**Example** - a daily safety cap on a cloud region, cut running transfers, no price:

```json
{
  "traffic_config": {
    "limit_bytes": 50000000000,
    "period": "day",
    "action": "interrupt"
  }
}
```

**Example** - metering and spend only:

```json
{
  "traffic_config": {
    "price_per_gb": 3.5,
    "currency": "EUR"
  }
}
```

### Units

Limits and prices use decimal gigabytes, as vendors bill them: 1 GB = 1,000,000,000 bytes. The UI enters GB and shows usage in the same units next to a limit or a price.

Octoprox meters the bytes that pass through it. The vendor's meter differs a little: TLS handshakes, how headers are counted, and whether a vendor bills request or response bytes. Compare one invoice against the Octoprox figure for the same period and set the limit slightly under the plan.

## Billing

With a `price_per_gb`, every usage figure is also shown as money:

- **Connector list** and **connector editor** - spend this period next to the usage bar, with the per-GB rate.
- **Overview** - a *Spend this period* tile summing the priced connectors, when they share a currency, and the number of connectors over their limit.
- **Traffic split** - a *Spend* column per connector for the selected window.
- **Prometheus** - `octoprox_connector_traffic_cost` per connector.

Prices are not stored with the metrics, so past periods and history charts are priced at the current rate. Change the price and every figure moves with it.

## Resetting Usage

**Reset usage** in the connector editor (editors and admins) starts the count over from now: for a vendor top-up, a plan change mid-cycle, or after moving a connector to a new account. History is untouched; only the count against the limit restarts, and a block raised by the limit is lifted. The reset point is shown as *Counting since* until the period ends. The same is available as `POST /api/v1/projects/{project_id}/connectors/{connector_id}/traffic/reset`.

## Monitoring

- **Connector list** - a *Traffic* column with the usage bar and, with a price, the spend; *Near limit* and *Over limit* badges in the status column.
- **Connector editor** - a *Traffic this period* section with used bytes up and down, the limit and action, spend, the period and when it resets, and a chart of the connector's traffic over 24 hours, 7 or 30 days.
- **Overview** - the usage bar next to each connector, an orange dot for a blocked one, and the spend tile.
- **Traffic split** - a blocked connector is listed as *over traffic limit* with its weight redistributed.
- **Logs** - `Connector approaching its traffic limit`, `Connector traffic limit reached`, `Connector blocked by its traffic limit` and `Connector released from its traffic limit`, each with the connector, project and byte counts.
- **Prometheus** - per connector: `octoprox_connector_traffic_bytes`, `octoprox_connector_traffic_limit_bytes`, `octoprox_connector_traffic_cost` and `octoprox_connector_traffic_blocked`.
- **API** - every connector response carries `traffic_usage`; see the [API reference]({{ site.baseurl }}/api#traffic-limits-traffic_config).

## Multi-instance Behaviour

Usage is one number per connector across the cluster. Each instance counts its own transfers as they happen, moves them into Redis on the 5 second metric flush, and hears about the other instances' bytes on the same channel; the leader writes the totals to `connector_metrics` once a minute. An instance sees a peer's unflushed bytes with up to one flush interval of delay, which is the accepted error at the limit.

A block is a Redis key that expires with the period, announced to every instance so the connector leaves selection everywhere within a moment. Under `interrupt`, transfers on every instance are cut the same way.
