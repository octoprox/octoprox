---
layout: docs
title: "Traffic Limits and Proxy Billing"
nav_id: traffic-limits
description: "Cap the bytes a connector carries per day, week or month, choose what happens at the limit, and see what each proxy pool costs with a price per GB."
---

# Traffic Limits & Billing

<p class="subtitle">Cap the bytes a connector carries per billing period, decide what happens at the cap, and see what the traffic costs.</p>

Every connector meters the traffic through its proxies: bytes in both directions, summed over a period that matches the vendor's billing cycle. On top of that you can set a **limit** for the period, choose what Octoprox does when it is reached, and enter the **price per GB** you pay the vendor so usage shows up as spend.

Traffic limits are per connector because that is where the vendor bill lands: one Bright Data zone, one Oxylabs plan, one cloud region.

## How It Works

1. Bytes are counted while a transfer runs, not only when it ends. A keep-alive tunnel open for an hour is charged against the limit as it goes (every 1 MiB or 5 seconds, whichever comes first), so the number in the UI moves within seconds and a long download is held to the cap while it runs (to within a few seconds of traffic, see [Multi-gateway Behaviour](#multi-gateway-behaviour)).
2. Usage is summed over the current **period**: a calendar day, a week starting on a weekday you pick, or a month starting on a day of the month you pick. Periods are in UTC. Each connector keeps its own history table (`connector_metrics`), so the total survives its proxies being rotated or re-synced.
3. At `warn_percent` of the limit the connector is flagged as *near limit* in the connector list, the Overview and the logs.
4. At the limit the connector's **action** applies:

| Action | What happens |
|--------|--------------|
| `alert` (default) | Keeps routing. The connector shows *Over limit · alert only*, a warning is logged, the Prometheus gauge moves. |
| `block` | The connector stops taking new requests and the other connectors share its weight. Transfers already running finish. |
| `interrupt` | Like `block`, and the transfers still running on the connector are closed too, on every gateway. |

5. A blocked connector is released when its period rolls over, when the limit is raised, when the action is changed to `alert`, or when usage is reset by hand. A reset or a limit edit asks a worker to decide again at once; until it has, the connector shows *Pending* (see [Multi-gateway Behaviour](#multi-gateway-behaviour)).

When every connector that could serve a request is blocked, the client gets the connector's `limit_status`: **509 Bandwidth Limit Exceeded** by default, or 429 for clients hard-wired to it. Either way the response carries `Proxy-Status: octoprox; error=bandwidth_limit_exceeded` ([RFC 9209](https://www.rfc-editor.org/rfc/rfc9209)). 509 is the default because it cannot be mistaken for the [rate limiter's 429]({{ site.baseurl }}/rate-limiting) or for a misconfiguration; vendors themselves do not agree (Oxylabs answers 407, Bright Data 502, Decodo 429). Under TLS interception the next request on a cut connection gets the same status as an HTTP response instead of a dropped socket.

Cloud connectors take no scaling step while blocked: the requests they refuse would read as demand. Rotation and draining go on. Sticky sessions bound to a blocked connector fall back to another connector, as they do for quarantine.

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

**Reset usage** in the connector editor (editors and admins) starts the count over from now: for a vendor top-up, a plan change mid-cycle, or after moving a connector to a new account. History is untouched; only the count against the limit restarts. A block raised by the limit is lifted by the traffic decision the reset asks for, normally within a second; until then the connector shows *Pending* beside its block, and the gateways keep the block. The count after a reset includes the traffic metered before it that is not yet flushed to the history (up to `database.metrics_flush_interval`, a minute by default), so a reset right after a block can leave the block in place; reset again after a minute. The reset point is shown as *Counting since* until the period ends. The same is available as `POST /api/v1/projects/{project_id}/connectors/{connector_id}/traffic/reset`.

## Monitoring

- **Connector list** - a *Traffic* column with the usage bar and, with a price, the spend; *Pending*, *Near limit* and *Over limit* badges in the status column, whose value (*Pending* for a connector waiting for a decision) also orders the column's sort.
- **Connector editor** - a *Traffic this period* section with used bytes up and down, the limit and action, spend, the period and when it resets, and a chart of the connector's traffic over 24 hours, 7 or 30 days.
- **Overview** - the usage bar next to each connector, an orange dot for a blocked one, the number of connectors over their limit, and the spend tile. The dot, the count and the bar's colour follow the block the gateways enforce, pending or not.
- **Traffic split** - a blocked connector is listed as *over traffic limit* with its weight redistributed.
- **Logs** - `Connector approaching its traffic limit`, `Connector traffic limit reached`, `Connector blocked by its traffic limit` and `Connector released from its traffic limit`, each with the connector, project and byte counts, in the log of the worker that decided: the block and the release once per change, approaching and reaching the limit once per period on each worker process.
- **Prometheus** - per connector: `octoprox_connector_traffic_bytes`, `octoprox_connector_traffic_limit_bytes`, `octoprox_connector_traffic_cost` and `octoprox_connector_traffic_blocked`.
- **API** - every connector response carries `traffic_usage`; see the [API reference]({{ site.baseurl }}/api#traffic-limits-traffic_config).

## Multi-gateway Behaviour

Usage is one number per connector across the install, and one job decides on it: the `traffic` job, on whichever worker takes it.

1. Each gateway counts its own transfers as they happen and adds them to the connector's counter in Redis every 5 seconds. In the same step it asks for a traffic decision for every connector with a limit whose bytes it carried.
2. A worker decides: it reads the connector's history for the period (written into `connector_metrics` once a minute by the metrics flush job), then the counters not flushed yet, compares the total with the limit, and stores the decision in Redis when it changes.
3. The controller publishes the change in the runtime section of the [routing feed]({{ site.baseurl }}/architecture#the-routing-feed), and every gateway applies it at once.
4. Every block is decided again each minute, so a block whose period ended, a lift lost to a Redis blip, or a decision that crossed an edit lasts a minute at most.

So a connector can overshoot its limit by what it carries in a few seconds: a meter's report (1 MiB or 5 seconds), a 5-second metering window, then the decision, the publish and the apply. Set the limit that much under the plan for a connector whose traffic in those seconds matters. Under `interrupt`, every gateway cuts the connector's running transfers at their next progress report once the block reaches it.

**A moment's undercount.** While a metrics flush moves a window from Redis into Postgres, which takes seconds, a decision reads those bytes in neither place: it counts them short, never twice. If that decision lands under the limit, nothing asks again until the connector's next metering window that carries bytes, so the block can land up to one metering window late.

**What the UI and the API show.** The block is the decision the gateways enforce, so the connector list, the pool counts, the traffic split and the `octoprox_connector_traffic_blocked` gauge agree with the gateways. The usage numbers (bytes this period, percent, status and spend) are computed from the flushed history plus the counters not flushed yet, so they move with the traffic, and at once after a reset or a limit edit.

**Pending.** A usage reset, or an edit of what decides the block (the limit, the action, the period or the reset day), asks a worker for a decision at once and marks the connector as pending. Until the decision lands, normally within a second, the gateways keep enforcing the decision they have, and the connector shows *Pending*, with a hover saying a worker is deciding its traffic state and, while it is blocked, that the block stands until it does. A decision that is lost is made by the minute's re-check, so a connector stays pending for a minute at most. An edit of the price, the currency, the warning line or the status clients get while blocked decides no block: it applies at once and never shows pending.

With Redis down the api shows no blocks and nothing pending, while the gateways keep the blocks they last applied. With no worker or no controller running, the gateways keep the blocks they last applied too, and a connector that reaches its limit meanwhile is blocked once a decision is made and published again.
