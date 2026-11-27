# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Project metrics: the pool and the counters now, the flushed history of the project and of each connector, and both
in the Prometheus text format.

Every number is read when asked for, the live parts from Redis through the read model: losing it shows the pool without
quarantines or traffic blocks, the counters without the window not flushed yet and demand as none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.core import utc_now
from api.db.redis import PROJECT_WINDOWS
from api.db.repository import ConnectorRepository, MetricsRepository, ProjectRepository
from api.models.metrics import RANGE_CONFIG, MetricsHistoryResponse, MetricsResponse
from api.services.errors import NotFoundError
from api.strategies import served_strategy

if TYPE_CHECKING:
    from api.core.stats import MetricDelta
    from api.db.session import SessionFactory
    from api.models.connector import ConnectorRow
    from api.models.metrics import MetricsRange, ScalingView
    from api.models.project import PoolCounts, ProjectView
    from api.services.read_model import RuntimeView


class MetricsService:
    """Reads a project's pool, counters, scaling and history and its connectors' history; exports them for Prometheus.

    Args:
        views: The read model, which joins the stored definitions with the soft state other roles keep in Redis.
        default_strategy: The strategy a project storing one no router has is served with.
    """

    def __init__(
        self, session_factory: SessionFactory, *, views: RuntimeView, default_strategy: str
    ) -> None:
        self._session_factory = session_factory
        self._views = views
        self._default_strategy = default_strategy

    async def summary(self, project_id: str) -> MetricsResponse:
        """The project's pool by status and its counters, the totals plus the window not flushed yet, and its strategy."""
        view, counters = await self._counted(project_id)
        strategy = served_strategy(view.project.routing_strategy, self._default_strategy)
        return MetricsResponse.of(view.pool, counters, strategy)

    async def scaling(self, project_id: str) -> ScalingView:
        """The project's demand against its healthy pool, and its cloud connectors' instances."""
        view = await self._views.scaling(project_id)
        if view is None:
            raise NotFoundError("Project not found")
        return view

    async def history(self, project_id: str, window: MetricsRange) -> MetricsHistoryResponse:
        """The project's snapshots over ``window`` as the workers' metrics flushes wrote them, oldest first."""
        delta, limit, bucket_seconds = RANGE_CONFIG[window]
        async with self._session_factory() as session:
            if await ProjectRepository(session).get_by_id(project_id) is None:
                raise NotFoundError("Project not found")
            since = utc_now() - delta
            repo = MetricsRepository(session)
            if bucket_seconds:
                rows = await repo.get_project_metrics_history_aggregated(
                    project_id=project_id,
                    since=since,
                    bucket_seconds=bucket_seconds,
                )
            else:
                rows = await repo.get_project_metrics_history(
                    project_id=project_id,
                    since=since,
                    limit=limit,
                    granularity=60,
                )
        # Rows come back in descending order; reverse to chronological
        return MetricsHistoryResponse.of(reversed(rows))

    async def connector_history(
        self, project_id: str, connector_id: str, window: MetricsRange
    ) -> MetricsHistoryResponse:
        """A connector's snapshots over ``window`` from its own history, which outlives its rotated proxies, oldest
        first; another project's is not found."""
        delta, limit, bucket_seconds = RANGE_CONFIG[window]
        since = utc_now() - delta
        async with self._session_factory() as session:
            if await ConnectorRepository(session).get_in_project(project_id, connector_id) is None:
                raise NotFoundError("Connector not found")
            repo = MetricsRepository(session)
            if bucket_seconds:
                rows = await repo.get_connector_metrics_history_aggregated(
                    connector_id=connector_id, since=since, bucket_seconds=bucket_seconds
                )
            else:
                rows = await repo.get_connector_metrics_history(
                    connector_id=connector_id, since=since, limit=limit, granularity=60
                )
        return MetricsHistoryResponse.of(reversed(rows))

    async def exposition(self, project_id: str) -> str:
        """The pool and the counters as the summary shows them, and each connector's traffic this period against its
        limit and price, in the Prometheus text format."""
        view, counters = await self._counted(project_id)
        connectors = await self._views.connectors(project_id)
        return _exposition(project_id, view.pool, counters, connectors)

    async def _counted(self, project_id: str) -> tuple[ProjectView, MetricDelta]:
        """The project's view and its counters; not found when there is no such project."""
        view = await self._views.project(project_id)
        if view is None:
            raise NotFoundError("Project not found")
        counters = (await self._views.counters(PROJECT_WINDOWS, [project_id]))[project_id]
        return view, counters


def _exposition(
    project_id: str, pool: PoolCounts, counters: MetricDelta, connectors: list[ConnectorRow]
) -> str:
    """The project's numbers in the Prometheus text format, a family per number, connectors by name."""
    label_str = f'{{project="{_label_value(project_id)}"}}'

    lines = [
        "# HELP octoprox_proxies_total Total number of proxies in the pool",
        "# TYPE octoprox_proxies_total gauge",
        f"octoprox_proxies_total{label_str} {pool.total}",
        "",
        "# HELP octoprox_proxies_healthy Number of healthy proxies",
        "# TYPE octoprox_proxies_healthy gauge",
        f"octoprox_proxies_healthy{label_str} {pool.healthy}",
        "",
        "# HELP octoprox_proxies_quarantined Number of quarantined (rate-limited) proxies",
        "# TYPE octoprox_proxies_quarantined gauge",
        f"octoprox_proxies_quarantined{label_str} {pool.quarantined}",
        "",
        "# HELP octoprox_requests_total Total number of requests processed",
        "# TYPE octoprox_requests_total counter",
        f"octoprox_requests_total{label_str} {counters.request_count}",
        "",
        "# HELP octoprox_requests_success_total Total successful requests",
        "# TYPE octoprox_requests_success_total counter",
        f"octoprox_requests_success_total{label_str} {counters.success_count}",
        "",
        "# HELP octoprox_requests_failure_total Total failed requests",
        "# TYPE octoprox_requests_failure_total counter",
        f"octoprox_requests_failure_total{label_str} {counters.failure_count}",
        "",
        "# HELP octoprox_bytes_sent_total Total bytes sent through proxies",
        "# TYPE octoprox_bytes_sent_total counter",
        f"octoprox_bytes_sent_total{label_str} {counters.bytes_sent}",
        "",
        "# HELP octoprox_bytes_received_total Total bytes received through proxies",
        "# TYPE octoprox_bytes_received_total counter",
        f"octoprox_bytes_received_total{label_str} {counters.bytes_received}",
    ]

    # Per-connector traffic this period, against the limit and the price.
    usage_lines: list[str] = []
    limit_lines: list[str] = []
    cost_lines: list[str] = []
    blocked_lines: list[str] = []
    for connector_view in sorted(connectors, key=lambda v: v.connector.name.lower()):
        connector, usage = connector_view.connector, connector_view.traffic_usage
        names = f'project="{_label_value(project_id)}",connector="{connector.id}",name="{_label_value(connector.name)}"'
        labels = f"{{{names}}}"
        usage_lines.append(f"octoprox_connector_traffic_bytes{labels} {usage.total_bytes}")
        if usage.limit_bytes is not None:
            limit_lines.append(
                f"octoprox_connector_traffic_limit_bytes{labels} {usage.limit_bytes}"
            )
        if usage.cost is not None:
            cost_lines.append(
                f'octoprox_connector_traffic_cost{{{names},currency="{usage.currency}"}} {usage.cost}'
            )
        blocked_lines.append(
            f"octoprox_connector_traffic_blocked{labels} {1 if usage.blocked else 0}"
        )
    if connectors:
        lines += [
            "",
            "# HELP octoprox_connector_traffic_bytes Bytes through the connector in its current traffic period",
            "# TYPE octoprox_connector_traffic_bytes gauge",
            *usage_lines,
        ]
        if limit_lines:
            lines += [
                "",
                "# HELP octoprox_connector_traffic_limit_bytes The connector's traffic limit for the period",
                "# TYPE octoprox_connector_traffic_limit_bytes gauge",
                *limit_lines,
            ]
        if cost_lines:
            lines += [
                "",
                "# HELP octoprox_connector_traffic_cost Spend in the current period at the connector's price per GB",
                "# TYPE octoprox_connector_traffic_cost gauge",
                *cost_lines,
            ]
        lines += [
            "",
            "# HELP octoprox_connector_traffic_blocked"
            " 1 while the connector takes no requests because of its traffic limit",
            "# TYPE octoprox_connector_traffic_blocked gauge",
            *blocked_lines,
        ]

    # The text format ends every line, the last one included, with a line feed.
    return "\n".join(lines) + "\n"


def _label_value(value: str) -> str:
    """Escape a string for a Prometheus label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
