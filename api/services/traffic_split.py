# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The traffic split: how a project's traffic divides between its connectors, expected now and observed over a window,
and under exit reputation for one site.

The expected share comes from the gateway's own router over a compile of the project (the routing preview), the observed
one from the flushed connector metrics, and the reputation from the project's records in Postgres.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from api.core import utc_now
from api.db.repository import ConnectorRepository, MetricsRepository, ProxyRepository
from api.db.reputation_repository import ExitReputationRepository
from api.models.exit_reputation import place_label
from api.models.metrics import (
    RANGE_CONFIG,
    ConnectorTrafficShare,
    ReputationPlaceOption,
    ReputationSiteOption,
    TrafficSplitResponse,
)
from api.reputation.record import place_levels
from api.reputation.site import report_site
from api.reputation.split import ReputationSplit, place_key
from api.services.errors import InputRejectedError, NotFoundError

if TYPE_CHECKING:
    from api.db.session import SessionFactory
    from api.feed.preview import RoutingPreview
    from api.models.metrics import MetricsRange


class TrafficSplitService:
    """Reads how a project's traffic divides between its connectors, and with a site each one's exit reputation there."""

    def __init__(self, session_factory: SessionFactory, *, preview: RoutingPreview) -> None:
        self._session_factory = session_factory
        self._preview = preview

    async def split(
        self,
        project_id: str,
        window: MetricsRange,
        *,
        site: str | None = None,
        place: str | None = None,
    ) -> TrafficSplitResponse:
        """Each connector's expected share now and observed share over ``window``, and with ``site`` its reputation
        and share of the requests there from ``place``. A site or place that names nothing is refused before the
        project is looked up."""
        try:
            wanted_site = report_site(site) if site is not None and site.strip() else None
            wanted_place = place_key(place) if wanted_site is not None else ""
        except ValueError as exc:
            raise InputRejectedError(str(exc)) from None
        preview = await self._preview.project(project_id)
        # The compile holds the project when it exists, so its absence is the 404.
        project = preview.get_project(project_id)
        if project is None:
            raise NotFoundError("Project not found")

        now = utc_now()
        delta, _limit, _bucket = RANGE_CONFIG[window]
        since = now - delta
        async with self._session_factory() as session:
            connectors = await ConnectorRepository(session).get_by_project(project_id)
            statuses = await ProxyRepository(session).count_by_connector(project_id)
            totals = await MetricsRepository(session).get_connector_totals_since(
                {c.id: since for c in connectors}
            )
            reputation = ExitReputationRepository(session)
            sites = await reputation.busiest_sites(project_id)
            places = await reputation.busiest_places(project_id, wanted_site) if wanted_site else []
            split = await ReputationSplit.read(
                session,
                project,
                preview,
                sites=[wanted_site] if wanted_site else [],
                levels={*place_levels(wanted_place), ""},
                now=now,
            )
        eligible_groups = split.groups
        # The preview's groups carry the configured weights: it reads no exit reputation.
        total_weight = int(sum(g.weight for g in eligible_groups.values()))
        shares = split.shares(wanted_site, wanted_place, [connector.id for connector in connectors])
        observed_total = sum(t["request_count"] for t in totals.values())
        observed_bytes_total = sum(t["bytes_sent"] + t["bytes_received"] for t in totals.values())
        # The rows each connector holds that still take requests, whatever their health.
        active: Counter[str] = Counter()
        for row in statuses:
            if not row.status.retiring:
                active[row.connector_id] += row.count

        connector_shares: list[ConnectorTrafficShare] = []
        for connector in sorted(connectors, key=lambda c: c.name.lower()):
            compiled = preview.snapshot.connector_view(connector.id)
            rows = (
                [p for p in compiled.proxies if not p.status.retiring]
                if compiled is not None
                else []
            )
            group = eligible_groups.get(connector.id)
            eligible = len(group.proxies) if group else 0
            expected = (group.weight / total_weight * 100) if group and total_weight else 0.0
            observed = totals.get(connector.id, {})
            requests = int(observed.get("request_count", 0))
            observed_bytes = int(observed.get("bytes_sent", 0)) + int(
                observed.get("bytes_received", 0)
            )
            traffic_config = connector.parsed_traffic_config
            share = ConnectorTrafficShare(
                connector_id=connector.id,
                name=connector.name,
                credential_type=connector.credential_type,
                enabled=connector.enabled,
                weight=connector.weight,
                dynamic=any(p.is_dynamic_gateway for p in rows),
                total_proxies=active[connector.id],
                eligible_proxies=eligible,
                expected_share=round(expected, 2),
                excluded_reason=split.excluded_reason(connector),
                observed_requests=requests,
                observed_share=round(requests / observed_total * 100, 2)
                if observed_total
                else None,
                observed_bytes=observed_bytes,
                cost=traffic_config.cost_of(observed_bytes),
                currency=traffic_config.currency
                if traffic_config.price_per_gb is not None
                else None,
            )
            share.weigh(shares[connector.id])
            connector_shares.append(share)

        return TrafficSplitResponse(
            strategy=preview.strategy_for_project(project_id).name,
            range=window,
            total_weight=total_weight,
            observed_requests=observed_total,
            observed_bytes=observed_bytes_total,
            connectors=connector_shares,
            site=wanted_site,
            place=wanted_place if wanted_site else None,
            reputation_applied=wanted_site is not None and split.applied,
            reputation_strength=split.rule.strength,
            reputation_floor=split.rule.floor,
            reputation_sites=[
                ReputationSiteOption(site=name, reports=reports) for name, reports in sites
            ],
            reputation_places=[
                ReputationPlaceOption(place=key, label=place_label(key), reports=reports)
                for key, reports in places
            ],
        )
