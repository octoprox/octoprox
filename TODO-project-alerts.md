# Project Alerts - Tracked Follow-ups

Deferred on 2026-11-16. The first step, a "Needs attention" card on the Overview built from state Octoprox already
stores, plus cluster problems shown to every user in plain words, is being built on its own. These two follow it.

## The gaps the first step leaves

- **No "since when".** Most problem states keep no start time: connector errors keep the latest error, quarantines
  and traffic blocks keep only their end, and traffic warnings, country coverage, healthy counts and success rate are
  computed on read and never stored. Only a proxy's status has a start (`status_changed_at`).
- **No history.** A problem that cleared before anyone looked leaves no trace. The audit log records human changes
  only.
- **Nobody is told.** The card helps only someone who opens the Overview. There are no webhooks, email or Slack
  messages, in either repo.

## 1. Alerts with history

- A leader job once a minute, after the metrics flush, checks the same conditions as the card, plus rate rules over
  the metrics tables:
  - success rate below X% over N minutes;
  - refusals or vendor 407/429 rising (`failure_metrics` by kind and upstream status);
  - spend near a budget.
- It opens an alert row with a start time when a condition appears and closes it when it clears. Rows are keyed by
  project, kind and subject (connector, country, ...), so a running condition updates its row rather than adding one.
- The Overview card then reads open alerts instead of computing on read, and keeps its shape.
- A project Alerts page lists open and resolved alerts, filterable by kind and subject. Retention runs in the hourly
  compaction, like the other history tables.
- Thresholds are per-project settings with defaults, in Project settings.

### Notes

- The worker decides and stores; the api only reads ([[roles do not overlap]]).
- The metrics tables are cheap to read here: per-minute rows on `(project, granularity, timestamp)` indexes, so a
  15-minute window is about 15 rows a project. History lags live traffic by one flush (60 s).
- Failure counts include kinds that are not request failures (`no_proxy_available` and others, `api/core/stats.py`).
  A rule has to pick its kinds on purpose.
- Flapping: a condition that clears and returns within a few minutes should reopen the same alert rather than add a
  new one.
- Fixed cut-offs flag a connector whose usual is noisy (a vendor that always refuses some requests), so people mute it;
  comparing each connector with its own usual would need fewer mutes.

## 2. Notifications

Two routes; they can coexist.

- **Webhook per project** (needs 1): a Slack-compatible JSON post on open and on resolve. Questions for when it is
  picked up: where the URL secret lives (masked in History like other secrets), retries, and whether a quiet period
  groups bursts.
- **Prometheus, no new delivery code:** more gauges on the per-project endpoint
  (`/projects/{id}/metrics/prometheus`): connectors with errors, syncs failing, countries nothing can serve, refusals
  by kind. Then documented alert rules in docs/metrics.md next to the existing "Useful alerts" list. Fits teams that
  already run Prometheus and Alertmanager.
