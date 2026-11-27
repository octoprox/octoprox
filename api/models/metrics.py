# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Project metrics: the ranges the history pages share, and what the metrics endpoints answer (pool, counters, scaling,
history, the traffic split and hosts).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from api.core.stats import HOST_OVERFLOW, success_rate
from api.reputation.weight import PlaceState, ReputationKind
from api.strategies import STRATEGY_NAMES

if TYPE_CHECKING:
    from api.core.stats import MetricDelta
    from api.models.exit_reputation import ConnectorShare
    from api.models.project import PoolCounts

# The ranges a history reads, from the last hour to the last thirty days (the keys of RANGE_CONFIG).
MetricsRange = Literal["1h", "6h", "24h", "7d", "30d"]

# What each history range reads: how far back (a timedelta), how many rows a
# raw query returns at most (one-minute rows: 60 per hour), and the bucket in
# seconds an aggregated query groups into (None reads the raw one-minute rows).
# Ranges up to a day are raw; longer ones are bucketed to a hundred-odd points
# so a chart stays readable and a query stays cheap.
RANGE_CONFIG: dict[str, tuple[timedelta, int, int | None]] = {
    "1h": (timedelta(hours=1), 60, None),
    "6h": (timedelta(hours=6), 360, None),
    "24h": (timedelta(hours=24), 1440, None),
    "7d": (timedelta(days=7), 0, 3600),  # 1-hour buckets -> ~168 points
    "30d": (timedelta(days=30), 0, 3600 * 6),  # 6-hour buckets -> ~120 points
}

# Bucket width per range for the per-host chart. Every range is bucketed
# (there are several series), sized to a hundred-odd points each.
HOST_HISTORY_BUCKETS: dict[str, int] = {
    "1h": 60,
    "6h": 300,
    "24h": 900,
    "7d": 3600,
    "30d": 3600 * 6,
}

# Most hosts one page of the Hosts table holds: enough to scroll through, few
# enough that one answer stays small; search reaches the rest.
MAX_HOSTS_PER_PAGE = 500
# Most hosts the chart gives a series of their own; every other host is folded
# into one more. Past a dozen stacked series no chart stays readable.
MAX_HOST_SERIES = 12


class PoolMetrics(BaseModel):
    """Proxy pool metrics."""

    total_proxies: int
    healthy_proxies: int
    unhealthy_proxies: int
    quarantined_proxies: int
    draining_proxies: int
    terminating_proxies: int
    total_requests: int
    total_successes: int
    total_failures: int
    overall_success_rate: float
    avg_latency_ms: float
    total_bytes_sent: int
    total_bytes_received: int


@dataclass(frozen=True)
class ScalingView:
    """A project's demand and its cloud instances, as the scaling metrics show them."""

    # The demand level's name in capitals (LOW, MEDIUM, HIGH).
    demand_level: str
    requests_per_minute: float
    rate_per_proxy: float
    # Instances of the project's enabled cloud connectors that no traffic limit blocks.
    current_instances: int
    healthy_instances: int
    # Sums over the project's enabled cloud connectors.
    min_instances: int
    max_instances: int
    draining_instances: int
    terminating_instances: int


class ScalingMetrics(BaseModel):
    """Auto-scaling metrics."""

    demand_level: str
    requests_per_minute: float
    rate_per_proxy: float
    current_instances: int
    healthy_instances: int
    min_instances: int
    max_instances: int
    draining_instances: int
    terminating_instances: int

    @classmethod
    def of(cls, view: ScalingView) -> ScalingMetrics:
        """A project's demand and cloud instances, as the read model counted them."""
        return cls(**asdict(view))


class StrategyMetrics(BaseModel):
    """Routing strategy metrics."""

    current_strategy: str
    available_strategies: list[str]


class MetricsResponse(BaseModel):
    """Combined metrics response."""

    pool: PoolMetrics
    strategy: StrategyMetrics

    @classmethod
    def of(cls, pool: PoolCounts, counters: MetricDelta, strategy: str) -> MetricsResponse:
        """The pool by status and the counters, rates to two places, beside the strategy served and every one offered."""
        return cls(
            pool=PoolMetrics(
                total_proxies=pool.total,
                healthy_proxies=pool.healthy,
                unhealthy_proxies=pool.unhealthy,
                quarantined_proxies=pool.quarantined,
                draining_proxies=pool.draining,
                terminating_proxies=pool.terminating,
                total_requests=counters.request_count,
                total_successes=counters.success_count,
                total_failures=counters.failure_count,
                overall_success_rate=round(success_rate(counters), 2),
                avg_latency_ms=round(counters.avg_latency_ms, 2),
                total_bytes_sent=counters.bytes_sent,
                total_bytes_received=counters.bytes_received,
            ),
            strategy=StrategyMetrics(
                current_strategy=strategy,
                available_strategies=list(STRATEGY_NAMES),
            ),
        )


class MetricsSnapshot(BaseModel):
    """A single metrics snapshot."""

    timestamp: datetime
    request_count: int
    success_count: int
    failure_count: int
    # Over the successful requests; None when none succeeded.
    avg_latency_ms: float | None
    # Each step's average over the successful requests (route, preflight, connect, handshake, response), adding up to
    # avg_latency_ms; None when none succeeded or for history from before steps were kept.
    step_latency_ms: dict[str, float] | None = None
    bytes_sent: int
    bytes_received: int


class MetricsHistoryResponse(BaseModel):
    """Historical metrics response."""

    snapshots: list[MetricsSnapshot]

    @classmethod
    def of(cls, rows: Iterable[Mapping[str, Any]]) -> MetricsHistoryResponse:
        """The history rows as snapshots, in the order given."""
        return cls(snapshots=[MetricsSnapshot(**row) for row in rows])


class ConnectorTrafficShare(BaseModel):
    """One connector's slice of a project's traffic under the current weights."""

    connector_id: str
    name: str
    credential_type: str
    enabled: bool
    weight: int
    dynamic: bool
    total_proxies: int
    eligible_proxies: int
    # Share of untargeted requests this connector takes right now, 0-100.
    # Zero when the connector is disabled or has no eligible proxy.
    expected_share: float
    # Why the connector takes nothing, when it does: "disabled", "traffic_limit"
    # (blocked by its traffic limit), "reserved_only" (it serves only requests
    # naming a country it reserves) or "no_eligible_proxies".
    excluded_reason: str | None = None
    observed_requests: int
    # Share of the project's requests in the window that went through this connector, 0-100.
    observed_share: float | None = None
    # Bytes, both directions, the connector carried in the window, and what
    # they cost at its price per GB (None when the connector is unpriced).
    observed_bytes: int = 0
    cost: float | None = None
    currency: str | None = None
    # Exit reputation for the site asked about (all unset without one): what
    # the weight is multiplied by there, what measures it, and the share it
    # makes of the requests to the site from the place asked about. A dynamic
    # connector is measured by its block rate at the finest level of the place
    # that has enough requests; a connector with IPs by its rows eligible for
    # such a request that are not avoided for the site.
    reputation_place: str | None = Field(
        default=None,
        description="The level of the place whose factor a dynamic connector's reputation came from; null when no "
        "level has one, and for a connector with IPs",
    )
    reputation: float = Field(
        default=1.0,
        description="What the weight is multiplied by for the site; 1 when nothing says otherwise",
    )
    reputation_kind: ReputationKind | None = None
    reputation_state: PlaceState | None = Field(
        default=None,
        description="Where the reputation stands at the place asked about, as the places view tells: own, inherited, "
        "not_judged, no_reports or takes_none",
    )
    blocked: int | None = Field(
        default=None,
        description="Recent blocked reports behind the reputation (decayed by Connector memory, and by Remember for "
        "since the last count); null without a record",
    )
    requests: int | None = Field(
        default=None, description="Recent requests behind the reputation, decayed the same way"
    )
    success: float | None = Field(
        default=None,
        description="A judged dynamic connector's own factor (one less its block rate) at the level its reputation came "
        "from, before it is compared with the best; null when not judged",
    )
    best_success: float | None = Field(
        default=None,
        description="The own factor of the best judged dynamic connector taking the requests, which this one is "
        "compared with; null when this one is not judged",
    )
    best_connector_id: str | None = Field(
        default=None, description="That best connector; null with best_success"
    )
    usable_ips: int | None = Field(
        default=None, description="A connector with IPs: its eligible rows not avoided for the site"
    )
    eligible_ips: int | None = Field(
        default=None,
        description="A connector with IPs: its rows eligible for a request to the site from the place, as routing finds them",
    )
    effective_weight: float = Field(
        default=0.0,
        description="The weight routing uses for requests to the site from the place; 0 when the connector takes none of them",
    )
    effective_share: float = Field(
        default=0.0,
        description="Share of the requests to the site from the place, 0-100, by effective weight where the project avoids, else by "
        "weight among the connectors serving them; the expected share without a site",
    )

    def weigh(self, share: ConnectorShare) -> None:
        """Take what exit reputation says of the connector for the site asked about."""
        reputation = share.reputation
        self.reputation_place = reputation.place
        self.reputation = reputation.value
        self.reputation_kind = reputation.kind
        self.reputation_state = share.state
        self.blocked = share.blocked
        self.requests = share.requests
        self.success = reputation.success
        self.best_success = reputation.best
        self.best_connector_id = reputation.best_connector_id
        self.usable_ips = reputation.usable_ips
        self.eligible_ips = reputation.eligible_ips
        self.effective_weight = round(share.effective_weight, 4)
        self.effective_share = round(share.effective_share, 2)


class ReputationSiteOption(BaseModel):
    """A site with exit reputation data for the project, and the reports made on it."""

    site: str
    reports: int


class ReputationPlaceOption(BaseModel):
    """A requested place with connector reputation data, how it reads, and the reports made for it."""

    place: str = Field(description="A LocationTarget key: US, US/NY, US/NY/new_york or US//austin")
    label: str = Field(description="United States, New York, United States or New York, NY")
    reports: int


class TrafficSplitResponse(BaseModel):
    """How a project's traffic divides between its connectors, expected and observed, and under exit reputation for one
    site."""

    strategy: str
    range: str
    total_weight: int
    observed_requests: int
    observed_bytes: int = 0
    connectors: list[ConnectorTrafficShare]
    site: str | None = Field(
        default=None, description="The site the effective shares are for, as records keep it"
    )
    place: str | None = Field(
        default=None, description="The requested place dynamic connectors are weighed at"
    )
    reputation_applied: bool = Field(
        default=False,
        description="Routing weighs the connectors for the site: the project avoids and a site was given",
    )
    reputation_strength: int = Field(
        default=1,
        description="How hard a dynamic connector is weighed against the best one at a place; 1 is the plain trim",
    )
    reputation_floor: float = Field(
        default=0.1,
        description="The least factor a dynamic connector's weight is multiplied by, 0.01-1",
    )
    reputation_sites: list[ReputationSiteOption] = Field(
        default_factory=list,
        description="Every site with exit reputation data for the project, most reported first",
    )
    reputation_places: list[ReputationPlaceOption] = Field(
        default_factory=list,
        description="Every place with connector reputation data for the site, most reported first; empty without a site",
    )


# ---------------------------------------------------------------------------
# Hosts: where a project's traffic goes, by destination host and connector.


class HostConnectorMetrics(BaseModel):
    """One connector's part of a host's traffic in the window."""

    connector_id: str
    # None when the connector was deleted after the rows were written (they
    # cascade with it, so this is a flush-window race, not a lasting state).
    name: str | None = None
    credential_type: str | None = None
    request_count: int
    success_count: int
    failure_count: int
    avg_latency_ms: float
    bytes_sent: int
    bytes_received: int


class HostMetrics(BaseModel):
    """A destination host's traffic in the window, in total and per connector."""

    host: str
    request_count: int
    success_count: int
    failure_count: int
    avg_latency_ms: float
    bytes_sent: int
    bytes_received: int
    # Share of the window's requests and bytes (both directions), 0-100.
    request_share: float
    bytes_share: float
    connectors: list[HostConnectorMetrics]


class HostsTotals(BaseModel):
    """The window's totals over every host that matched."""

    host_count: int
    request_count: int
    success_count: int
    failure_count: int
    avg_latency_ms: float
    bytes_sent: int
    bytes_received: int


class HostsConnector(BaseModel):
    """A connector of the project, for the filter and the legend."""

    connector_id: str
    name: str
    credential_type: str
    enabled: bool


class HostsResponse(BaseModel):
    """A project's requested hosts in the window, busiest first."""

    range: str
    since: datetime
    # The row hosts past the per-window cap are folded into, when present.
    overflow_host: str = HOST_OVERFLOW
    # Whether per-host counting is on at all (``metrics.hosts.enabled``).
    enabled: bool
    # How many hosts the response holds at most; ``totals.host_count`` says
    # how many matched. Search to reach the rest.
    limit: int
    totals: HostsTotals
    connectors: list[HostsConnector]
    hosts: list[HostMetrics]


class HostSeriesPoint(BaseModel):
    """One bucket of one host's series."""

    timestamp: datetime
    request_count: int
    success_count: int
    failure_count: int
    avg_latency_ms: float
    bytes_sent: int
    bytes_received: int


class HostSeries(BaseModel):
    """One host's traffic over time."""

    host: str
    points: list[HostSeriesPoint]


class HostsHistoryResponse(BaseModel):
    """The busiest hosts of the window over time, with the rest folded into one series."""

    range: str
    since: datetime
    bucket_seconds: int
    overflow_host: str = HOST_OVERFLOW
    series: list[HostSeries]
