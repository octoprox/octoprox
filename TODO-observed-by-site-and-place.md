# Observed by Site and Place - Tracked Follow-ups

The Traffic split card (`web/src/components/TrafficSplit.tsx`, a card on
the Overview and Connectors pages) lets a user pick a site and a
requested place: "Requests to [google.com] from [Australia]". Everything
in the card follows those pickers except the Observed column.
`observed_share` from `get_traffic_split` in `api/routes/metrics.py` is
each connector's share of all the project's requests over the picked
range, every site and every place, summed from `connector_metrics` by
`MetricsRepository.get_connector_totals_since` (`api/db/repository.py`).

Found in Nov 2026: a user read a connector at "expected 8%, observed 1%"
for google.com from Australia as Australia missing its target. The 1% was
that connector's share of all traffic, and Australia is about 11% of the
project's requests. The plain view (no site picked) has the same gap:
Expected is for requests naming no country, Observed counts every request.

Until this is built, the card says so (changed 2026-11-20). The column
header still reads "Observed", but in the wide layout a band over it (and
over Spend when connectors are priced) reads "All traffic · <range>", with
a divider setting those columns apart, and on phones each row labels the
figure "All traffic · <range>". The footer and the InfoTip say the pickers
do not narrow it.

Goal: Observed for the picked site and place, at every place level
(country, state, city), and for requests naming no place ("No location").

## What exists

- `RequestMetering.record_completed` in `api/gateway/metering.py` already
  receives each request's `location` (a `LocationTarget`) next to its
  `target_host`.
- `_deltas` there counts each request per (project, connector, host) into
  the `hosts` window. From there: the gateway's window goes to Redis
  (`MetricWindows` and `HOST_WINDOWS` in `api/db/redis.py`),
  `MetricsFlushJob` in `api/jobs/metrics.py` writes it to `host_metrics`
  (migration `api/migrations/versions/036_add_host_metrics.py`),
  `CompactionJob` folds it through `COMPACTION_TIERS` (minutes for a day,
  hours for a week, six hours for a month, then days) and deletes it past
  the project's `metrics_retention_days`, and `api/core/backup.py` carries
  it.
- Exit reputation's `_count_site_request` (same file) counts per
  (connector, site, place), but only through dynamic connectors, only for
  projects whose exit reputation records, and as counts that fade with a
  half-life. It cannot serve as observed.

## Design sketch

- A `place_metrics` table shaped like `host_metrics`: project, connector,
  place (a `LocationTarget.key`, "" for none), timestamp, granularity,
  request, success and failure counts, bytes. Same flush, compaction,
  retention and backup.
- One row per exact requested place. A country's or state's figure is its
  own key plus every key under it, read with one prefix query
  (`place = 'US' OR place LIKE 'US/%'`). A city named without a state
  (`US//austin`) counts at its country but at no state, as `place_levels`
  in `api/reputation/record.py` treats it; the state prefix `US/TX/`
  already leaves it out.
- Request counts alone are enough for the split. Bytes per place need the
  meter (`RequestMetering.meter` and `progress`, `TrafficMeter` in
  `api/core/traffic_limiter.py`) to carry the place the way it carries
  `target_host`. The card's cost comes from observed bytes, so a cost for
  the picked place needs them too.
- `get_traffic_split` already takes `site` and `place`, but ignores
  `place` when no site is given. If decision 2 picks place only, it could
  also read a place without a site.
- Optionally a per-window cap on places per project, like
  `host_metrics_max_hosts` (`fold_host_tail` in `api/core/stats.py`).

## Volume

Measured 2026-11-20 on a busy install, one row per connector and place a
bucket:

- A project naming countries only, about 45 countries over 4 connectors:
  at most ~180 rows a minute, ~260k a day at minute buckets.
- A project targeting cities, about 2,000 cities over 2-3 connectors: up
  to ~6-9M rows a day at minute buckets (2-3M with hourly bursts), ~1.7M
  a day at 5-minute buckets, ~150k a day at hourly buckets.

Compaction keeps minute rows for a day only, so the cost is the write rate
and one day of rows, not the history.

## Open decisions

1. First bucket size for this table: minute (the 1h view exact, most
   rows), 5 minutes, or hourly (the 1h view reads the last whole hour).
   The flush writes one row per key per `metrics_flush_interval` (60s), so
   a coarser bucket means flushing this window on its own cadence or adding
   into the bucket's row. 5 minutes also needs its own first tier in
   `COMPACTION_TIERS`.
2. Place only, or site plus place. Place only makes Observed "all sites
   from Australia". Site plus place makes it exactly "google.com from
   Australia", at one more row per site, keyed by `counted_site`
   (`api/reputation/site.py`), the form the card's site picker uses. Its
   cost is not measured: the rows under Volume times the number of sites
   each place's requests reach, so close to them when that is mostly one
   site, unbounded otherwise.

## When built

- The "All traffic · <range>" band over Observed, and the same label on
  phone rows, name the filter instead, for example "google.com from
  Australia · 1h".
- "No location" shows real counts for requests naming none.
- The docs that say the pickers do not narrow Observed say what it covers
  instead: the Traffic split paragraphs in `docs/routing-strategies.md`,
  the `observed_share` paragraph in `docs/api.md` (`site` and `place`
  narrow none of the observed figures) and the Traffic split bullet in
  `docs/exit-reputation.md`.
- So does the card copy in `web/src/components/TrafficSplit.tsx`: the
  InfoTip sentence that the pickers do not narrow All traffic, the footer
  text that it counts every request whatever its site, and the Observed
  cell's hover title ("every site and location").
- The Countries page (`web/src/pages/CountriesPage.tsx`), which shows no
  per-country traffic today, could read the same table.

Size: about 15 files, about 1,000 lines with tests, one migration, no new
page.
