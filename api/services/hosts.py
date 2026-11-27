# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Host metrics (the Hosts page): where a project's traffic went over a window, by destination host and connector.

Sums the host history the workers' metrics flushes write, so the last flush interval of traffic is not in it yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from api.core import utc_now
from api.core.stats import normalize_host
from api.db.repository import ConnectorRepository, MetricsRepository, ProjectRepository
from api.models.metrics import (
    HOST_HISTORY_BUCKETS,
    MAX_HOST_SERIES,
    RANGE_CONFIG,
    HostConnectorMetrics,
    HostMetrics,
    HostsConnector,
    HostSeries,
    HostSeriesPoint,
    HostsHistoryResponse,
    HostsResponse,
    HostsTotals,
)
from api.services.errors import NotFoundError

if TYPE_CHECKING:
    from api.db.session import SessionFactory
    from api.models.metrics import MetricsRange


class HostMetricsService:
    """Reads a project's requested hosts over a window, busiest first, in total and over time.

    Args:
        enabled: Whether per-host counting is on at all (``metrics.hosts.enabled``), which every answer tells.
    """

    def __init__(self, session_factory: SessionFactory, *, enabled: bool) -> None:
        self._session_factory = session_factory
        self._enabled = enabled

    async def of_project(
        self,
        project_id: str,
        window: MetricsRange,
        *,
        connector_id: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> HostsResponse:
        """The hosts the project requested in ``window``, busiest first and at most ``limit``, with totals over every one
        that matched and a per-connector split; ``connector_id`` must be the project's."""
        delta, _limit, _bucket = RANGE_CONFIG[window]
        async with self._session_factory() as session:
            if await ProjectRepository(session).get_by_id(project_id) is None:
                raise NotFoundError("Project not found")
            since = utc_now() - delta
            search = search.strip() if search else None
            connectors = {
                c.id: c for c in await ConnectorRepository(session).get_by_project(project_id)
            }
            if connector_id is not None and connector_id not in connectors:
                raise NotFoundError("Connector not found in this project")
            repo = MetricsRepository(session)
            summary = await repo.get_host_summary(
                project_id, since, connector_id=connector_id, search=search or None
            )
            rows = await repo.get_host_totals(
                project_id, since, connector_id=connector_id, search=search or None, limit=limit
            )
            breakdown = await repo.get_host_connector_breakdown(
                project_id, since, [r["host"] for r in rows], connector_id=connector_id
            )

        total_requests = summary["request_count"]
        total_bytes = summary["bytes_sent"] + summary["bytes_received"]
        hosts: list[HostMetrics] = []
        for row in rows:
            host_bytes = row["bytes_sent"] + row["bytes_received"]
            per_connector = []
            for part in breakdown.get(row["host"], []):
                connector = connectors.get(part["connector_id"])
                per_connector.append(
                    HostConnectorMetrics(
                        connector_id=part["connector_id"],
                        name=connector.name if connector else None,
                        credential_type=connector.credential_type if connector else None,
                        request_count=part["request_count"],
                        success_count=part["success_count"],
                        failure_count=part["failure_count"],
                        avg_latency_ms=round(part["avg_latency_ms"], 2),
                        bytes_sent=part["bytes_sent"],
                        bytes_received=part["bytes_received"],
                    )
                )
            hosts.append(
                HostMetrics(
                    host=row["host"],
                    request_count=row["request_count"],
                    success_count=row["success_count"],
                    failure_count=row["failure_count"],
                    avg_latency_ms=round(row["avg_latency_ms"], 2),
                    bytes_sent=row["bytes_sent"],
                    bytes_received=row["bytes_received"],
                    request_share=round(row["request_count"] / total_requests * 100, 2)
                    if total_requests
                    else 0.0,
                    bytes_share=round(host_bytes / total_bytes * 100, 2) if total_bytes else 0.0,
                    connectors=per_connector,
                )
            )

        return HostsResponse(
            range=window,
            since=since,
            enabled=self._enabled,
            limit=limit,
            totals=HostsTotals(
                host_count=summary["host_count"],
                request_count=summary["request_count"],
                success_count=summary["success_count"],
                failure_count=summary["failure_count"],
                avg_latency_ms=round(summary["avg_latency_ms"], 2),
                bytes_sent=summary["bytes_sent"],
                bytes_received=summary["bytes_received"],
            ),
            connectors=[
                HostsConnector(
                    connector_id=c.id,
                    name=c.name,
                    credential_type=c.credential_type,
                    enabled=c.enabled,
                )
                for c in sorted(connectors.values(), key=lambda c: c.name.lower())
            ],
            hosts=hosts,
        )

    async def history(
        self,
        project_id: str,
        window: MetricsRange,
        *,
        connector_id: str | None = None,
        top: int = 8,
        hosts: Sequence[str] | None = None,
    ) -> HostsHistoryResponse:
        """The busiest ``top`` hosts of ``window`` (or the ``hosts`` named) over time, busiest first, the rest folded
        into one last series; every series carries every bucket, zero where quiet, so they stack without gaps."""
        delta, _limit, _bucket = RANGE_CONFIG[window]
        bucket_seconds = HOST_HISTORY_BUCKETS[window]
        async with self._session_factory() as session:
            if await ProjectRepository(session).get_by_id(project_id) is None:
                raise NotFoundError("Project not found")
            since = utc_now() - delta
            connector_repo = ConnectorRepository(session)
            if (
                connector_id is not None
                and await connector_repo.get_in_project(project_id, connector_id) is None
            ):
                raise NotFoundError("Connector not found in this project")
            repo = MetricsRepository(session)
            if hosts:
                # The stored form (normalize_host), each host once, in the order given.
                named = (normalize_host(part) for part in hosts)
                chosen = list(dict.fromkeys(h for h in named if h is not None))[:MAX_HOST_SERIES]
            else:
                chosen = [
                    r["host"]
                    for r in await repo.get_host_totals(
                        project_id, since, connector_id=connector_id, limit=top
                    )
                ]
            rows = await repo.get_host_history_aggregated(
                project_id, since, bucket_seconds, chosen, connector_id=connector_id
            )

        # Every series gets every bucket, so the chart stacks cleanly.
        timestamps = sorted({row["timestamp"] for row in rows})
        by_series: dict[str, dict[datetime, dict[str, Any]]] = {host: {} for host in chosen}
        for row in rows:
            by_series.setdefault(row["host"], {})[row["timestamp"]] = row
        order = [*chosen, *(h for h in by_series if h not in chosen)]
        series: list[HostSeries] = []
        for host in order:
            points = by_series.get(host, {})
            if not points and host not in chosen:
                continue
            series.append(
                HostSeries(
                    host=host,
                    points=[
                        HostSeriesPoint(
                            timestamp=ts,
                            request_count=p.get("request_count", 0),
                            success_count=p.get("success_count", 0),
                            failure_count=p.get("failure_count", 0),
                            avg_latency_ms=round(float(p.get("avg_latency_ms", 0.0)), 2),
                            bytes_sent=p.get("bytes_sent", 0),
                            bytes_received=p.get("bytes_received", 0),
                        )
                        for ts in timestamps
                        for p in [points.get(ts, {})]
                    ],
                )
            )

        return HostsHistoryResponse(
            range=window, since=since, bucket_seconds=bucket_seconds, series=series
        )
