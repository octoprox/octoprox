"""Repository layer for database operations."""

from datetime import datetime

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.models import ProxyModel, SourceModel, ProxyMetricsModel
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.models.source import ProxySource, SourceType


class SourceRepository:
    """Repository for source database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[ProxySource]:
        """Get all sources."""
        result = await self._session.execute(select(SourceModel))
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_id(self, source_id: str) -> ProxySource | None:
        """Get source by ID."""
        result = await self._session.execute(
            select(SourceModel).where(SourceModel.id == source_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def create(self, source: ProxySource) -> ProxySource:
        """Create a new source."""
        model = SourceModel(
            id=source.id,
            name=source.name,
            type=source.type.value if isinstance(source.type, SourceType) else source.type,
            enabled=source.enabled,
            config=source.config,
            refresh_interval_seconds=source.refresh_interval_seconds,
            created_at=source.created_at,
            updated_at=source.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return source

    async def update(self, source: ProxySource) -> ProxySource:
        """Update an existing source."""
        result = await self._session.execute(
            select(SourceModel).where(SourceModel.id == source.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.name = source.name
            model.type = source.type.value if isinstance(source.type, SourceType) else source.type
            model.enabled = source.enabled
            model.config = source.config
            model.refresh_interval_seconds = source.refresh_interval_seconds
            model.updated_at = datetime.utcnow()
            await self._session.flush()
        return source

    async def delete(self, source_id: str) -> bool:
        """Delete a source."""
        result = await self._session.execute(
            delete(SourceModel).where(SourceModel.id == source_id)
        )
        return result.rowcount > 0

    def _to_domain(self, model: SourceModel) -> ProxySource:
        """Convert database model to domain model."""
        return ProxySource(
            id=model.id,
            name=model.name,
            type=SourceType(model.type),
            enabled=model.enabled,
            config=model.config,
            refresh_interval_seconds=model.refresh_interval_seconds,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class ProxyRepository:
    """Repository for proxy database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[Proxy]:
        """Get all proxies."""
        result = await self._session.execute(select(ProxyModel))
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_id(self, proxy_id: str) -> Proxy | None:
        """Get proxy by ID."""
        result = await self._session.execute(
            select(ProxyModel).where(ProxyModel.id == proxy_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_by_source(self, source_id: str) -> list[Proxy]:
        """Get all proxies for a source."""
        result = await self._session.execute(
            select(ProxyModel).where(ProxyModel.source_id == source_id)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def create(self, proxy: Proxy) -> Proxy:
        """Create a new proxy."""
        model = ProxyModel(
            id=proxy.id,
            host=proxy.host,
            port=proxy.port,
            protocol=proxy.protocol.value if isinstance(proxy.protocol, ProxyProtocol) else proxy.protocol,
            username=proxy.username,
            password=proxy.password,
            source_id=proxy.source_id,
            tags=proxy.tags,
            metadata_=proxy.metadata,
            created_at=proxy.created_at,
            updated_at=proxy.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return proxy

    async def update(self, proxy: Proxy) -> Proxy:
        """Update an existing proxy."""
        result = await self._session.execute(
            select(ProxyModel).where(ProxyModel.id == proxy.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.host = proxy.host
            model.port = proxy.port
            model.protocol = proxy.protocol.value if isinstance(proxy.protocol, ProxyProtocol) else proxy.protocol
            model.username = proxy.username
            model.password = proxy.password
            model.tags = proxy.tags
            model.metadata_ = proxy.metadata
            model.updated_at = datetime.utcnow()
            await self._session.flush()
        return proxy

    async def delete(self, proxy_id: str) -> bool:
        """Delete a proxy."""
        result = await self._session.execute(
            delete(ProxyModel).where(ProxyModel.id == proxy_id)
        )
        return result.rowcount > 0

    async def delete_by_source(self, source_id: str) -> int:
        """Delete all proxies for a source."""
        result = await self._session.execute(
            delete(ProxyModel).where(ProxyModel.source_id == source_id)
        )
        return result.rowcount

    def _to_domain(self, model: ProxyModel) -> Proxy:
        """Convert database model to domain model."""
        return Proxy(
            id=model.id,
            host=model.host,
            port=model.port,
            protocol=ProxyProtocol(model.protocol),
            username=model.username,
            password=model.password,
            source_id=model.source_id,
            tags=model.tags or [],
            metadata=model.metadata_ or {},
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class MetricsRepository:
    """Repository for metrics database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_metrics_snapshot(
        self,
        proxy_id: str,
        request_count: int,
        success_count: int,
        failure_count: int,
        avg_latency_ms: float,
        status: str,
    ) -> None:
        """Save a metrics snapshot to the database."""
        model = ProxyMetricsModel(
            proxy_id=proxy_id,
            timestamp=datetime.utcnow(),
            request_count=request_count,
            success_count=success_count,
            failure_count=failure_count,
            avg_latency_ms=avg_latency_ms,
            status=status,
        )
        self._session.add(model)
        await self._session.flush()

    async def get_metrics_history(
        self,
        proxy_id: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get historical metrics for a proxy."""
        query = select(ProxyMetricsModel).where(
            ProxyMetricsModel.proxy_id == proxy_id
        )
        if since:
            query = query.where(ProxyMetricsModel.timestamp >= since)
        query = query.order_by(ProxyMetricsModel.timestamp.desc()).limit(limit)

        result = await self._session.execute(query)
        models = result.scalars().all()
        return [
            {
                "timestamp": m.timestamp,
                "request_count": m.request_count,
                "success_count": m.success_count,
                "failure_count": m.failure_count,
                "avg_latency_ms": m.avg_latency_ms,
                "status": m.status,
            }
            for m in models
        ]

    async def get_cumulative_metrics_for_all_proxies(self) -> dict[str, dict]:
        """Get cumulative metrics (sum of all snapshots) for each proxy.

        Returns a dict mapping proxy_id to its total metrics across all snapshots.
        Used to restore metrics on startup - these totals should be combined with
        the current Redis window.
        """
        from sqlalchemy import func

        # Sum counts and compute weighted average for latency
        # weighted_avg = sum(avg_latency * request_count) / sum(request_count)
        query = (
            select(
                ProxyMetricsModel.proxy_id,
                func.sum(ProxyMetricsModel.request_count).label("total_requests"),
                func.sum(ProxyMetricsModel.success_count).label("total_successes"),
                func.sum(ProxyMetricsModel.failure_count).label("total_failures"),
                func.sum(
                    ProxyMetricsModel.avg_latency_ms * ProxyMetricsModel.request_count
                ).label("weighted_latency_sum"),
            )
            .group_by(ProxyMetricsModel.proxy_id)
        )

        result = await self._session.execute(query)
        rows = result.all()

        metrics = {}
        for row in rows:
            total_requests = row.total_requests or 0
            weighted_latency_sum = row.weighted_latency_sum or 0
            avg_latency = weighted_latency_sum / total_requests if total_requests > 0 else 0.0

            metrics[row.proxy_id] = {
                "request_count": total_requests,
                "success_count": row.total_successes or 0,
                "failure_count": row.total_failures or 0,
                "avg_latency_ms": avg_latency,
            }

        return metrics
