# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Views built on the traffic model: failure examples, exit locations and the traffic split.

Also the Activity log, and the city names of the Connect page.
"""

from __future__ import annotations

import re
from datetime import timedelta

from .common import GB, NOW, ago, iso, short_id, unit, uuid_for
from .series import RANGE_MINUTES, conn_totals
from .world import (
    BRD_GATEWAY,
    OXY_GATEWAY,
    P1,
    P2,
    P3,
    PROJECTS,
    STEPS,
    build_proxies,
    connectors_of,
    user_id,
)

# --------------------------------------------------------------------------- destination hosts

# The main project's destination hosts, busiest first; the failure examples are spread over them.
TOP_HOSTS = [
    "shop.example.com", "catalog.example.net", "api.example.org", "search.example.com", "deals.example.org",
    "m.shop.example.com", "images.example.net", "reviews.example.com", "marketplace.example.net",
    "inventory.example.org", "compare.example.com", "store.example.org", "prices.example.net",
    "cdn.catalog.example.net", "outlet.example.com", "offers.example.org", "groceries.example.com",
    "electronics.example.net", "fashion.example.org", "homeware.example.com", "toys.example.net",
    "books.example.org", "sports.example.com", "beauty.example.net", "garden.example.org",
]


# --------------------------------------------------------------------------- failure examples

def _steps_until(profile_steps: dict, failed_at: str, failed_ms: float) -> dict:
    """The latency steps of a request that failed at ``failed_at`` after ``failed_ms`` there."""
    out = {}
    for k in ["route", "preflight", "connect", "handshake", "response"]:
        if k == failed_at:
            out[k] = round(failed_ms, 1)
            break
        out[k] = round(profile_steps[k], 1)
    return out


def failure_records(project_id: str) -> list[dict]:
    """The failure log's kept examples, newest first: busy recurring ones, the two incidents and a quiet tail."""
    if project_id != P1:
        return []
    by_key = {c.key: c for c in connectors_of(P1)}
    today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    recs: list[dict] = []

    def add(kind: str, status: int, conn_key: str | None, target: str | None, message: str, *, method: str = "CONNECT",
            error_type: str | None = None, proxy: str | None = None, upstream_status: int | None = None,
            upstream_message: str | None = None, step: str = "connect", step_ms: float | None = None, count: int = 1,
            last_min: int = 5, span_min: int = 60, session: str | None = None, location: str | None = None,
            peer: str | None = None) -> None:
        c = by_key.get(conn_key) if conn_key else None
        steps = None
        latency = None
        if c is not None:
            profile = STEPS[c.profile]
            steps = _steps_until(profile, step, step_ms if step_ms is not None else profile.get(step, 100) * 1.4)
            latency = round(sum(steps.values()), 1)
        last = NOW - timedelta(minutes=last_min, seconds=int(unit("fs", len(recs)) * 50))
        first = last - timedelta(minutes=span_min) if count > 1 else last
        fp = short_id(f"{kind}:{target}:{message}:{proxy}:{len(recs)}", 16)
        recs.append({
            "id": f"{kind}:{fp}", "kind": kind, "status": status, "message": message, "error_type": error_type,
            "method": method if not peer else None, "target": target, "session_id": session, "location": location,
            "peer_id": uuid_for(f"peer:{peer}") if peer else None, "peer_name": peer,
            "proxy_id": uuid_for(f"proxy:{c.id}:{len(recs) % 7}") if c else None,
            "connector_id": c.id if c else None, "connector_name": c.name if c else None,
            "proxy": proxy, "upstream_status": upstream_status, "upstream_message": upstream_message,
            "latency_ms": latency, "step": step if c else None, "steps": steps, "count": count,
            "first_at": iso(first), "last_at": iso(last),
        })

    timeout_msg = "No answer from the upstream proxy within 30 seconds"
    brd = f"{BRD_GATEWAY[0]}:{BRD_GATEWAY[1]}"
    oxy = f"{OXY_GATEWAY[0]}:{OXY_GATEWAY[1]}"
    # Recurring, busy examples first (they keep moving up as they recur).
    add("upstream_timeout", 504, "brightdata", "search.example.com:443", timeout_msg, error_type="TimeoutError",
        proxy=brd, step="handshake", step_ms=30000, count=4211, last_min=0, span_min=1380, location="US")
    add("upstream_rejected", 502, "brightdata", "search.example.com:443",
        "Upstream proxy refused the tunnel: 429 Too Many Requests", error_type="ProxyError", proxy=brd,
        upstream_status=429, upstream_message="Too many concurrent requests for zone residential_pricing; retry later",
        step="connect", step_ms=212, count=1903, last_min=1, span_min=1290)
    add("upstream_timeout", 504, "webshare", "catalog.example.net:443", timeout_msg, error_type="TimeoutError",
        proxy="198.51.100.37:6117", step="handshake", step_ms=30000, count=1288, last_min=1, span_min=1400)
    add("upstream_error", 502, "webshare", "shop.example.com:443", "Connection reset by peer",
        error_type="ConnectionResetError", proxy="198.51.100.52:6132", step="handshake", step_ms=88, count=1045,
        last_min=2, span_min=1410)
    add("upstream_timeout", 504, "oxylabs", "marketplace.example.net:443", timeout_msg, error_type="TimeoutError",
        proxy=oxy, step="handshake", step_ms=30000, count=988, last_min=2, span_min=1200, session="ox-de-07",
        location="DE")
    add("upstream_rejected", 502, "brightdata", "deals.example.org:443",
        "Upstream proxy refused the tunnel: 502 Bad Gateway", error_type="ProxyError", proxy=brd, upstream_status=502,
        upstream_message="No peer available in the requested city right now (city=lyon)", step="connect", step_ms=640,
        count=731, last_min=3, span_min=980, location="FR, city lyon")
    add("upstream_unreachable", 502, "webshare", "shop.example.com:443",
        "[Errno 111] Connect call failed ('198.51.100.88', 6158)", error_type="ConnectionRefusedError",
        proxy="198.51.100.88:6158", step="connect", step_ms=3, count=642, last_min=3, span_min=600)
    add("upstream_timeout", 504, "aws", "shop.example.com:443", timeout_msg, error_type="TimeoutError",
        proxy="203.0.113.15:3128", step="response", step_ms=30000, method="GET", count=588, last_min=4, span_min=1300)
    add("client_closed", 400, "aws", "api.example.org:443", "Client closed the connection mid-body",
        proxy="203.0.113.19:3128", step="response", step_ms=640, method="POST", count=402, last_min=4, span_min=1100)
    add("upstream_error", 502, "gcp", "catalog.example.net:443", "Server disconnected without sending a response",
        error_type="ServerDisconnectedError", proxy="198.51.100.3:3128", step="response", step_ms=410, method="GET",
        count=377, last_min=5, span_min=1000)
    add("upstream_rejected", 502, "oxylabs", "deals.example.org:443", "Upstream proxy refused the tunnel: 403 Forbidden",
        error_type="ProxyError", proxy=oxy, upstream_status=403,
        upstream_message="Access to this target is restricted for your account; contact your account manager",
        step="connect", step_ms=180, count=356, last_min=6, span_min=900)
    add("preflight_mismatch", 502, "oxylabs", "shop.example.com:443",
        "Exit country mismatch: requested DE, observed AT (203.0.113.77)", step="preflight", step_ms=1730, count=212,
        last_min=7, span_min=1400, location="DE")
    add("upstream_timeout", 504, "gcp", "search.example.com:443", timeout_msg, error_type="TimeoutError",
        proxy="198.51.100.6:3128", step="handshake", step_ms=30000, count=198, last_min=9, span_min=700)
    add("no_proxy_available", 502, None, "search.example.com:443", "No upstream proxy available for KR and this domain",
        count=164, last_min=11, span_min=1300, location="KR")
    add("preflight_mismatch", 502, "brightdata", "reviews.example.com:443",
        "Exit city mismatch: requested US, state NY, city new_york, observed US, state NJ, city newark "
        "(198.51.100.201)", step="preflight", step_ms=1910, count=141, last_min=12, span_min=1250,
        location="US, state NY, city new_york", session="nyc-basket-31")
    add("all_proxies_quarantined", 429, None, "catalog.example.net:443",
        "All proxies are temporarily rate-limited. Retry later.", count=96, last_min=14, span_min=300)
    add("upstream_error", 502, "oxylabs", "search.example.com:443", "Connection lost during the TLS handshake",
        error_type="SSLError", proxy=oxy, step="handshake", step_ms=2210, count=88, last_min=16, span_min=900)
    add("upstream_timeout", 504, "webshare", "images.example.net:443", timeout_msg, error_type="TimeoutError",
        proxy="203.0.113.140:6141", step="response", step_ms=30000, method="GET", count=73, last_min=18, span_min=800)
    add("upstream_error", 502, "webshare", "m.shop.example.com:443", "Connection reset by peer",
        error_type="ConnectionResetError", proxy="198.51.100.61:6140", step="handshake", step_ms=95, count=41,
        last_min=21, span_min=500, peer="QA iPhone 15")
    add("invalid_location", 400, None, None, "A state or city needs a country: add -cc-<country> to the username",
        count=12, last_min=34, span_min=240)
    add("upstream_unreachable", 502, "aws", "shop.example.com:443", "[Errno 113] Connect call failed ('203.0.113.16', 3128)",
        error_type="OSError", proxy="203.0.113.16:3128", step="connect", step_ms=3100, count=31, last_min=52, span_min=14)
    add("client_closed", 400, "brightdata", "m.shop.example.com:443", "Client closed the connection mid-body",
        proxy=brd, step="response", step_ms=1880, method="POST", count=29, last_min=66, span_min=600)
    # The morning's Oxylabs incident: one example that recurred a few thousand times, quiet since.
    add("upstream_rejected", 502, "oxylabs", "catalog.example.net:443", "Upstream proxy refused the tunnel: 502 Bad Gateway",
        error_type="ProxyError", proxy=oxy, upstream_status=502,
        upstream_message="upstream connect error or disconnect/reset before headers. reset reason: connection timeout",
        step="connect", step_ms=5020, count=5874,
        last_min=int((NOW - (today + timedelta(hours=10, minutes=13))).total_seconds() // 60), span_min=32)
    # The overnight wave of Bright Data timeouts.
    add("upstream_timeout", 504, "brightdata", "shop.example.com:443", timeout_msg, error_type="TimeoutError", proxy=brd,
        step="connect", step_ms=30000, count=1534,
        last_min=int((NOW - (today + timedelta(hours=2, minutes=21))).total_seconds() // 60), span_min=17,
        location="GB")
    # A longer tail of quieter examples across hosts and connectors.
    tail_hosts = TOP_HOSTS[4:25]
    kinds = [("upstream_timeout", 504, "TimeoutError", timeout_msg, "handshake", 30000),
             ("upstream_error", 502, "ConnectionResetError", "Connection reset by peer", "handshake", 120),
             ("upstream_unreachable", 502, "ConnectionRefusedError", None, "connect", 4)]
    conn_cycle = ["webshare", "aws", "gcp", "oxylabs", "brightdata"]
    for i in range(60):
        kind, status, error_type, msg, step, ms = kinds[i % 3]
        ck = conn_cycle[i % 5]
        # A vendor gateway always takes the connection: it times out instead.
        if kind == "upstream_unreachable" and ck in ("brightdata", "oxylabs"):
            kind, status, error_type, msg, step, ms = kinds[0]
        host = tail_hosts[(i * 7) % len(tail_hosts)]
        if ck == "aws":
            proxy = f"203.0.113.{11 + i % 12}:3128"
        elif ck == "gcp":
            proxy = f"198.51.100.{1 + i % 8}:3128"
        elif ck == "webshare":
            proxy = f"198.51.100.{30 + (i * 3) % 90}:{6100 + i % 99}"
        else:
            proxy = brd if ck == "brightdata" else oxy
        if msg is None:
            ip, port = proxy.split(":")
            msg = f"[Errno 111] Connect call failed ('{ip}', {port})"
        add(kind, status, ck, f"{host}:443", msg, error_type=error_type, proxy=proxy, step=step, step_ms=ms,
            count=max(1, int(60 * (0.93 ** i) * (0.5 + unit("tc", i)))), last_min=25 + i * 19, span_min=60 + i * 7)
    recs.sort(key=lambda r: r["last_at"], reverse=True)
    return recs


# --------------------------------------------------------------------------- exit locations

def _level(claimed: int, confirmed: int, contradicted: int) -> dict:
    judged = confirmed + contradicted
    return {"claimed": claimed, "confirmed": confirmed, "contradicted": contradicted,
            "open": max(0, claimed - confirmed - contradicted),
            "accuracy": round(confirmed / judged, 4) if judged else None}


def _scale(days: int) -> float:
    """The share of the 30-day counts a window of ``days`` holds."""
    return {1: 0.075, 7: 0.36, 30: 1.0, 90: 2.3}.get(days, days / 30)


def accuracy_rows(project_id: str | None, connector_id: str | None, days: int) -> list[dict]:
    """How often each connector's exits were where the vendor claimed, by country, state and city."""
    if project_id not in (None, P1) and connector_id is None:
        return []
    by_key = {c.key: c for c in connectors_of(P1)}
    k = _scale(days)
    rows = []

    def count(key: str, n: int) -> int:
        # Sampled exits grow with the window; a fixed proxy is the same exit however long the window is.
        return max(0, round(n * k)) if key in ("brightdata", "oxylabs") else n

    def row(key: str, exits: int, country: tuple, state: tuple, city: tuple, breakdown: list[tuple],
            coverage: dict | None = None) -> None:
        c = by_key[key]
        rows.append({
            "connector_id": c.id, "connector_name": c.name, "project_id": P1, "exits": count(key, exits),
            "country": _level(*(count(key, x) for x in country)), "state": _level(*(count(key, x) for x in state)),
            "city": _level(*(count(key, x) for x in city)),
            "breakdown": [{"level": lv, "claimed": a, "observed": b, "exits": max(1, count(key, n))}
                          for lv, a, b, n in breakdown],
            "coverage": coverage,
        })

    row("brightdata", 24816, (24816, 21904, 489), (3120, 2701, 297), (1844, 1402, 318),
        [("country", "US", "CA", 141), ("country", "GB", "IE", 96), ("country", "DE", "AT", 83),
         ("country", "FR", "BE", 57), ("country", "ES", "PT", 39), ("country", "NL", "BE", 33),
         ("country", "SE", "NO", 22), ("country", "MX", "US", 18),
         ("state", "NY", "NJ", 118), ("state", "CA", "NV", 64), ("state", "IL", "IN", 47), ("state", "TX", "OK", 39),
         ("state", "FL", "GA", 29),
         ("city", "new_york", "newark", 92), ("city", "los_angeles", "long_beach", 61),
         ("city", "chicago", "evanston", 44), ("city", "london", "croydon", 38),
         ("city", "paris", "boulogne_billancourt", 27), ("city", "berlin", "potsdam", 21),
         ("city", "lyon", "villeurbanne", 18), ("city", "miami", "fort_lauderdale", 17)],
        coverage={"dynamic": True, "preflight_on": True, "sampled_percent": 5})
    row("oxylabs", 6412, (6412, 5880, 66), (0, 0, 0), (640, 553, 71),
        [("country", "DE", "AT", 31), ("country", "FR", "CH", 19), ("country", "GB", "IE", 16),
         ("city", "london", "watford", 24), ("city", "munich", "augsburg", 18), ("city", "paris", "versailles", 15),
         ("city", "berlin", "potsdam", 14)])
    row("webshare", 100, (100, 96, 3), (0, 0, 0), (0, 0, 0), [("country", "NL", "DE", 2), ("country", "CA", "US", 1)])
    row("gcp", 236, (236, 235, 1), (0, 0, 0), (0, 0, 0), [("country", "DE", "NL", 1)])
    row("aws", 418, (418, 418, 0), (0, 0, 0), (0, 0, 0), [])
    if connector_id:
        rows = [r for r in rows if r["connector_id"] == connector_id]
    return rows


def exits_rows(project_id: str | None, connector_id: str | None, days: int) -> list[dict]:
    """Unique and reused exit IPs per connector over the window."""
    if project_id not in (None, P1) and connector_id is None:
        return []
    by_key = {c.key: c for c in connectors_of(P1)}
    k = _scale(days)
    # unique in 30 days, unique ever, reused, most sightings of one IP, minutes since the last sighting
    spec = {
        "aws": (418, 1206, 418, 22, 3),
        "gcp": (236, 702, 236, 31, 2),
        "brightdata": (24816, 61240, 2171, 6, 9),
        "oxylabs": (6412, 15988, 1290, 14, 4),
        "webshare": (100, 100, 100, 41810, 0),
    }
    rows = []
    for key, (window, ever, reused, max_sightings, last_min) in spec.items():
        c = by_key[key]
        # A static list is the same IPs whatever the window; the others rotate through new ones.
        in_window = window if key == "webshare" else round(window * k)
        rows.append({
            "connector_id": c.id, "connector_name": c.name, "project_id": P1,
            "coverage": {"dynamic": True, "preflight_on": True, "sampled_percent": 5} if key == "brightdata" else None,
            "unique_total": ever, "unique_in_window": in_window, "sightings": in_window * 37, "reused": reused,
            "max_sightings": int(max_sightings * k) if key == "webshare" else max_sightings,
            "last_seen": ago(minutes=last_min),
        })
    if connector_id:
        rows = [r for r in rows if r["connector_id"] == connector_id]
    return rows


# --------------------------------------------------------------------------- traffic split

def traffic_split(project_id: str, rng: str) -> dict:
    """Each connector's expected and observed share of the requests, its eligible proxies and its cost."""
    conns = connectors_of(project_id)
    minutes = RANGE_MINUTES.get(rng, 1440)
    proxies = build_proxies(project_id)
    total_weight = sum(c.weight for c in conns if c.enabled)
    observed = {c.id: conn_totals(c.id, minutes) for c in conns}
    obs_req = sum(o["request_count"] for o in observed.values())
    obs_bytes = sum(o["bytes_sent"] + o["bytes_received"] for o in observed.values())
    rows = []
    for c in conns:
        mine = [p for p in proxies if p["connector_id"] == c.id]
        # Untargeted traffic skips exits in a country the connector excludes, and its exits in a country it reserves.
        skipped = set(c.routing_extra.get("excluded_countries", [])) | set(c.routing_extra.get("reserved_countries", []))
        eligible = sum(1 for p in mine if p["status"] == "healthy" and not p["quarantined"] and p["country"] not in skipped)
        o = observed[c.id]
        b = o["bytes_sent"] + o["bytes_received"]
        price = c.traffic_config.get("price_per_gb")
        rows.append({
            "connector_id": c.id, "name": c.name, "credential_type": c.ctype, "enabled": c.enabled, "weight": c.weight,
            "dynamic": bool(c.target and c.target.get("dynamic")), "total_proxies": len(mine),
            "eligible_proxies": eligible, "expected_share": round(c.weight / total_weight * 100, 2),
            "excluded_reason": None, "observed_requests": o["request_count"],
            "observed_share": round(o["request_count"] / max(1, obs_req) * 100, 2), "observed_bytes": b,
            "cost": round(b / GB * price, 2) if price is not None else None,
            "currency": c.traffic_config.get("currency", "USD") if price is not None else None,
        })
    return {"strategy": PROJECTS[project_id].strategy, "range": rng, "total_weight": total_weight,
            "observed_requests": obs_req, "observed_bytes": obs_bytes, "connectors": rows}


# --------------------------------------------------------------------------- activity (the Enterprise audit log)

UA_MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 "
          "Safari/537.36")
UA_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0"
UA_CLI = "octoprox-ci/1.4 python-httpx/0.28.1"
# What the log holds in all, of which activity_entries() is the newest page.
ACTIVITY_TOTAL = 1284


def activity_entries() -> list[dict]:
    """The newest audit log entries: sign-ins, API tokens and changes across the projects, newest first."""
    conn = {c.key: c for c in connectors_of(P1)}
    out: list[dict] = []

    def e(minutes: int, action: str, etype: str, eid: str, name: str, actor: str | None, *, project: str | None = None,
          changes: dict | None = None, details: dict | None = None) -> None:
        out.append({
            "id": uuid_for(f"audit:{len(out)}"), "created_at": ago(minutes=minutes), "action": action,
            "entity_type": etype, "entity_id": eid, "entity_name": name, "project_id": project,
            "actor_id": user_id(actor) if actor else None, "actor": actor or "", "request_id": short_id(f"req:{len(out)}"),
            "changes": changes or {}, "details": details or {},
        })

    tok = {"token_id": uuid_for("token:ci-deploy"), "token_name": "ci-deploy", "token_prefix": "opx_7Hk2"}
    e(4, "signed_in", "user", user_id("maria"), "maria", "maria",
      details={"method": "sso", "provider": "google", "client_ip": "203.0.113.24", "user_agent": UA_MAC})
    e(12, "updated", "connector", conn["brightdata"].id, conn["brightdata"].name, "devon", project=P1,
      changes={"traffic_config.limit_bytes": [120 * GB, 160 * GB]})
    e(19, "token_first_used", "user", user_id("devon"), "devon", "devon",
      details={**tok, "client_ip": "198.51.100.17", "user_agent": UA_CLI})
    e(21, "token_issued", "user", user_id("devon"), "devon", "devon", details={**tok, "expires_at": "2027-02-10T00:00:00Z"})
    e(23, "signed_in", "user", user_id("devon"), "devon", "devon",
      details={"method": "password", "client_ip": "198.51.100.17", "user_agent": UA_WIN})
    e(47, "updated", "connector", conn["webshare"].id, conn["webshare"].name, "devon", project=P1,
      changes={"routing_config.weight": [6, 7]}, details={"auth_token": "ci-deploy", "auth_token_id": tok["token_id"]})
    e(63, "sign_in_failed", "user", "", "admin", None,
      details={"method": "password", "reason": "Unknown username", "client_ip": "192.0.2.201", "user_agent": UA_WIN})
    e(88, "updated", "wireguard_device", uuid_for("peer:Smart TV test rig"), "Smart TV test rig", "priya", project=P1,
      changes={"country": ["US", "GB"], "session_id": [None, "tv-rig"]})
    e(141, "updated", "project", P2, "Market research", "priya", project=P2,
      changes={"routing_strategy": ["random", "round_robin"]})
    e(190, "signed_in", "user", user_id("priya"), "priya", "priya",
      details={"method": "sso", "provider": "github", "client_ip": "203.0.113.58", "user_agent": UA_MAC})
    e(236, "traffic_reset", "connector", uuid_for(f"connector:{P3}:oxylabs"), "Oxylabs residential", "maria",
      project=P3, details={"reason": "Vendor top-up"})
    e(301, "updated", "geo_settings", "1", "IP attribution settings", "maria",
      changes={"default_city_radius_km": [25, 40]})
    e(356, "created", "credential", uuid_for(f"credential:{P3}:IPRoyal fares"), "IPRoyal fares", "priya", project=P3,
      changes={"name": [None, "IPRoyal fares"], "type": [None, "iproyal"], "config.password": [None, "********"]})
    e(358, "created", "connector", uuid_for(f"connector:{P3}:iproyal"), "IPRoyal residential", "priya", project=P3,
      changes={"name": [None, "IPRoyal residential"], "routing_config.weight": [None, 1],
               "traffic_config.limit_bytes": [None, 120 * GB]})
    e(412, "invited", "user", user_id("sam"), "sam", "maria", changes={"username": [None, "sam"], "role": [None, "viewer"]},
      details={"via": "invite"})
    e(530, "updated", "user", user_id("devon"), "devon", "maria", changes={"role": ["viewer", "editor"]})
    e(612, "token_revoked", "user", user_id("devon"), "devon", "devon",
      details={"token_id": uuid_for("token:old"), "token_name": "ci-deploy-2025", "token_prefix": "opx_Qm81"})
    e(705, "updated", "connector", conn["oxylabs"].id, conn["oxylabs"].name, "devon", project=P1,
      changes={"config.country_code": [["US", "GB", "DE"], ["US", "GB", "DE", "FR"]]})
    e(788, "signed_in", "user", user_id("sam"), "sam", "sam",
      details={"method": "password", "client_ip": "203.0.113.90", "user_agent": UA_WIN})
    e(902, "rotated", "wireguard_device", uuid_for("peer:QA Pixel 8"), "QA Pixel 8", "priya", project=P1,
      changes={"public_key": ["********", "********"]})
    e(1040, "updated", "project", P1, "Price monitoring", "maria", project=P1, changes={"location_preflight": [False, True]})
    e(1180, "deleted", "connector", uuid_for("connector:old-decodo"), "Decodo mobile trial", "maria", project=P1,
      changes={"name": ["Decodo mobile trial", None]})
    e(1330, "sign_in_failed", "user", user_id("devon"), "devon", "devon",
      details={"method": "password", "reason": "Wrong password", "client_ip": "198.51.100.17", "user_agent": UA_WIN})
    e(1460, "signed_in", "user", user_id("maria"), "maria", "maria",
      details={"method": "sso", "provider": "google", "client_ip": "203.0.113.24", "user_agent": UA_MAC})
    e(1530, "updated", "system_settings", "1", "System settings", "maria", changes={"audit_retention_days": [180, 365]})
    e(1702, "created", "proxy", uuid_for("proxy:manual"), "203.0.113.250:8080", "devon", project=P2,
      changes={"host": [None, "203.0.113.250"], "port": [None, 8080]})
    e(1890, "updated", "connector", uuid_for(f"connector:{P2}:decodo"), "Decodo ISP pool", "priya", project=P2,
      changes={"routing_config.reserved_countries": [None, ["DE"]]})
    e(2105, "imported", "geo_database", uuid_for("geodb:city"), "City database (weekly)", "maria", details={"via": "url"})
    e(2390, "signed_in", "user", user_id("devon"), "devon", "devon",
      details={"method": "password", "client_ip": "198.51.100.17", "user_agent": UA_WIN})
    e(2660, "updated", "sign_in_provider", "github", "GitHub", "maria", changes={"enabled": [False, True]})
    return out


# --------------------------------------------------------------------------- connect

CITY_ALIASES = {"nyc": "new_york"}


def slug(text: str) -> str:
    """A city name as the proxy username spells it: lower case, words joined by underscores."""
    s = re.sub(r"[\s-]+", "_", text.strip().lower())
    return CITY_ALIASES.get(s, s)
