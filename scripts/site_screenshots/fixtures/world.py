# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The made-up install the screenshots show: projects, connectors, credentials, proxies, users and devices.

Names and addresses are invented: destination hosts are example domains and every IP is in a
documentation range (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24). The vendors are the real
integrations Octoprox ships, with their public gateway hostnames.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .common import GB, ago, unit, uuid_for

# --------------------------------------------------------------------------- users

USERS = [
    # username, role, email, sign-in providers, last sign-in in minutes ago
    ("maria", "admin", "maria@example.com", ["google"], 4),
    ("devon", "editor", "devon@example.com", [], 38),
    ("priya", "editor", "priya@example.com", ["github"], 190),
    ("sam", "viewer", "sam@example.com", [], 1460),
]
ME = "maria"


def user_id(name: str) -> str:
    return uuid_for(f"user:{name}")


# --------------------------------------------------------------------------- projects

P1 = uuid_for("project:price-monitoring")
P2 = uuid_for("project:market-research")
P3 = uuid_for("project:travel-fares")
P4 = uuid_for("project:real-estate-listings")


@dataclass
class ProjectSpec:
    """A project and the shape of its daily traffic."""

    id: str
    name: str
    description: str
    username: str
    password: str
    strategy: str
    created_days_ago: int
    # Requests per second: base + amp * shape(hour) ** 1.25, where shape is 0 at the local hour
    # `trough` and 1 twelve hours later.
    base: float
    amp: float
    trough: float


PROJECTS: dict[str, ProjectSpec] = {
    P1: ProjectSpec(P1, "Price monitoring", "Daily price and stock checks across retail catalogs in the US and Europe",
                    "pricemon", "Vq7-tR2x-91kL", "health_based", 214, base=14.0, amp=57.0, trough=2.5),
    P2: ProjectSpec(P2, "Market research", "Search result and listing snapshots for the quarterly market reports",
                    "research", "Hs4-pW8n-27dQ", "round_robin", 167, base=5.0, amp=24.0, trough=1.0),
    P3: ProjectSpec(P3, "Travel fares", "Fare and availability checks across booking sites, sticky per search",
                    "fares", "Lm2-kZ6c-58vT", "sticky", 96, base=3.0, amp=12.0, trough=8.0),
    P4: ProjectSpec(P4, "Real estate listings", "New and changed listings from property portals in the US and Canada",
                    "listings", "Rt5-yN3b-64wJ", "least_used", 58, base=2.0, amp=9.0, trough=6.5),
}

# --------------------------------------------------------------------------- connectors

# Latency steps in ms of a successful request through each kind of upstream.
STEPS = {
    "cloud": {"route": 1.6, "preflight": 6.0, "connect": 29.0, "handshake": 192.0, "response": 154.0},
    "datacenter": {"route": 1.5, "preflight": 5.0, "connect": 44.0, "handshake": 161.0, "response": 129.0},
    "residential_dynamic": {"route": 2.1, "preflight": 94.0, "connect": 138.0, "handshake": 518.0, "response": 302.0},
    "residential_pool": {"route": 2.0, "preflight": 37.0, "connect": 131.0, "handshake": 472.0, "response": 338.0},
}

# How a connector's failures split by kind; upstream_rejected carries the vendor statuses below.
KIND_MIX = {
    "cloud": {"upstream_timeout": 0.55, "upstream_error": 0.25, "upstream_unreachable": 0.12, "client_closed": 0.08},
    "datacenter": {"upstream_timeout": 0.40, "upstream_unreachable": 0.28, "upstream_error": 0.20, "client_closed": 0.12},
    "residential_dynamic": {"upstream_timeout": 0.44, "upstream_rejected": 0.41, "upstream_error": 0.08,
                            "preflight_mismatch": 0.04, "client_closed": 0.03},
    "residential_pool": {"upstream_timeout": 0.36, "upstream_rejected": 0.49, "upstream_error": 0.07,
                         "preflight_mismatch": 0.05, "client_closed": 0.03},
}
VENDOR_STATUSES = {
    "brightdata": [(502, 0.58), (429, 0.31), (403, 0.11)],
    "oxylabs": [(502, 0.79), (403, 0.21)],
    "decodo": [(502, 0.7), (429, 0.3)],
    "netnut": [(502, 0.8), (403, 0.2)],
    "iproyal": [(502, 0.75), (429, 0.25)],
}


@dataclass
class ConnSpec:
    """A connector and how its traffic behaves."""

    project: str
    key: str
    name: str
    ctype: str
    cred_name: str
    weight: int
    profile: str  # a key of STEPS and KIND_MIX
    kb_recv: float
    kb_sent: float
    fail: float  # failure ratio before load and incidents
    config: dict
    target: dict | None
    traffic_config: dict = field(default_factory=dict)
    rate_limit_config: dict = field(default_factory=dict)
    enabled: bool = True
    created_days_ago: int = 120
    updated_minutes_ago: int = 600
    share_bias: float = 1.0
    routing_extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return uuid_for(f"connector:{self.project}:{self.key}")

    @property
    def cred_id(self) -> str:
        return uuid_for(f"credential:{self.project}:{self.ctype}:{self.cred_name}")


# The vendors' public gateways, as the integrations use them (the failure log and the proxy list show them).
BRD_GATEWAY = ("brd.superproxy.io", 33335)
OXY_GATEWAY = ("pr.oxylabs.io", 7777)

BD_COUNTRIES = ["US", "CA", "MX", "BR", "GB", "IE", "DE", "FR", "ES", "IT", "NL", "PL", "SE", "JP", "IN", "SG", "AU",
                "ZA", "AE"]
OXY_COUNTRIES = ["US", "GB", "DE", "FR"]

# project, key, name, credential type, credential name, weight, profile, KB received and sent per request,
# failure ratio, config, target, then the optional fields by name
CONNECTORS: list[ConnSpec] = [
    # Price monitoring, the project every screenshot shows
    ConnSpec(P1, "aws", "AWS us-east-1", "aws", "AWS production", 5, "cloud", 17.6, 1.9, 0.011,
             {"auth_mode": "host_role", "instance_name": "pricemon-proxy", "region": "us-east-1",
              "instance_type": "t4g.small", "key_pair_name": "scraping-ops", "security_group": "sg-0a1b2c3d4e5f60718",
              "tags": {"team": "pricing", "env": "prod"}, "countries": ["US"], "min_proxies": 8, "max_proxies": 16,
              "min_rotation_period_minutes": 360, "max_rotation_period_minutes": 1440},
             {"total": 12, "per_country": None, "countries": ["US"], "on_demand": [], "dynamic": False,
              "exit_sample_percent": None},
             traffic_config={"price_per_gb": 0.09}, created_days_ago=210, updated_minutes_ago=2900),
    ConnSpec(P1, "gcp", "GCP Frankfurt", "gcp", "GCP scraping", 3, "cloud", 18.5, 1.9, 0.013,
             {"project_id": "example-scraping-prod", "instance_name": "pricemon-eu", "zone": "europe-west3-a",
              "machine_type": "e2-small", "network": "default", "tags": {"team": "pricing"}, "countries": ["DE"],
              "min_proxies": 4, "max_proxies": 10, "min_rotation_period_minutes": 360,
              "max_rotation_period_minutes": 1440},
             {"total": 8, "per_country": None, "countries": ["DE"], "on_demand": [], "dynamic": False,
              "exit_sample_percent": None},
             traffic_config={"price_per_gb": 0.12}, created_days_ago=180, updated_minutes_ago=8700),
    ConnSpec(P1, "brightdata", "Bright Data", "brightdata", "Pricing", 3, "residential_dynamic", 21.0, 2.2, 0.034,
             {"zone_name": "residential_pricing", "zone_password": "c8v2m4q9x1t7", "proxy_type": "residential",
              "session_mode": "dynamic", "exit_sample_percent": 5, "country_code": BD_COUNTRIES},
             {"total": 1, "per_country": None, "countries": BD_COUNTRIES, "on_demand": [], "dynamic": True,
              "exit_sample_percent": 5},
             traffic_config={"limit_bytes": 160 * GB, "price_per_gb": 5.88, "action": "block"}, created_days_ago=150,
             updated_minutes_ago=12, share_bias=1.08, routing_extra={"reserved_countries": ["DE"]}),
    ConnSpec(P1, "oxylabs", "Oxylabs pool", "oxylabs", "Pricing", 2, "residential_pool", 20.0, 2.1, 0.030,
             {"session_mode": "pool", "num_proxies": 10, "country_code": OXY_COUNTRIES, "session_duration_minutes": 10},
             {"total": 40, "per_country": 10, "countries": OXY_COUNTRIES, "on_demand": [], "dynamic": False,
              "exit_sample_percent": None},
             traffic_config={"limit_bytes": 150 * GB, "price_per_gb": 6.0}, created_days_ago=133,
             updated_minutes_ago=4400, routing_extra={"reserved_countries": ["DE"]}),
    ConnSpec(P1, "webshare", "Webshare list", "webshare", "Pricing", 7, "datacenter", 15.2, 1.8, 0.019,
             {"mode": "direct", "plan_id": "", "country_code": [], "num_proxies": ""},
             None,
             traffic_config={"limit_bytes": 500 * GB},
             rate_limit_config={"max_requests": 120, "window_seconds": 60, "quarantine_seconds_min": 60,
                                "quarantine_seconds_max": 300},
             created_days_ago=201, updated_minutes_ago=47, share_bias=0.96,
             routing_extra={"excluded_countries": ["FR"]}),
    # Market research
    ConnSpec(P2, "brightdata", "Bright Data residential", "brightdata", "Bright Data research", 3,
             "residential_dynamic", 32.0, 2.4, 0.024,
             {"zone_name": "residential_research", "proxy_type": "residential", "session_mode": "dynamic",
              "exit_sample_percent": 10, "country_code": []},
             {"total": 1, "per_country": None, "countries": [], "on_demand": [], "dynamic": True,
              "exit_sample_percent": 10},
             traffic_config={"limit_bytes": 200 * GB, "price_per_gb": 5.88}),
    ConnSpec(P2, "decodo", "Decodo ISP pool", "decodo", "Decodo research", 2, "residential_pool", 29.0, 2.2, 0.018,
             {"num_proxies": 8, "country_code": ["US", "GB", "DE"]},
             {"total": 24, "per_country": 8, "countries": ["US", "GB", "DE"], "on_demand": [], "dynamic": False,
              "exit_sample_percent": None},
             traffic_config={"price_per_gb": 4.2}),
    ConnSpec(P2, "azure", "Azure westeurope fleet", "azure", "Azure research", 2, "cloud", 27.5, 2.0, 0.012,
             {"location": "westeurope", "vm_size": "Standard_B1s", "countries": ["NL"], "min_proxies": 4,
              "max_proxies": 12},
             {"total": 6, "per_country": None, "countries": ["NL"], "on_demand": [], "dynamic": False,
              "exit_sample_percent": None},
             traffic_config={"price_per_gb": 0.087}),
    ConnSpec(P2, "webshare", "Webshare static list", "webshare", "Webshare research", 3, "datacenter", 24.5, 2.0, 0.016,
             {"mode": "direct"}, None),
    # Travel fares
    ConnSpec(P3, "oxylabs", "Oxylabs residential", "oxylabs", "Oxylabs fares", 3, "residential_pool", 35.5, 2.6, 0.034,
             {"session_mode": "pool", "num_proxies": 6, "country_code": ["US", "GB", "ES", "IT", "FR"],
              "session_duration_minutes": 30},
             {"total": 30, "per_country": 6, "countries": ["US", "GB", "ES", "IT", "FR"], "on_demand": [],
              "dynamic": False, "exit_sample_percent": None},
             traffic_config={"limit_bytes": 250 * GB, "price_per_gb": 6.0}),
    ConnSpec(P3, "netnut", "NetNut ISP", "netnut", "NetNut fares", 2, "residential_pool", 33.0, 2.4, 0.022,
             {"num_proxies": 10, "country_code": ["US", "GB"]},
             {"total": 20, "per_country": 10, "countries": ["US", "GB"], "on_demand": [], "dynamic": False,
              "exit_sample_percent": None},
             traffic_config={"price_per_gb": 3.5}),
    ConnSpec(P3, "aws", "AWS eu-west-1 fleet", "aws", "AWS fares", 2, "cloud", 30.0, 2.1, 0.012,
             {"region": "eu-west-1", "instance_type": "t4g.small", "countries": ["IE"], "min_proxies": 4,
              "max_proxies": 12},
             {"total": 8, "per_country": None, "countries": ["IE"], "on_demand": [], "dynamic": False,
              "exit_sample_percent": None},
             traffic_config={"price_per_gb": 0.09}),
    ConnSpec(P3, "iproyal", "IPRoyal residential", "iproyal", "IPRoyal fares", 1, "residential_dynamic", 34.0, 2.5,
             0.031,
             {"session_mode": "dynamic", "country_code": ["US", "GB", "ES", "IT", "FR", "DE", "PT"]},
             {"total": 1, "per_country": None, "countries": ["US", "GB", "ES", "IT", "FR", "DE", "PT"],
              "on_demand": [], "dynamic": True, "exit_sample_percent": 5},
             traffic_config={"limit_bytes": 120 * GB, "price_per_gb": 4.9}),
    # Real estate listings
    ConnSpec(P4, "decodo", "Decodo residential", "decodo", "Decodo listings", 3, "residential_dynamic", 41.0, 2.6,
             0.027,
             {"session_mode": "dynamic", "country_code": ["US", "CA"]},
             {"total": 1, "per_country": None, "countries": ["US", "CA"], "on_demand": [], "dynamic": True,
              "exit_sample_percent": 5},
             traffic_config={"limit_bytes": 80 * GB, "price_per_gb": 4.2}),
    ConnSpec(P4, "webshare", "Webshare listings", "webshare", "Webshare listings", 2, "datacenter", 37.0, 2.2, 0.018,
             {"mode": "direct"}, None),
    ConnSpec(P4, "gcp", "GCP us-central1 fleet", "gcp", "GCP listings", 2, "cloud", 39.0, 2.1, 0.012,
             {"zone": "us-central1-a", "machine_type": "e2-small", "countries": ["US"], "min_proxies": 4,
              "max_proxies": 10},
             {"total": 6, "per_country": None, "countries": ["US"], "on_demand": [], "dynamic": False,
              "exit_sample_percent": None},
             traffic_config={"price_per_gb": 0.12}),
    # The custom provider of the Providers screen, in use.
    ConnSpec(P4, "acme", "Acme residential", "acme", "Acme listings", 1, "residential_pool", 36.0, 2.4, 0.024,
             {"num_proxies": 10, "country_code": ["US", "CA"], "region": "na-east"},
             {"total": 20, "per_country": 10, "countries": ["US", "CA"], "on_demand": [], "dynamic": False,
              "exit_sample_percent": None},
             traffic_config={"price_per_gb": 3.8}, created_days_ago=5),
]

CONN_BY_ID = {c.id: c for c in CONNECTORS}


def connectors_of(project_id: str) -> list[ConnSpec]:
    return [c for c in CONNECTORS if c.project == project_id]


def credentials_of(project_id: str) -> list[dict]:
    """The project's credentials: one per distinct credential its connectors name."""
    seen: dict[str, dict] = {}
    for c in connectors_of(project_id):
        if c.cred_id in seen:
            continue
        seen[c.cred_id] = {
            "id": c.cred_id,
            "name": c.cred_name,
            "type": c.ctype,
            "project_id": project_id,
            "has_username": c.ctype in ("oxylabs", "decodo", "netnut", "iproyal", "acme"),
            "has_password": c.ctype not in ("aws", "gcp", "azure"),
            "created_at": ago(days=c.created_days_ago + 1, hours=3),
            "updated_at": ago(days=max(1, c.created_days_ago // 3)),
        }
    return list(seen.values())


CREDENTIAL_CONFIG = {
    "aws": {"auth_mode": "host_role"},
    "gcp": {"auth_mode": "host_identity", "project_id": "example-scraping-prod"},
    "azure": {"auth_mode": "host_identity", "subscription_id": "00000000-0000-4000-8000-000000000000"},
    "brightdata": {"token": "********", "customer_id": "hl_example42"},
    "oxylabs": {"proxy_type": "residential", "username": "example_pricing", "password": "********"},
    "decodo": {"proxy_type": "isp", "username": "example_research", "password": "********"},
    "webshare": {"api_key": "********"},
    "netnut": {"username": "example_fares", "password": "********"},
    "iproyal": {"username": "example_fares", "password": "********"},
    "acme": {"api_key": "********", "username": "example_listings", "password": "********"},
}

# --------------------------------------------------------------------------- proxies

# Where the fixed exits of the main project's connectors are: (country, state, city, count).
P1_PLACES = {
    "aws": [("US", "VA", "ashburn", 12)],
    "gcp": [("DE", "HE", "frankfurt", 8)],
    "oxylabs": [("US", "NY", "new_york", 4), ("US", "CA", "los_angeles", 3), ("US", "IL", "chicago", 3),
                ("GB", "ENG", "london", 6), ("GB", "ENG", "manchester", 4),
                ("DE", "BE", "berlin", 5), ("DE", "BY", "munich", 5),
                ("FR", "IDF", "paris", 7), ("FR", "ARA", "lyon", 3)],
    "webshare": [("US", "NY", "new_york", 9), ("US", "VA", "ashburn", 10), ("US", "CA", "los_angeles", 7),
                 ("US", "IL", "chicago", 6), ("US", "TX", "dallas", 5), ("US", "FL", "miami", 5),
                 ("GB", "ENG", "london", 14), ("DE", "HE", "frankfurt", 10), ("NL", "NH", "amsterdam", 8),
                 ("CA", "ON", "toronto", 6), ("FR", "IDF", "paris", 5), ("JP", "13", "tokyo", 5),
                 ("ES", "MD", "madrid", 4), ("IE", "L", "dublin", 3), ("IT", "25", "milan", 3)],
}


def doc_ip(slot: int) -> str:
    """A documentation-range address for slot 0 to 761."""
    ranges = ["203.0.113", "198.51.100", "192.0.2"]
    r, host = divmod(slot, 254)
    return f"{ranges[r % 3]}.{host + 1}"


def build_proxies(project_id: str) -> list[dict]:
    """The project's proxies; the main project's sit where P1_PLACES says, a few of them unwell."""
    if project_id != P1:
        return _other_project_proxies(project_id)
    out: list[dict] = []
    slot = 10
    by_key = {c.key: c for c in connectors_of(P1)}
    for key in ("aws", "gcp", "brightdata", "oxylabs", "webshare"):
        c = by_key[key]
        if key == "brightdata":
            out.append(_proxy(c, 0, BRD_GATEWAY[0], BRD_GATEWAY[1], BRD_GATEWAY[0], None, None, None, "healthy", False))
            continue
        i = 0
        for country, state, city, n in P1_PLACES[key]:
            for _ in range(n):
                ip = doc_ip(slot)
                slot += 1
                status, quarantined = "healthy", False
                if key == "aws" and i == 4:
                    status = "draining"
                if key == "gcp" and i == 6:
                    status = "terminating"
                if key == "webshare" and i in (17, 41, 77):
                    quarantined = True
                if key == "webshare" and i == 58:
                    status = "unhealthy"
                if key in ("aws", "gcp"):
                    host, port = ip, 3128
                elif key == "oxylabs":
                    host, port = OXY_GATEWAY
                else:
                    host, port = ip, 6100 + i
                out.append(_proxy(c, i, host, port, ip, country, state, city, status, quarantined))
                i += 1
    return out


def _proxy(c: ConnSpec, i: int, host: str, port: int, display: str, country: str | None, state: str | None,
           city: str | None, status: str, quarantined: bool) -> dict:
    requests = int(800 + unit(f"preq:{c.id}", i) * 5200)
    fail = c.fail * (0.6 + unit(f"pfail:{c.id}", i))
    failures = int(requests * fail)
    return {
        "id": uuid_for(f"proxy:{c.id}:{i}"),
        "host": host,
        "port": port,
        "protocol": "http",
        "username": None,
        "password": None,
        "display_host": display,
        "connector_id": c.id,
        "connector_name": c.name,
        "connector_enabled": c.enabled,
        "status": status,
        "request_count": requests,
        "success_count": requests - failures,
        "failure_count": failures,
        "success_rate": round((requests - failures) / requests * 100, 1),
        "avg_latency_ms": round(sum(STEPS[c.profile].values()) * (0.85 + 0.3 * unit(f"plat:{c.id}", i)), 1),
        "bytes_sent": int(requests * c.kb_sent * 1024),
        "bytes_received": int(requests * c.kb_recv * 1024),
        "quarantined": quarantined,
        "quarantine_remaining_seconds": 140 if quarantined else 0,
        "country": country,
        "country_source": "database" if country else None,
        "vendor_country": country,
        "location_conflict": False,
        "location_verdict": "confirmed" if country else None,
        "state_verdict": None,
        "city_verdict": None,
        "outside_declared_countries": False,
        "location": None,
        "state_code": state,
        "state_source": "database" if state else None,
        "city": city,
        "city_source": "database" if city else None,
        "tags": [c.ctype],
        "created_at": ago(hours=6 + int(unit(f"pcreated:{c.id}", i) * 300)),
    }


def _other_project_proxies(project_id: str) -> list[dict]:
    """The other projects' proxies, all healthy: as many as their connectors target, over their countries."""
    out = []
    for c in connectors_of(project_id):
        target = c.target or {}
        countries = target.get("countries") or ["US"]
        for i in range(target.get("total", 40)):
            country = None if target.get("dynamic") else countries[i % len(countries)]
            out.append(_proxy(c, i, doc_ip(300 + i), 8000 + i, doc_ip(300 + i), country, None, None, "healthy", False))
    return out


def pool_counts(project_id: str) -> dict:
    """The project's proxies by state, as the pool metrics count them."""
    proxies = build_proxies(project_id)
    quarantined = sum(1 for p in proxies if p["quarantined"])
    return {
        "total": len(proxies),
        "healthy": sum(1 for p in proxies if p["status"] == "healthy" and not p["quarantined"]),
        "quarantined": quarantined,
        # Quarantined proxies count as unhealthy too, as the API counts them.
        "unhealthy": sum(1 for p in proxies if p["status"] not in ("healthy", "draining", "terminating"))
        + sum(1 for p in proxies if p["status"] == "healthy" and p["quarantined"]),
        "draining": sum(1 for p in proxies if p["status"] == "draining"),
        "terminating": sum(1 for p in proxies if p["status"] == "terminating"),
    }


# --------------------------------------------------------------------------- provider catalog

def fld(key: str, label: str, field_type: str = "text", **settings: object) -> dict:
    """A provider form field, every setting at the API's default unless given."""
    return {
        "key": key, "label": label, "type": field_type, "required": False, "secret": False, "readonly": False,
        "default": None, "placeholder": None, "help": None, "details": None, "group": "general", "options": [],
        "options_preset": None, "options_from": None, "options_from_when": None, "empty_label": None, "fill": {},
        "min": None, "max": None, "max_from_option": [], "depends_on": [], "pattern": None, "transform": None,
        "show_when": None, **settings,
    }


def option(value: str, label: str, description: str | None = None) -> dict:
    return {"value": value, "label": label, "description": description}


DYNAMIC_DETAILS = (
    "Dynamic keeps one gateway proxy and renders the vendor credentials per request: a client -sessid- always maps to "
    "the same vendor session, a request without one gets a fresh session and the vendor rotates the exit, -cc- picks the "
    "country per request and -st- and -city- narrow it."
)

PROXY_TYPE_OPTIONS = [option("residential", "Residential"), option("mobile", "Mobile"), option("isp", "ISP"),
                      option("datacenter", "Datacenter")]


def _session_fields(proxy_type_scope: str) -> list[dict]:
    return [
        fld("session_mode", "Sessions", "select", default="pool",
            options=[option("pool", "Fixed pool"), option("dynamic", "Dynamic (unlimited)")],
            help='Fixed pool provisions "Number of proxies" sessions; dynamic creates a vendor session per request.',
            details=DYNAMIC_DETAILS,
            show_when={"field": proxy_type_scope, "in": ["residential", "mobile"], "equals": None, "negate": False}),
        fld("exit_sample_percent", "Exit sampling (%)", "number", default=5, min=0, max=100,
            help="Share of requests without a -sessid- whose exit preflight checks. Requests with a -sessid- are "
                 "always checked.",
            show_when={"field": "connector.session_mode", "equals": "dynamic", "in": None, "negate": False}),
        fld("num_proxies", "Number of proxies", "number", required=True, default=1, min=1, max=1000,
            help="Per country when countries are listed.",
            show_when={"field": "connector.session_mode", "equals": "dynamic", "in": None, "negate": True}),
        fld("country_code", "Countries", "select", options_preset="countries", empty_label="All countries",
            help="Leave empty for all countries."),
    ]


def _provider(pid: str, name: str, description: str, kind: str, *, cloud: bool = False, beta: bool = False,
              custom: bool = False, docs: str | None = None, credential_fields: list[dict] | None = None,
              connector_fields: list[dict] | None = None, proxy_type_field: str | None = None,
              proxy_types: tuple[tuple[str, str, str], ...] = (), egress: tuple[str, ...] = (),
              gateways: tuple[str, ...] = (), validates: bool = False) -> dict:
    """A catalog entry as GET /providers lists it, with its use counted over the install's credentials and connectors."""
    return {
        "id": pid, "name": name, "description": description, "kind": kind,
        "source": "custom" if custom else "builtin", "editable": custom, "syncable": kind == "descriptor",
        "cloud": cloud, "beta": beta, "logo": None, "logo_dark": None, "docs_url": docs,
        "credential_fields": credential_fields or [], "connector_fields": connector_fields or [],
        "proxy_type_field": proxy_type_field,
        "proxy_types": [{"key": key, "label": label, "mode": mode} for key, label, mode in proxy_types],
        "egress_hosts": list(egress), "gateway_hosts": list(gateways), "has_validation": validates,
        "credential_count": sum(1 for p in PROJECTS for cred in credentials_of(p) if cred["type"] == pid),
        "connector_count": sum(1 for c in CONNECTORS if c.ctype == pid), "version": 1,
        "updated_at": ago(days=5, hours=3) if custom else None,
    }


# The fields of the custom provider: the example of the descriptor reference in docs/providers.md, with the
# region_password field its `fill` names. Made up, like its .example hosts.
ACME_CREDENTIAL_FIELDS = [
    fld("api_key", "API key", "password", required=True, secret=True),
    fld("username", "Proxy username", required=True),
    fld("password", "Proxy password", "password", required=True, secret=True),
]
ACME_CONNECTOR_FIELDS = [
    fld("num_proxies", "Number of proxies", "number", required=True, default=1, min=1, max=1000),
    fld("country_code", "Country", "select", options_preset="countries"),
    fld("region", "Region", "select", options_from="regions", fill={"region_password": "password"},
        show_when={"field": "credential.api_key", "equals": None, "in": None, "negate": False}),
    fld("region_password", "Region password", "password", secret=True, readonly=True),
]


def providers_catalog() -> list[dict]:
    """The catalog the API serves: the shipped providers as they describe themselves, and the custom one.

    Only the vendors carry connector fields, which the inspector builds its forms from; no screen needs the rest.
    """
    bright_fields = [
        fld("zone_name", "Zone", "select", required=True, options_from="zones",
            fill={"zone_password": "password", "proxy_type": "proxy_type"},
            help="Proxy type and zone password follow the selected zone."),
        fld("zone_password", "Zone password", "password", secret=True, required=True,
            placeholder="Auto-filled from the zone"),
        fld("proxy_type", "Proxy type", "select", required=True, readonly=True, help="Derived from the zone.",
            options=PROXY_TYPE_OPTIONS),
        *_session_fields("connector.proxy_type"),
        fld("healthcheck_url", "Healthcheck URL", "url", group="advanced", placeholder="https://httpbin.org/ip (default)",
            help="Use a custom URL if the zone is restricted to certain targets."),
    ]
    oxy_fields = [
        *_session_fields("credential.proxy_type"),
        fld("session_duration_minutes", "Session duration (minutes)", "number", default=10, min=1, max=30,
            help="How long a sticky session keeps the same IP (1-30).",
            show_when={"field": "credential.proxy_type", "in": ["residential", "mobile"], "equals": None,
                       "negate": False}),
    ]
    webshare_fields = [
        fld("mode", "Connection mode", "select", required=True, default="direct",
            options=[option("direct", "Direct", "Connect to each proxy's own IP and port"),
                     option("backbone", "Backbone", "Connect through p.webshare.io (required for residential plans)")]),
        fld("plan_id", "Plan ID", help="Leave empty for the account's default plan."),
        fld("country_code", "Countries", "select", options_preset="countries", help="Leave empty for all countries."),
        fld("num_proxies", "Maximum proxies", "number", min=1, help="Per listed country. Leave empty to mirror the whole list."),
    ]
    generic = _session_fields("credential.proxy_type")
    sessions = (("residential", "Residential", "session"), ("mobile", "Mobile", "session"))
    catalog = [
        _provider("static_proxy_provider", "Static Proxy Provider", "Manually managed proxy servers", "code"),
        _provider("aws", "Amazon Web Services", "EC2 instances as proxy servers", "code", cloud=True),
        _provider("gcp", "Google Cloud Platform", "Compute Engine VMs as proxy servers", "code", cloud=True),
        _provider("azure", "Microsoft Azure", "Virtual Machines as proxy servers", "code", cloud=True),
        _provider("brightdata", "Bright Data", "Residential, Mobile, ISP and Datacenter proxies via zones", "descriptor",
                  docs="https://docs.brightdata.com/", connector_fields=bright_fields,
                  proxy_type_field="connector.proxy_type",
                  proxy_types=(*sessions, ("isp", "ISP", "port"), ("datacenter", "Datacenter", "port")),
                  egress=("api.brightdata.com",), gateways=(BRD_GATEWAY[0],), validates=True),
        _provider("oxylabs", "Oxylabs", "Residential, Mobile, ISP and Datacenter proxies", "descriptor",
                  docs="https://developers.oxylabs.io/", connector_fields=oxy_fields,
                  proxy_type_field="credential.proxy_type",
                  proxy_types=(*sessions, ("isp", "ISP", "port"), ("dedicated_isp", "Dedicated ISP", "port"),
                               ("datacenter", "Datacenter", "port"),
                               ("datacenter_dedicated", "Dedicated Datacenter", "port")),
                  gateways=("dc.oxylabs.io", "ddc.oxylabs.io", "disp.oxylabs.io", "isp.oxylabs.io", OXY_GATEWAY[0])),
        _provider("decodo", "Decodo", "Residential, Mobile, ISP and Datacenter proxies (formerly Smartproxy)",
                  "descriptor", beta=True, connector_fields=generic, proxy_type_field="credential.proxy_type",
                  proxy_types=(*sessions, ("isp", "ISP", "port"), ("datacenter", "Datacenter", "port")),
                  egress=("api.decodo.com",), gateways=("dc.decodo.com", "gate.decodo.com", "isp.decodo.com"),
                  validates=True),
        _provider("webshare", "Webshare", "Datacenter, static residential and rotating residential proxy lists",
                  "descriptor", beta=True, docs="https://apidocs.webshare.io/", connector_fields=webshare_fields,
                  proxy_types=(("proxies", "Proxy list", "list"),), egress=("proxy.webshare.io",), validates=True),
        _provider("iproyal", "IPRoyal", "Residential proxies with sticky sessions", "descriptor", beta=True,
                  connector_fields=generic, proxy_type_field="credential.proxy_type",
                  proxy_types=(sessions[0],), egress=("resi-api.iproyal.com",),
                  gateways=("{connector.entry_node|or:geo.iproyal.com}",), validates=True),
        _provider("netnut", "NetNut", "Residential, Static residential (ISP), Mobile and Datacenter proxies",
                  "descriptor", beta=True, connector_fields=generic, proxy_type_field="credential.proxy_type",
                  proxy_types=(sessions[0], ("static_residential", "Static residential (ISP)", "session"), sessions[1],
                               ("datacenter", "Datacenter", "session")),
                  gateways=("gw.netnut.net",)),
        _provider("acme", "Acme Proxies", "Residential proxies", "descriptor", custom=True,
                  docs="https://docs.acme.example", credential_fields=ACME_CREDENTIAL_FIELDS,
                  connector_fields=ACME_CONNECTOR_FIELDS, proxy_types=(("residential", "Residential", "session"),),
                  egress=("api.acme.example",), gateways=("gw.acme.example",), validates=True),
    ]
    # In the API's order: code providers first, then by name.
    return sorted(catalog, key=lambda p: (p["kind"] != "code", p["name"].lower()))


COUNTRY_NAMES = {
    "AE": "United Arab Emirates", "AR": "Argentina", "AT": "Austria", "AU": "Australia", "BE": "Belgium",
    "BR": "Brazil", "CA": "Canada", "CH": "Switzerland", "CL": "Chile", "CN": "China", "CO": "Colombia",
    "CZ": "Czechia", "DE": "Germany", "DK": "Denmark", "EG": "Egypt", "ES": "Spain", "FI": "Finland", "FR": "France",
    "GB": "United Kingdom", "GR": "Greece", "HK": "Hong Kong", "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland",
    "IL": "Israel", "IN": "India", "IT": "Italy", "JP": "Japan", "KR": "South Korea", "MX": "Mexico",
    "MY": "Malaysia", "NG": "Nigeria", "NL": "Netherlands", "NO": "Norway", "NZ": "New Zealand", "PE": "Peru",
    "PH": "Philippines", "PL": "Poland", "PT": "Portugal", "RO": "Romania", "SA": "Saudi Arabia", "SE": "Sweden",
    "SG": "Singapore", "TH": "Thailand", "TR": "Turkey", "TW": "Taiwan", "UA": "Ukraine", "US": "United States",
    "VN": "Vietnam", "ZA": "South Africa",
}


def country_options() -> list[dict]:
    return [option(code, name) for code, name in sorted(COUNTRY_NAMES.items(), key=lambda kv: kv[1])]


def connector_options() -> dict:
    """The regions, zones and machine sizes the cloud connector forms offer."""
    return {
        "aws_regions": [{"code": "us-east-1", "name": "US East (N. Virginia)"},
                        {"code": "us-west-2", "name": "US West (Oregon)"},
                        {"code": "eu-west-1", "name": "Europe (Ireland)"},
                        {"code": "eu-central-1", "name": "Europe (Frankfurt)"},
                        {"code": "ap-northeast-1", "name": "Asia Pacific (Tokyo)"}],
        "aws_instance_types": [
            {"code": "t4g.small", "vcpus": 2, "memory_gb": 2, "architecture": "arm64",
             "description": "2 vCPU, 2 GiB, Graviton"},
            {"code": "t3.small", "vcpus": 2, "memory_gb": 2, "architecture": "x86_64", "description": "2 vCPU, 2 GiB"},
            {"code": "t3.micro", "vcpus": 2, "memory_gb": 1, "architecture": "x86_64", "description": "2 vCPU, 1 GiB"},
        ],
        "gcp_zones": [{"code": "europe-west3-a", "name": "Frankfurt (europe-west3-a)"},
                      {"code": "us-central1-a", "name": "Iowa (us-central1-a)"}],
        "gcp_machine_types": [
            {"code": "e2-small", "vcpus": 2, "memory_gb": 2, "architecture": "x86_64",
             "description": "2 shared vCPU, 2 GB"},
            {"code": "e2-micro", "vcpus": 2, "memory_gb": 1, "architecture": "x86_64",
             "description": "2 shared vCPU, 1 GB"},
        ],
        "azure_locations": [{"code": "westeurope", "name": "West Europe"}, {"code": "eastus", "name": "East US"}],
        "azure_vm_sizes": [{"code": "Standard_B1s", "vcpus": 1, "memory_gb": 1, "architecture": "x86_64",
                            "description": "1 vCPU, 1 GiB"}],
        "countries": [{"code": o["value"], "name": o["label"]} for o in country_options()],
    }


# --------------------------------------------------------------------------- tunnel devices (main project)

# name, tunnel address, online, minutes since its last handshake, endpoint, country, state, city, session,
# requests and MB relayed, days since it was added
WIREGUARD_DEVICES = [
    ("QA iPhone 15", "192.0.2.10", True, 1, "203.0.113.201:51820", "US", "NY", "new_york", None, 18412, 2310, 41),
    ("QA Pixel 8", "192.0.2.11", True, 2, "203.0.113.202:41234", "US", "CA", "los_angeles", None, 15206, 1984, 41),
    ("Smart TV test rig", "192.0.2.12", True, 0, "198.51.100.230:51820", "GB", None, None, "tv-rig", 6120, 9850, 22),
    ("Retail kiosk emulator", "192.0.2.13", False, 380, "198.51.100.231:60111", "DE", None, None, None, 2208, 412, 15),
    ("Office router (London)", "192.0.2.14", True, 1, "203.0.113.240:51820", None, None, None, None, 48230, 15640, 63),
]
OPENVPN_DEVICES = [
    ("Legacy router (Berlin)", "192.0.2.40", True, 95, "198.51.100.241:1194", "DE", None, None, None, 9230, 3120, 30),
]
