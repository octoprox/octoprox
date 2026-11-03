# IP Feedback - Design

A client sees what a site thinks of an exit: a captcha, a 403, a clean page.
Octoprox only sees a tunnel. Exit reports let a client tell Octoprox that an
exit IP was good or bad for a site. Octoprox keeps that with the IP, next to
what it sees of the IP itself (refusals, tunnel failures, location
mismatches), for as long as the IP matters. Routing then keeps bad exits
away from a site, moves a session off a bad exit, and prefers static rows
with a good record under `health_based`.

Vendors hand the same IPs out again, often days apart (over 60% reuse seen
on a dynamic provider), so the record has to outlive any one session or
cooldown.

Designed Nov 2026, nothing built yet.

## Decisions

| Topic | Decision |
|-------|----------|
| Channel | A report API. No endpoint on the gateways, and no implicit detection, which would only see plain `http://` and TLS-interception traffic |
| Naming the exit | The proxy username as used (carrying `-sessid-` or the new `-tag-`), an exit IP, or a proxy id. Named IP lists can be uploaded too |
| Where it lives | Postgres, per project, keyed by IP: it holds whichever connector hands the IP out, and covers uploaded IPs no connector has served yet. Redis holds only the routing copy, rebuilt from Postgres |
| Scope | Per site. A report names the site, and `google.com` covers `www.google.com`, as domain filters match (`matches_domain`) |
| Recency | Lifetime counts for reporting, and a decayed score for decisions: a report's weight halves every half-life (a project setting) |
| Own outcomes | Counted per IP next to the reports: successes, vendor refusals, tunnel failures, location mismatches |
| Session on rejection | A per-project setting, `move` or `refuse`, for every preflight rejection, location ones included. Built first |
| Good reports | Counted, weigh against bad ones in the decision, and become a third term of the `health_based` score for static rows |
| IP lists | Named lists with a verdict, a site or all sites, and an optional expiry. Single IPs only in v1, no CIDR ranges |

## Build order

1. Session move on preflight rejection.
2. Exit records for `-sessid-` and `-tag-`.
3. Per-IP report rows, the report API and its consumer, the routing copy,
   avoidance.
4. Own outcomes from the gateways.
5. Named IP lists.
6. `health_based` term, and the views.

## 1. Session move on preflight rejection

Today:

- A dynamic gateway derives the vendor session from `project:sessid:place`
  (`render_request` in `api/providers/sdk/provider.py`), so the same
  `-sessid-` always gets the same exit. A rejected session is refused until
  its cached verdict expires: the shorter of `preflight_session_ttl_seconds`
  and the vendor's session length. Nothing can keep the client's sessid and
  ask the vendor for a new exit. Under a strategy other than sticky, the
  retry excludes the gateway row (`ExitVerifier.verify`), so the session
  moves to another connector or is refused.
- A sticky session on static rows is never moved after a rejection
  (`StickySessionStrategy.allows_exit_reselection`).

A new project setting (name open) says what a rejection does to a session:

- `refuse`, the default: today's behaviour.
- `move`:
  - Dynamic: the derivation gets a generation, `project:sessid:place:gen`.
    Generation 0 derives the same id as today, so no session moves on
    upgrade. A rejection bumps the generation, renders the same row again
    and echoes, within `preflight_max_attempts`. The new exit is judged like
    any other, on location and the IP's record both.
  - Static sticky: the session is bound to another eligible row, as happens
    when its row leaves the pool.

Where the generation lives: the cached verdict of a client session moves
from a key per vendor session (`GEO_PREFLIGHT_KEY` plus
`:vendor_session:place`) to one key per client session and place, holding
the current generation and that generation's verdict.

- One GET still answers both, so a request costs the same round trip as
  today.
- The key outlives the verdict: its TTL is the verdict's plus an idle
  allowance (the sticky binding's 300 s), and every echo rewrites it. A
  session in use keeps its generation, and a stale verdict is echoed again
  on the current generation, never on 0.
- Two gateways bumping at once both compute `gen + 1` and render the same
  vendor session, so they agree without a lock. The generation is only ever
  written upwards.

## 2. Exit records

A report by username needs the exit that username was using. For a request
carrying `-sessid-` or `-tag-`, the gateway records in Redis, per project,
sessid or tag, and place: the proxy id, the connector id, the exit IP when
known, and when. This one is short-lived by nature, so Redis is its home.

- Written when the exit changes and refreshed before it expires, as sticky
  bindings are (`StickySessionStrategy._refresh_redis`), so a busy session
  costs no write per request. A tag is usually per request, so that is one
  write per tagged request.
- Kept a few minutes after the last request.
- Dynamic rows: the IP is known only when preflight echoed the request.
  Static rows: the row's last known IP (`META_DISCOVERED_IP`).

`-tag-<x>` is a new username parameter with no routing effect. Project
usernames are checked against the reserved separators when they are created
(`api/routes/projects.py`), so an existing username containing `-tag-` would
stop parsing: check for one before adding the separator.

## 3. Reports

### API

`POST /api/v1/projects/{id}/exit-reports`, a batch:

```json
{
  "reports": [
    {"username": "myuser-cc-us-sessid-a1", "site": "google.com", "verdict": "bad", "reason": "captcha"},
    {"ip": "203.0.113.7", "site": "amazon.com", "verdict": "good"}
  ]
}
```

- Exactly one of `username`, `ip` and `proxy_id` per report. A username
  without a sessid or tag, or whose exit record has expired, fails that
  report, and the response says which. A dynamic gateway row's id names no
  exit, so it needs `ip`.
- `reason` is optional and only logged.
- The API resolves the exit from stored state (the exit record, the row)
  and appends one message per report to a reports stream. It writes no
  rows and decides nothing.

### Rows

`exit_ip_reports`, one row per project, IP and site:

- Lifetime `good` and `bad` counts, first and last report times.
- `weighted_good`, `weighted_bad` and `weighted_at`: the decayed counts. On
  every update each is multiplied by `2^-(now - weighted_at) / half_life`
  before the new reports are added.
- `avoid_until`: when routing stops avoiding the IP for the site, or null.

A consumer on the workers reads the reports stream and upserts in batches,
as `SightingIngest` does into `connector_exit_ips` (`add_exit_ips`), so RDS
takes a write per batch and IP, not per report. The table gets the same
fillfactor treatment as `connector_exit_ips`, so an update rewrites its row
in place. The exit IP views join it by IP.

### Decision

The consumer decides with each upsert, so no sweep is needed. An IP is
avoided for the site while its weighted bad count is at least a threshold
and its weighted bad share is at least a minimum. The end is computed once:
`now + half_life * log2(weighted_bad / threshold)`. With a three-day
half-life and a threshold of 0.5, one bad report avoids the IP for three
days and three bad reports for about eight. A good report lowers the share
and can lift the avoid early. The half-life, threshold and share are
project settings, with defaults to settle.

### Routing copy

What routing needs now, written by the consumer whenever `avoid_until`
changes:

- Dynamic exits: a Redis hash per project and IP, site to end, the key's
  TTL the latest end. After each echo, preflight reads the IP's hash once,
  and the sites are copied into the cached verdict, so a cached request
  needs no extra read. The report message carries the reported session's
  key, so the consumer updates that verdict too. Other sessions on the IP
  see the change at their next echo.
- Static rows: on a new runtime signal from the consumer, like
  `RuntimeSignal.QUARANTINE`, the controller reads the avoids of its rows'
  IPs from Postgres into a list of sites and ends per proxy, in the feed's
  runtime section next to the quarantines (`api/feed/compiler.py`).
- After Redis comes back empty, a leader sweep rewrites the hashes from the
  rows whose `avoid_until` is still ahead.

### Avoidance

- Dynamic, which needs preflight on: a site in the IP's hash matching the
  request's host rejects the exit like a location mismatch: retry, then
  move or refuse by the setting. With reports in use, rotating requests
  have to be echoed every time instead of sampled by `exit_sample_percent`,
  at the vendor-traffic cost described in `TODO-probe-traffic-metering.md`.
- Static: the router drops rows avoided for the request's host before the
  strategy runs (`Router._excluded`), so every strategy avoids them. Under
  `refuse`, a sticky session whose row is avoided is held and refused until
  the avoid ends, as `sticky_quarantine` does for rate limits
  (`Router._is_sticky_quarantine_blocked`). Pooled slots, whose IP the
  vendor can move, are also checked at their echo, as dynamic rows are.
- A session is one IP, so an avoid for one site moves the session for
  every site.

## 4. Own outcomes

`exit_ip_outcomes`, one row per project and IP, no site: what Octoprox
itself saw through the IP, lifetime and weighted like the reports.

- Successes, vendor refusals (a 4xx to the CONNECT), tunnel failures
  (unreachable or timed out), and location mismatches from preflight.
- Only where the exit IP is known: always for static rows, for dynamic ones
  when preflight echoed the request.
- Each gateway sums its outcomes per project and IP and appends one message
  per flush to an outcomes stream, as the metrics flusher does, and a
  worker consumer upserts in batches. That is one extra write path, at
  about one row per distinct IP per flush.
- These outcomes describe the IP rather than a site's verdict. In v1 they
  are recorded, shown and feed `health_based`, and they do not avoid an IP
  on their own (see the open questions).

## 5. Named IP lists

- `ip_lists` (project, name, verdict, a site or all sites, optional expiry)
  and `ip_list_entries` (list, IP). Single IPs only.
- Uploaded through the API, as a JSON array or a text file with one IP per
  line.
- A bad list's IPs are avoided for its site while the list lives, with no
  decay. A worker writes them into the routing copy on upload and removes
  them on removal or expiry. The leader sweep that rebuilds the copy
  includes them. A good list's IPs count toward `health_based`.
- Lists never change the report rows, so removing a list takes back
  exactly what it added.

## 6. health_based and views

- `health_based` gets a third score term for static rows: the row's
  weighted good share for the request's site, from reports and good lists,
  plus its own outcomes. It is a neutral 50 when there is no record, and it
  reaches the gateways through the runtime section. Today the score is 60%
  success share and 40% latency. The strategy needs the request's host,
  which `RoutingStrategy.select` does not take today.
- Views: report and outcome counts on the exit IP views, joined by IP.
  Hourly counts per connector and site in Postgres for the Hosts page. A
  Prometheus counter per connector and verdict, with no site label.

## Open questions

- API auth: personal API tokens, or the project's proxy credentials, which
  a scraper already has?
- Defaults for the half-life, threshold and share.
- Should own outcomes avoid an IP (for every site) past some failure share,
  or only score it?
- Every eligible exit avoided for the site: 502, or 429 as for quarantine,
  since avoids end?
- The `health_based` weights, and whether the shared own outcomes replace
  the gateway-local ones it scores on today.
- Retention: the rows grow with every IP reported or seen. Delete rows
  whose weighted counts have decayed to nothing after some time, as
  `exit_ip_retention_days` does for `connector_exit_ips`?
- A size cap on lists. Each listed IP is a Redis hash in the routing copy,
  about 100 bytes, so a 1M-IP list is about 100 MB, more than the spidey
  Redis (512Mi) can take next to everything else.
- Write load on spidey's db.t4g.small: own outcomes add about one upsert
  per distinct IP per flush. Settle the flush interval against the RDS
  headroom.
- Reports for WireGuard and OpenVPN devices, which have no username: out of
  scope for v1?
