# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The fake Octoprox API: ``handle()`` answers a request to /api/v1 with the demo data.

Anything the screens do not need is answered 404, and the driver reports it, so a screen that
starts calling a new endpoint does not go unnoticed.
"""

from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import parse_qs

from . import analytics, series
from .common import NOW, VERSION, ago, iso, key_for, short_id, uuid_for
from .world import (
    CONN_BY_ID,
    CREDENTIAL_CONFIG,
    ME,
    OPENVPN_DEVICES,
    P1,
    P1_PLACES,
    PROJECTS,
    USERS,
    WIREGUARD_DEVICES,
    ConnSpec,
    build_proxies,
    connector_options,
    connectors_of,
    country_options,
    credentials_of,
    pool_counts,
    providers_catalog,
    user_id,
)


class NotHandledError(Exception):
    """The fake API has no answer for the request."""


# --------------------------------------------------------------------------- builders

def project_json(pid: str) -> dict:
    p = PROJECTS[pid]
    pc = pool_counts(pid)
    return {
        "id": p.id, "name": p.name, "description": p.description, "username": p.username, "password": p.password,
        "routing_strategy": p.strategy, "connection_timeout": 30, "max_retries": 2, "tls_mitm_mode": "off",
        "tls_mitm_engine": None, "tls_mitm_browser": None, "metrics_retention_days": 30,
        "location_contradicted": "evidence", "location_unknown": "vendor", "location_preflight": True,
        "location_session_move": False, "location_conflict_rule": None, "location_city_radius_km": None,
        "exit_headers": True, "observation_log": True, "observation_retention_days": None, "pending_deletion": False,
        "created_at": ago(days=p.created_days_ago), "updated_at": ago(days=1, hours=7),
        "credential_count": len(credentials_of(pid)), "connector_count": len(connectors_of(pid)),
        "proxy_count": pc["total"], "healthy_proxy_count": pc["healthy"],
    }


def connector_json(c: ConnSpec) -> dict:
    return {
        "id": c.id, "name": c.name, "credential_id": c.cred_id, "credential_name": c.cred_name,
        "credential_type": c.ctype, "project_id": c.project, "config": c.config,
        "routing_config": {"weight": c.weight, **c.routing_extra}, "rate_limit_config": c.rate_limit_config,
        "traffic_config": c.traffic_config, "health_check_config": {}, "health_check_interval": 60,
        "default_health_check_interval": 60, "traffic_reset_at": None, "traffic_usage": series.traffic_usage(c),
        "enabled": c.enabled, "proxy_count": sum(1 for p in build_proxies(c.project) if p["connector_id"] == c.id),
        "target": c.target, "last_error": None, "last_error_at": None, "consecutive_errors": 0,
        "created_at": ago(days=c.created_days_ago), "updated_at": ago(minutes=c.updated_minutes_ago),
    }


def pool_json(pid: str) -> dict:
    pc = pool_counts(pid)
    t = series.project_totals(pid, 1440)
    # The pool's counters run from when its proxies were added, a little over nine days of traffic.
    scale = 9.4
    req = int(t["request_count"] * scale)
    succ = int(t["success_count"] * scale)
    return {
        "pool": {
            "total_proxies": pc["total"], "healthy_proxies": pc["healthy"], "unhealthy_proxies": pc["unhealthy"],
            "quarantined_proxies": pc["quarantined"], "draining_proxies": pc["draining"],
            "terminating_proxies": pc["terminating"], "total_requests": req, "total_successes": succ,
            "total_failures": req - succ, "overall_success_rate": round(succ / req * 100, 2),
            "avg_latency_ms": t["avg_latency_ms"], "total_bytes_sent": int(t["bytes_sent"] * scale),
            "total_bytes_received": int(t["bytes_received"] * scale),
        },
        "strategy": {"current_strategy": PROJECTS[pid].strategy,
                     "available_strategies": ["round_robin", "random", "least_used", "sticky", "health_based"]},
    }


def scaling_json(pid: str) -> dict:
    rpm = series.requests_per_minute(pid)
    pc = pool_counts(pid)
    cloud = [c for c in connectors_of(pid) if c.ctype in ("aws", "gcp", "azure")]
    current = sum((c.target or {}).get("total") or 0 for c in cloud)
    return {
        "demand_level": "HIGH" if rpm > 3000 else "MEDIUM" if rpm > 900 else "LOW",
        "requests_per_minute": round(rpm, 1), "rate_per_proxy": round(rpm / max(1, pc["total"]), 1),
        "current_instances": current, "healthy_instances": max(0, current - pc["draining"] - pc["terminating"]),
        "min_instances": sum(int(c.config.get("min_proxies", 2)) for c in cloud),
        "max_instances": sum(int(c.config.get("max_proxies", 10)) for c in cloud),
        "draining_instances": pc["draining"], "terminating_instances": pc["terminating"],
    }


def connect_json(pid: str, q: dict[str, str]) -> dict:
    """The Connect page's proxy username for the place and session asked for, and where it would go."""
    p = PROJECTS[pid]
    country = (q.get("country") or "").strip().upper() or None
    state = (q.get("state") or "").strip().upper() or None
    city = analytics.slug(q["city"]) if q.get("city") else None
    sessid = (q.get("sessid") or "").strip() or None
    parts = [p.username]
    if country:
        parts.append(f"-cc-{country.lower()}")
        if state:
            parts.append(f"-st-{state.lower()}")
        if city:
            parts.append(f"-city-{city}")
    if sessid:
        parts.append(f"-sessid-{sessid}")
    conns = {c.key: c for c in connectors_of(pid)}
    routes = []
    countries = ["US", "GB"]
    places = []
    if pid == P1:
        # The connectors that could take the place: the dynamic one anywhere it serves, the others with
        # their fixed exits counted where those are.
        eligible = []
        for key, c in conns.items():
            if key == "brightdata":
                if country is None or country in c.target["countries"]:
                    eligible.append((c, True, 1))
                continue
            n = sum(count for cc, st, ci, count in P1_PLACES.get(key, [])
                    if (country is None or cc == country) and (state is None or st == state)
                    and (city is None or ci == city))
            if n:
                eligible.append((c, False, n))
        total_w = sum(c.weight for c, _, _ in eligible) or 1
        routes = [{"connector_id": c.id, "connector_name": c.name, "dynamic": dynamic, "on_demand": False,
                   "proxies": n, "share": round(c.weight / total_w, 4)} for c, dynamic, n in eligible]
        countries = sorted({cc for spots in P1_PLACES.values() for cc, *_ in spots}
                           | set(conns["brightdata"].target["countries"]))
        places = [{"country": cc, "state": st, "city": ci, "proxies": count}
                  for key in ("webshare", "oxylabs", "aws", "gcp") for cc, st, ci, count in P1_PLACES.get(key, [])]
    return {
        "endpoint": {"scheme": "http", "host": "proxy.example.com", "port": 8080},
        "username": "".join(parts), "sessid": sessid,
        "location": {"country": country, "state": state, "city": city} if country else None,
        "error": None, "routes": routes, "countries": countries, "any_country": False, "places": places,
    }


def tunnel_metrics(requests: int, mb: int) -> dict:
    """What the pool relayed for a device, from its requests and MB."""
    successes = int(requests * 0.985)
    return {"request_count": requests, "success_count": successes, "failure_count": requests - successes,
            "avg_latency_ms": 540.0, "bytes_sent": int(mb * 1e6 * 0.06), "bytes_received": int(mb * 1e6 * 0.94),
            "connections_by_address": int(requests * 0.02), "encrypted_dns_blocked": int(requests * 0.004)}


def wireguard_peers(pid: str) -> dict:
    peers = []
    if pid == P1:
        for name, addr, online, minutes, endpoint, cc, st, ci, sess, req, mb, days in WIREGUARD_DEVICES:
            peers.append({
                "id": uuid_for(f"peer:{name}"), "project_id": pid, "name": name, "public_key": key_for(f"wg:{name}"),
                "has_private_key": True, "has_preshared_key": True, "address": addr, "enabled": True,
                "session_id": sess, "country": cc, "state": st, "city": ci,
                "created_at": ago(days=days), "updated_at": ago(days=max(1, days // 3)),
                "status": {"online": online, "last_handshake_at": ago(minutes=minutes, seconds=17), "endpoint": endpoint,
                           "live": online, "rx_bytes": int(mb * 1e6 * 0.07), "tx_bytes": int(mb * 1e6 * 0.95)},
                "metrics": tunnel_metrics(req, mb),
            })
    return {"total": len(peers), "peers": peers, "server_configured": True, "server_public_key": key_for("wg:server")}


def openvpn_peers(pid: str) -> dict:
    peers = []
    if pid == P1:
        for name, addr, online, minutes, endpoint, cc, st, ci, sess, req, mb, days in OPENVPN_DEVICES:
            peers.append({
                "id": uuid_for(f"peer:{name}"), "project_id": pid, "name": name,
                "serial": short_id(f"ser{name}", 16).upper(), "certificate_expires_at": iso(NOW + timedelta(days=700)),
                "address": addr, "enabled": True, "session_id": sess, "country": cc, "state": st, "city": ci,
                "created_at": ago(days=days), "updated_at": ago(days=4),
                "status": {"online": online, "connected_since": ago(minutes=minutes), "last_seen_at": ago(minutes=minutes),
                           "endpoint": endpoint, "live": online, "rx_bytes": int(mb * 1e6 * 0.06),
                           "tx_bytes": int(mb * 1e6 * 0.93)},
                "metrics": tunnel_metrics(req, mb),
            })
    return {"total": len(peers), "peers": peers, "server_configured": True, "ca_fingerprint": f"SHA256:{short_id('ca')}"}


def zones_options() -> dict:
    """The Bright Data zones the connector form offers."""
    return {"options": [
        {"value": "residential_pricing", "label": "residential_pricing", "description": "residential (res_rotating)",
         "extra": {"type": "res_rotating", "proxy_type": "residential", "password": "c8v2m4q9x1t7"}},
        {"value": "isp_us_static", "label": "isp_us_static", "description": "isp (res_static) - 50 IPs",
         "extra": {"type": "res_static", "proxy_type": "isp", "password": "k1z8w3p0n6r2", "total_ips": 50}},
        {"value": "datacenter_eu", "label": "datacenter_eu", "description": "datacenter (dc) - 200 IPs",
         "extra": {"type": "dc", "proxy_type": "datacenter", "password": "t5b9q2m7x4c1", "total_ips": 200}},
    ]}


# --------------------------------------------------------------------------- routing

def handle(method: str, path: str, query: str) -> tuple[int, object]:
    """The status and JSON body answering ``method`` on ``path`` (after /api/v1) with ``query``."""
    q = {k: v[-1] for k, v in parse_qs(query).items()}
    try:
        return 200, _route(method.upper(), path, q)
    except NotHandledError:
        return 404, {"detail": "Not Found"}


def _route(method: str, path: str, q: dict[str, str]) -> object:
    if method == "GET":
        if path == "/auth/status":
            return {"authenticated": True, "username": ME, "role": "admin", "user_id": user_id(ME),
                    "theme_preference": None}
        if path == "/edition":
            return {"name": "enterprise", "organisation": "Example Ltd"}
        if path == "/license":
            return {"state": "active", "organisation": "Example Ltd", "license_id": "lic_01JC7EXAMPLE42",
                    "expires": "2027-10-31", "grace_ends": "2027-11-30", "seats": 25, "seats_used": len(USERS),
                    "features": ["audit"], "banner": None, "refusal": None}
        if path == "/releases":
            # Nothing unseen and no update: no badge on the Help menu.
            return {"current_version": VERSION, "last_seen_version": VERSION, "unseen": 0, "notes": [], "update": None}
        if path == "/providers":
            providers = providers_catalog()
            return {"total": len(providers), "providers": providers, "presets": {"countries": country_options()},
                    "countries": []}
        if path == "/connector-options":
            return connector_options()
        if path == "/projects":
            return {"total": len(PROJECTS), "projects": [project_json(pid) for pid in PROJECTS]}
        if path == "/geo/accuracy":
            days = int(q.get("days", 30))
            return {"since": ago(days=days),
                    "connectors": analytics.accuracy_rows(q.get("project_id"), q.get("connector_id"), days)}
        if path == "/geo/exits":
            days = int(q.get("days", 30))
            return {"since": ago(days=days),
                    "connectors": analytics.exits_rows(q.get("project_id"), q.get("connector_id"), days)}
        if path == "/audit/retention":
            return {"days": 365}
        if path == "/audit":
            return _audit_page(q)
        mm = re.fullmatch(r"/projects/([0-9a-f-]{36})(/.*)?", path)
        if mm and mm.group(1) in PROJECTS:
            return _project_get(mm.group(1), mm.group(2) or "", q)
    if method == "POST" and path == "/providers/brightdata/options/zones":
        return zones_options()
    raise NotHandledError


def _audit_page(q: dict[str, str]) -> dict:
    """A page of the audit log, filtered the way the Activity page asks."""
    entries = analytics.activity_entries()
    filtered = [e for e in entries
                if (not q.get("entity_type") or e["entity_type"] == q["entity_type"])
                and (not q.get("action") or e["action"] == q["action"])
                and (not q.get("actor") or e["actor"].lower() == q["actor"].lower())
                and (not q.get("project_id") or e["project_id"] == q["project_id"])]
    offset, limit = int(q.get("offset", 0)), int(q.get("limit", 50))
    total = analytics.ACTIVITY_TOTAL if len(filtered) == len(entries) else len(filtered)
    return {"entries": filtered[offset:offset + limit], "total": total}


def _project_get(pid: str, sub: str, q: dict[str, str]) -> object:
    if sub == "":
        return project_json(pid)
    if sub == "/metrics":
        return pool_json(pid)
    if sub == "/metrics/scaling":
        return scaling_json(pid)
    if sub == "/metrics/history":
        return {"snapshots": series.project_history(pid, q.get("range", "24h"))}
    if sub == "/metrics/traffic-split":
        return analytics.traffic_split(pid, q.get("range", "1h"))
    if sub == "/connectors":
        conns = connectors_of(pid)
        return {"total": len(conns), "connectors": [connector_json(c) for c in conns]}
    mm = re.fullmatch(r"/connectors/([0-9a-f-]{36})/metrics/history", sub)
    if mm and mm.group(1) in CONN_BY_ID:
        return {"snapshots": series.history([mm.group(1)], q.get("range", "24h"))}
    if sub == "/credentials":
        creds = credentials_of(pid)
        return {"total": len(creds), "credentials": creds}
    mm = re.fullmatch(r"/credentials/([0-9a-f-]{36})", sub)
    if mm:
        for cred in credentials_of(pid):
            if cred["id"] == mm.group(1):
                return {**cred, "config": CREDENTIAL_CONFIG.get(cred["type"], {})}
    if sub == "/proxies":
        proxies = build_proxies(pid)
        return {"total": len(proxies), "healthy": sum(1 for p in proxies if p["status"] == "healthy" and not p["quarantined"]),
                "proxies": proxies}
    if sub == "/failures/summary":
        return series.failure_summary(pid, q.get("range", "24h"))
    if sub == "/failures":
        recs = analytics.failure_records(pid)
        if q.get("kind"):
            recs = [r for r in recs if r["kind"] == q["kind"]]
        return {"records": recs[: int(q.get("count", 200))], "next_cursor": None, "total": len(recs), "newest": 48211,
                "enabled": True, "max_entries": 500, "retention_hours": 72}
    if sub == "/failures/head":
        return {"changed": 0, "total": len(analytics.failure_records(pid))}
    if sub == "/connect":
        return connect_json(pid, q)
    if sub == "/wireguard/peers":
        return wireguard_peers(pid)
    if sub == "/openvpn/peers":
        return openvpn_peers(pid)
    raise NotHandledError
