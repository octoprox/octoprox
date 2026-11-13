# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Traffic over time: one per-minute model that every chart and total is summed from.

A project's request rate follows a daily curve with scheduled-job bumps at the top of each hour,
quieter weekends and a little noise, and each connector carries a share of it. Failures, latency
steps and bytes follow from the connector's profile. Two incidents give the failure charts
something to show: a burst of Oxylabs 502s this morning and a short wave of Bright Data timeouts
overnight. Because every view sums the same minutes, the Overview, connector and failure numbers
agree with each other.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from functools import cache

from .common import GB, NOW, from_minute, iso, minute_index, signed, smooth
from .world import (
    CONN_BY_ID,
    KIND_MIX,
    P1,
    PROJECTS,
    STEPS,
    VENDOR_STATUSES,
    ConnSpec,
    connectors_of,
)

NOW_MIN = minute_index(NOW)
# History trails live traffic by a minute: this is the last minute saved.
LAST = NOW_MIN - 1
STEP_KEYS = ["route", "preflight", "connect", "handshake", "response"]

# (connector key, start, end, extra failure ratio, kind) of the main project's incidents today.
TODAY = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
INCIDENTS = [
    ("oxylabs", TODAY + timedelta(hours=9, minutes=41), TODAY + timedelta(hours=10, minutes=14), 0.36,
     "upstream_rejected"),
    ("brightdata", TODAY + timedelta(hours=2, minutes=4), TODAY + timedelta(hours=2, minutes=22), 0.10,
     "upstream_timeout"),
]


def shape(hour: float, trough: float) -> float:
    """The daily curve: 0 at the hour ``trough``, 1 twelve hours later."""
    return 0.5 - 0.5 * math.cos(2 * math.pi * (hour - trough) / 24)


def weekday_factor(dt: datetime) -> float:
    def factor(d: datetime) -> float:
        return {5: 0.62, 6: 0.55}.get(d.weekday(), 1.0)

    # Blended over the first two hours of a day, so weekends fade in and out.
    h = dt.hour + dt.minute / 60
    if h < 2:
        w = h / 2
        return factor(dt - timedelta(days=1)) * (1 - w) + factor(dt) * w
    return factor(dt)


@cache
def project_rate(project_id: str, m: int) -> float:
    """The project's requests in minute ``m``."""
    p = PROJECTS[project_id]
    dt = from_minute(m)
    rate = p.base + p.amp * shape(dt.hour + dt.minute / 60, p.trough) ** 1.25
    # Scheduled jobs start at the top of the hour.
    rate *= 1 + 0.07 * math.exp(-dt.minute / 6)
    rate *= weekday_factor(dt)
    rate *= (1 + 0.045 * smooth(f"rate:{project_id}", m, 23) + 0.02 * smooth(f"rate2:{project_id}", m, 5)
             + 0.008 * signed(f"w:{project_id}", m))
    return rate * 60


def load_factor(project_id: str, m: int) -> float:
    dt = from_minute(m)
    return shape(dt.hour + dt.minute / 60, PROJECTS[project_id].trough)


def incident_extra(c: ConnSpec, m: int) -> tuple[float, str | None]:
    """The extra failure ratio and its kind when an incident hits the connector in minute ``m``."""
    dt = from_minute(m)
    for key, start, end, extra, kind in INCIDENTS:
        if c.project == P1 and c.key == key and start <= dt < end:
            # Ramps in and out over a few minutes.
            span = (end - start).total_seconds() / 60
            pos = (dt - start).total_seconds() / 60
            ramp = min(1.0, pos / 4, (span - pos) / 4)
            return extra * max(0.2, ramp), kind
    return 0.0, None


@cache
def conn_minute(cid: str, m: int) -> dict:
    """One connector's minute: requests, failures (incident ones apart), latency steps and bytes."""
    c = CONN_BY_ID[cid]
    total_weight = sum(x.weight for x in connectors_of(c.project) if x.enabled)
    share = c.weight / total_weight * c.share_bias
    share *= 1 + 0.05 * smooth(f"share:{cid}", m, 37) + 0.02 * signed(f"sharew:{cid}", m)
    lf = load_factor(c.project, m)
    fail = c.fail * (1 + 0.25 * smooth(f"fail:{cid}", m, 17) + 0.35 * lf) * (1 + 0.25 * signed(f"failw:{cid}", m))
    extra, extra_kind = incident_extra(c, m)
    requests = max(0, round(project_rate(c.project, m) * share))
    base_failures = requests * fail
    extra_failures = requests * extra
    failures = min(requests, round(base_failures + extra_failures))
    lat_scale = (1 + 0.14 * lf) * (1 + 0.05 * smooth(f"lat:{cid}", m, 29) + 0.03 * signed(f"latw:{cid}", m))
    if extra_kind:
        lat_scale *= 1.18
    kb_recv = c.kb_recv * (1 + 0.06 * smooth(f"kb:{cid}", m, 41) + 0.03 * signed(f"kbw:{cid}", m))
    return {
        "requests": requests,
        "successes": requests - failures,
        "failures": failures,
        "base_failures": base_failures,
        "extra_failures": extra_failures,
        "extra_kind": extra_kind,
        "steps": {k: v * lat_scale for k, v in STEPS[c.profile].items()},
        "bytes_sent": int(requests * c.kb_sent * 1024 * (1 + 0.02 * signed(f"ks:{cid}", m))),
        "bytes_received": int(requests * kb_recv * 1024),
    }


def _snapshot(ts: datetime, parts: list[dict]) -> dict:
    """A history point summing ``parts``, its latency steps weighted by successes."""
    succ = sum(p["successes"] for p in parts)
    if succ > 0:
        steps: dict | None = {k: sum(p["steps"][k] * p["successes"] for p in parts) / succ for k in STEP_KEYS}
        avg: float | None = sum(steps.values())
        steps = {k: round(v, 1) for k, v in steps.items()}
    else:
        steps, avg = None, None
    return {
        "timestamp": iso(ts),
        "request_count": sum(p["requests"] for p in parts),
        "success_count": succ,
        "failure_count": sum(p["failures"] for p in parts),
        "avg_latency_ms": round(avg, 1) if avg is not None else None,
        "step_latency_ms": steps,
        "bytes_sent": sum(p["bytes_sent"] for p in parts),
        "bytes_received": sum(p["bytes_received"] for p in parts),
    }


# Minutes in each range and the history bucket in minutes (None: one point a minute).
RANGES = {"1h": (60, None), "6h": (360, None), "24h": (1440, None), "7d": (7 * 1440, 60), "30d": (30 * 1440, 360)}
RANGE_MINUTES = {rng: minutes for rng, (minutes, _) in RANGES.items()}


def bucket_parts(cids: list[str], start: int, end: int, stride: int) -> list[dict]:
    """Each connector's minutes [start, end) summed, sampling every ``stride`` minutes and scaling up."""
    parts = []
    for cid in cids:
        acc = {"requests": 0, "successes": 0, "failures": 0, "bytes_sent": 0, "bytes_received": 0,
               "steps": dict.fromkeys(STEP_KEYS, 0.0)}
        succ_w = 0
        for m in range(start, end, stride):
            x = conn_minute(cid, m)
            for k in ("requests", "successes", "failures", "bytes_sent", "bytes_received"):
                acc[k] += x[k] * stride
            for k in STEP_KEYS:
                acc["steps"][k] += x["steps"][k] * x["successes"]
            succ_w += x["successes"]
        if succ_w:
            acc["steps"] = {k: v / succ_w for k, v in acc["steps"].items()}
        parts.append(acc)
    return parts


def history(cids: list[str], rng: str) -> list[dict]:
    """The connectors' history over the range, as the metrics history endpoints answer it."""
    minutes, bucket = RANGES.get(rng, RANGES["24h"])
    start = LAST - minutes + 1
    if bucket is None:
        return [_snapshot(from_minute(m), [conn_minute(cid, m) for cid in cids]) for m in range(start, LAST + 1)]
    out = []
    stride = 5 if bucket <= 60 else 15
    for b in range((start // bucket) * bucket, LAST + 1, bucket):
        lo, hi = max(b, start), min(b + bucket, LAST + 1)
        out.append(_snapshot(from_minute(b), bucket_parts(cids, lo, hi, stride)))
    return out


def project_history(project_id: str, rng: str) -> list[dict]:
    return history([c.id for c in connectors_of(project_id)], rng)


def totals(cids: list[str], start: int, end: int, stride: int = 1) -> dict:
    return _snapshot(from_minute(start), bucket_parts(cids, start, end, stride))


@cache
def project_totals(project_id: str, minutes: int) -> dict:
    stride = 1 if minutes <= 1440 else 5
    return totals([c.id for c in connectors_of(project_id)], LAST - minutes + 1, LAST + 1, stride)


@cache
def conn_totals(cid: str, minutes: int) -> dict:
    stride = 1 if minutes <= 1440 else 5
    return totals([cid], LAST - minutes + 1, LAST + 1, stride)


def requests_per_minute(project_id: str) -> float:
    """Over the last five minutes, the requests Octoprox refused itself included, as the API counts them."""
    forwarded = sum(conn_minute(c.id, m)["requests"] for m in range(NOW_MIN - 5, NOW_MIN) for c in connectors_of(project_id))
    refused = round(forwarded * sum(REFUSED.values()))
    return (forwarded + refused) / 5


# --------------------------------------------------------------------------- traffic usage this billing period

PERIOD_START = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
PERIOD_END = datetime(NOW.year + (NOW.month == 12), NOW.month % 12 + 1, 1, tzinfo=UTC)


@cache
def month_bytes(cid: str) -> tuple[int, int]:
    t = totals([cid], minute_index(PERIOD_START), NOW_MIN, 10)
    return t["bytes_sent"], t["bytes_received"]


def traffic_usage(c: ConnSpec) -> dict:
    """The connector's traffic this month against its limit, and what it cost."""
    sent, received = month_bytes(c.id)
    total = sent + received
    tc = c.traffic_config
    limit = tc.get("limit_bytes")
    price = tc.get("price_per_gb")
    pct = total / limit * 100 if limit else None
    status = "ok" if pct is None or pct < tc.get("warn_percent", 80) else ("warning" if pct < 100 else "exceeded")
    return {
        "period": tc.get("period", "month"),
        "period_start": iso(PERIOD_START),
        "period_end": iso(PERIOD_END),
        "bytes_sent": sent,
        "bytes_received": received,
        "total_bytes": total,
        "limit_bytes": limit,
        "percent": round(pct, 2) if pct is not None else None,
        "action": tc.get("action", "alert"),
        "status": status,
        "blocked": False,
        "pending": False,
        "price_per_gb": price,
        "currency": tc.get("currency", "USD"),
        "cost": round(total / GB * price, 2) if price is not None else None,
        "reset_at": None,
    }


# --------------------------------------------------------------------------- failures

# Requests Octoprox refused itself, per request of the project; Overview does not count them.
REFUSED = {"no_proxy_available": 0.00031, "all_proxies_quarantined": 0.00012, "invalid_location": 0.00002}
FAILURE_BUCKETS = {"1h": 60, "6h": 300, "24h": 900, "7d": 3600, "30d": 21600}


def failure_summary(project_id: str, rng: str) -> dict:
    """Failures over the range by kind, vendor status and connector, and by kind over time."""
    minutes = RANGE_MINUTES.get(rng, 1440)
    bucket = FAILURE_BUCKETS.get(rng, 900) // 60
    start = LAST - minutes + 1
    first = (start // bucket) * bucket
    stride = 1 if minutes <= 1440 else 5
    conns = connectors_of(project_id)
    buckets = list(range(first, LAST + 1, bucket))
    kinds: dict[str, list[float]] = {}
    by_conn: dict[str | None, float] = {}
    statuses: dict[int, float] = {}
    for bi, b in enumerate(buckets):
        for m in range(max(b, start), min(b + bucket, LAST + 1), stride):
            proj_requests = 0
            for c in conns:
                x = conn_minute(c.id, m)
                proj_requests += x["requests"]
                for kind, frac in KIND_MIX[c.profile].items():
                    v = x["base_failures"] * frac * stride
                    kinds.setdefault(kind, [0.0] * len(buckets))[bi] += v
                    if kind == "upstream_rejected":
                        for st, sf in VENDOR_STATUSES.get(c.ctype, [(502, 1.0)]):
                            statuses[st] = statuses.get(st, 0) + v * sf
                if x["extra_kind"]:
                    v = x["extra_failures"] * stride
                    kinds.setdefault(x["extra_kind"], [0.0] * len(buckets))[bi] += v
                    if x["extra_kind"] == "upstream_rejected":
                        statuses[502] = statuses.get(502, 0) + v
                by_conn[c.id] = by_conn.get(c.id, 0) + x["failures"] * stride
            for kind, frac in REFUSED.items():
                v = proj_requests * frac * (1 + 0.8 * smooth(f"refused:{kind}", m, 31)) * stride
                kinds.setdefault(kind, [0.0] * len(buckets))[bi] += v
                by_conn[None] = by_conn.get(None, 0) + v
    kind_totals = {k: round(sum(v)) for k, v in kinds.items()}
    order = sorted(kind_totals, key=lambda k: -kind_totals[k])
    rejected = [{"status": st, "count": round(v)} for st, v in sorted(statuses.items(), key=lambda kv: -kv[1])]
    names = {c.id: c.name for c in conns}
    return {
        "range": rng,
        "since": iso(from_minute(start)),
        "bucket_seconds": bucket * 60,
        "total": sum(kind_totals.values()),
        "kinds": [{"kind": k, "count": kind_totals[k], "upstream_statuses": rejected if k == "upstream_rejected" else []}
                  for k in order],
        "connectors": [{"connector_id": cid, "name": names.get(cid) if cid else None, "count": round(v)}
                       for cid, v in sorted(by_conn.items(), key=lambda kv: -kv[1])],
        "series": [{"kind": k, "points": [{"timestamp": iso(from_minute(b)), "count": round(kinds[k][i])}
                                          for i, b in enumerate(buckets)]}
                   for k in order],
        "enabled": True,
    }
