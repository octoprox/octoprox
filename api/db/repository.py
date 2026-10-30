# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Repository layer for database operations."""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    ColumnElement,
    DateTime,
    Integer,
    Select,
    String,
    Text,
    and_,
    bindparam,
    case,
    column,
    delete,
    exists,
    func,
    insert,
    literal,
    literal_column,
    null,
    or_,
    select,
    text,
    union_all,
    update,
    values,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from api.core import utc_now
from api.core.stats import (
    HOST_OVERFLOW,
    STEP_COLUMNS,
    FailureCountKey,
    MetricDelta,
    TunnelPeerMetricDelta,
)
from api.db.models import (
    LAST_ERROR_CHARS,
    ConnectorMetricsModel,
    ConnectorModel,
    CredentialModel,
    FailureMetricsModel,
    HostMetricsModel,
    MetricFlushModel,
    MetricTotalsModel,
    OpenVpnPeerModel,
    ProjectMetricsModel,
    ProjectModel,
    ProviderAuditModel,
    ProviderDescriptorModel,
    ProxyMetricsModel,
    ProxyModel,
    StepLatencyColumns,
    SystemMetricsModel,
    TunnelPeerMetricsModel,
    UserModel,
    WireGuardPeerModel,
)
from api.db.redis import PROJECT_WINDOWS, PROXY_WINDOWS, TUNNEL_PEER_WINDOWS, TotalledWindows
from api.geo.models import META_LOCATION_CHECKED_AT, ConflictRule, ContradictedAction, UnknownAction
from api.models.connector import Connector
from api.models.credential import Credential
from api.models.project import MitmBrowser, MitmEngine, MitmMode, Project
from api.models.provider import ProviderAuditEntry, ProviderRecord
from api.models.proxy import Proxy, ProxyProtocol, ProxyStatus
from api.models.user import User, UserRole


@dataclass
class ProjectCounts:
    """What one project holds, as the projects list shows it (``ProjectRepository.get_counts``)."""

    credentials: int = 0
    connectors: int = 0
    # Proxies per status (ProxyStatus values); a status no proxy has is absent.
    proxies_by_status: dict[str, int] = field(default_factory=dict)

    @property
    def proxies(self) -> int:
        """Every proxy of the project, whatever its status."""
        return sum(self.proxies_by_status.values())


@dataclass(frozen=True)
class ConnectorStatusCount:
    """How many proxies of one connector hold one status."""

    project_id: str
    connector_id: str
    status: ProxyStatus
    count: int


@dataclass(frozen=True)
class ProxyPlacement:
    """Where one proxy sits: its connector, and its status."""

    connector_id: str
    status: ProxyStatus


class ProjectRepository:
    """Repository for project database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[Project]:
        """Get all projects."""
        result = await self._session.execute(select(ProjectModel))
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_id(self, project_id: str) -> Project | None:
        """Get project by ID."""
        result = await self._session.execute(
            select(ProjectModel).where(ProjectModel.id == project_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_many(self, project_ids: Iterable[str]) -> list[Project]:
        """The projects among ``project_ids`` that exist, in id order; one statement however many are asked for."""
        wanted = sorted(set(project_ids))
        if not wanted:
            return []
        result = await self._session.execute(
            select(ProjectModel).where(ProjectModel.id.in_(wanted)).order_by(ProjectModel.id)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def create(self, project: Project) -> Project:
        """Create a new project."""
        model = ProjectModel(
            id=project.id,
            name=project.name,
            description=project.description,
            username=project.username,
            password=project.password,
            routing_strategy=project.routing_strategy,
            health_check_interval=project.health_check_interval,
            health_check_timeout=project.health_check_timeout,
            connection_timeout=project.connection_timeout,
            max_retries=project.max_retries,
            tls_mitm_mode=project.tls_mitm_mode,
            tls_mitm_engine=project.tls_mitm_engine,
            tls_mitm_browser=project.tls_mitm_browser,
            metrics_retention_days=project.metrics_retention_days,
            location_contradicted=project.location_contradicted.value,
            location_unknown=project.location_unknown.value,
            location_preflight=project.location_preflight,
            location_conflict_rule=project.location_conflict_rule.value if project.location_conflict_rule else None,
            location_city_radius_km=project.location_city_radius_km,
            exit_headers=project.exit_headers,
            observation_log=project.observation_log,
            observation_retention_days=project.observation_retention_days,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return project

    async def update(self, project: Project) -> Project | None:
        """Write a stored project as ``project`` has it; the row as stored, ``updated_at`` stamped, or None when it is gone."""
        result = await self._session.execute(
            select(ProjectModel).where(ProjectModel.id == project.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.name = project.name
            model.description = project.description
            model.username = project.username
            model.password = project.password
            model.routing_strategy = project.routing_strategy
            model.health_check_interval = project.health_check_interval
            model.health_check_timeout = project.health_check_timeout
            model.connection_timeout = project.connection_timeout
            model.max_retries = project.max_retries
            model.tls_mitm_mode = project.tls_mitm_mode
            model.tls_mitm_engine = project.tls_mitm_engine
            model.tls_mitm_browser = project.tls_mitm_browser
            model.metrics_retention_days = project.metrics_retention_days
            model.location_contradicted = project.location_contradicted.value
            model.location_unknown = project.location_unknown.value
            model.location_preflight = project.location_preflight
            model.location_conflict_rule = (
                project.location_conflict_rule.value if project.location_conflict_rule else None
            )
            model.location_city_radius_km = project.location_city_radius_km
            model.exit_headers = project.exit_headers
            model.observation_log = project.observation_log
            model.observation_retention_days = project.observation_retention_days
            model.updated_at = utc_now()
            await self._session.flush()
        return self._to_domain(model) if model else None

    async def delete(self, project_id: str) -> bool:
        """Delete a project (cascades to sources and proxies)."""
        result = await self._session.execute(
            delete(ProjectModel).where(ProjectModel.id == project_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    async def get_counts(self, project_id: str | None = None) -> dict[str, ProjectCounts]:
        """What each project holds (credentials, connectors, proxies by status), by project id, in one round trip.

        A project holding nothing is absent, and ``project_id`` narrows the counts to one project.
        """
        credentials = select(
            CredentialModel.project_id, literal("credentials").label("what"), null().label("status"), func.count()
        ).group_by(CredentialModel.project_id)
        connectors = select(
            ConnectorModel.project_id, literal("connectors"), null(), func.count()
        ).group_by(ConnectorModel.project_id)
        proxies = (
            select(ConnectorModel.project_id, literal("proxies"), ProxyModel.status, func.count())
            .join(ProxyModel, ProxyModel.connector_id == ConnectorModel.id)
            .group_by(ConnectorModel.project_id, ProxyModel.status)
        )
        if project_id is not None:
            credentials = credentials.where(CredentialModel.project_id == project_id)
            connectors = connectors.where(ConnectorModel.project_id == project_id)
            proxies = proxies.where(ConnectorModel.project_id == project_id)
        result: Result[Any, Any, Any, Any] = await self._session.execute(union_all(credentials, connectors, proxies))
        counts: dict[str, ProjectCounts] = {}
        for owner, what, status, count in result.all():
            entry = counts.setdefault(owner, ProjectCounts())
            if what == "proxies":
                entry.proxies_by_status[status] = count
            elif what == "credentials":
                entry.credentials = count
            else:
                entry.connectors = count
        return counts

    def _to_domain(self, model: ProjectModel) -> Project:
        """Convert database model to domain model."""
        return Project(
            id=model.id,
            name=model.name,
            description=model.description,
            username=model.username,
            password=model.password,
            routing_strategy=model.routing_strategy,
            health_check_interval=model.health_check_interval,
            health_check_timeout=model.health_check_timeout,
            connection_timeout=model.connection_timeout,
            max_retries=model.max_retries,
            tls_mitm_mode=MitmMode(model.tls_mitm_mode),
            tls_mitm_engine=MitmEngine(model.tls_mitm_engine) if model.tls_mitm_engine else None,
            tls_mitm_browser=MitmBrowser(model.tls_mitm_browser) if model.tls_mitm_browser else None,
            metrics_retention_days=model.metrics_retention_days,
            # Migration 043 wrote a valid value on every row; an unknown value is a bug, not a default.
            location_contradicted=ContradictedAction(model.location_contradicted or "evidence"),
            location_unknown=UnknownAction(model.location_unknown or "vendor"),
            location_preflight=bool(model.location_preflight),
            location_conflict_rule=ConflictRule(model.location_conflict_rule) if model.location_conflict_rule else None,
            location_city_radius_km=model.location_city_radius_km,
            exit_headers=bool(model.exit_headers),
            observation_log=bool(model.observation_log),
            observation_retention_days=model.observation_retention_days,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class CredentialRepository:
    """Repository for credential database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[Credential]:
        """Get all credentials, ordered by creation time."""
        result = await self._session.execute(
            select(CredentialModel).order_by(CredentialModel.created_at)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_project(self, project_id: str) -> list[Credential]:
        """Get all credentials for a project, ordered by creation time."""
        result = await self._session.execute(
            select(CredentialModel)
            .where(CredentialModel.project_id == project_id)
            .order_by(CredentialModel.created_at)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_id(self, credential_id: str) -> Credential | None:
        """Get credential by ID."""
        result = await self._session.execute(
            select(CredentialModel).where(CredentialModel.id == credential_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_in_project(self, project_id: str, credential_id: str) -> Credential | None:
        """The credential with this id if it belongs to the project, else None (a route under another project 404s)."""
        result = await self._session.execute(
            select(CredentialModel).where(
                CredentialModel.id == credential_id, CredentialModel.project_id == project_id
            )
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def usage_by_type(self, type_id: str | None = None) -> dict[str, tuple[int, int]]:
        """How many credentials each provider type has, and connectors on them; a type nothing uses is absent.

        ``type_id`` counts that type alone, in two grouped statements whatever the number of rows.
        """
        credentials = select(CredentialModel.type, func.count(CredentialModel.id)).group_by(CredentialModel.type)
        connectors = (
            select(CredentialModel.type, func.count(ConnectorModel.id))
            .join(ConnectorModel, ConnectorModel.credential_id == CredentialModel.id)
            .group_by(CredentialModel.type)
        )
        if type_id is not None:
            credentials = credentials.where(CredentialModel.type == type_id)
            connectors = connectors.where(CredentialModel.type == type_id)
        credential_counts: dict[str, int] = dict((await self._session.execute(credentials)).all())
        connector_counts: dict[str, int] = dict((await self._session.execute(connectors)).all())
        return {ptype: (count, connector_counts.get(ptype, 0)) for ptype, count in credential_counts.items()}

    async def create(self, credential: Credential) -> Credential:
        """Create a new credential."""
        model = CredentialModel(
            id=credential.id,
            name=credential.name,
            type=credential.type,
            project_id=credential.project_id,
            config=credential.config,
            created_at=credential.created_at,
            updated_at=credential.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return credential

    async def update(self, credential: Credential) -> Credential | None:
        """Write a stored credential's name and config as ``credential`` has them; the row as stored, ``updated_at`` stamped, or None when it is gone."""
        result = await self._session.execute(
            select(CredentialModel).where(CredentialModel.id == credential.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.name = credential.name
            model.config = credential.config
            model.updated_at = utc_now()
            await self._session.flush()
        return self._to_domain(model) if model else None

    async def delete(self, credential_id: str) -> bool:
        """Delete a credential."""
        result = await self._session.execute(
            delete(CredentialModel).where(CredentialModel.id == credential_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    def _to_domain(self, model: CredentialModel) -> Credential:
        """Convert database model to domain model."""
        return Credential(
            id=model.id,
            name=model.name,
            type=model.type,
            project_id=model.project_id,
            config=model.config,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class ConnectorRepository:
    """Repository for connector database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[Connector]:
        """Get all connectors, ordered by creation time."""
        result = await self._session.execute(
            select(ConnectorModel)
            .options(selectinload(ConnectorModel.credential))
            .order_by(ConnectorModel.created_at)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_project(self, project_id: str) -> list[Connector]:
        """Get all connectors for a project, ordered by creation time."""
        result = await self._session.execute(
            select(ConnectorModel)
            .where(ConnectorModel.project_id == project_id)
            .options(selectinload(ConnectorModel.credential))
            .order_by(ConnectorModel.created_at)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_id(self, connector_id: str) -> Connector | None:
        """Get connector by ID."""
        result = await self._session.execute(
            select(ConnectorModel)
            .where(ConnectorModel.id == connector_id)
            .options(selectinload(ConnectorModel.credential))
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_in_project(self, project_id: str, connector_id: str) -> Connector | None:
        """The connector with this id if it belongs to the project, else None (a route under another project 404s)."""
        result = await self._session.execute(
            select(ConnectorModel)
            .where(ConnectorModel.id == connector_id, ConnectorModel.project_id == project_id)
            .options(selectinload(ConnectorModel.credential))
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_with_credential(
        self, connector_id: str, *, project_id: str | None = None
    ) -> tuple[Connector, Credential] | None:
        """A connector with its credential, joined in one statement; None when it is gone, or not in ``project_id`` when given."""
        query = (
            select(ConnectorModel)
            .where(ConnectorModel.id == connector_id)
            .options(joinedload(ConnectorModel.credential))
        )
        if project_id is not None:
            query = query.where(ConnectorModel.project_id == project_id)
        model = (await self._session.execute(query)).scalar_one_or_none()
        if model is None:
            return None
        return self._to_domain(model), CredentialRepository(self._session)._to_domain(model.credential)

    async def get_many(self, connector_ids: Iterable[str]) -> list[Connector]:
        """The connectors among ``connector_ids`` that exist, in id order; one statement however many are asked for."""
        return [connector for connector, _credential in await self.get_many_with_credentials(connector_ids)]

    async def get_many_with_credentials(self, connector_ids: Iterable[str]) -> list[tuple[Connector, Credential]]:
        """Each of ``connector_ids`` that exists with its credential, in id order, joined in one statement."""
        wanted = sorted(set(connector_ids))
        if not wanted:
            return []
        result = await self._session.execute(
            select(ConnectorModel)
            .where(ConnectorModel.id.in_(wanted))
            .options(joinedload(ConnectorModel.credential))
            .order_by(ConnectorModel.id)
        )
        credentials = CredentialRepository(self._session)
        return [
            (self._to_domain(model), credentials._to_domain(model.credential))
            for model in result.scalars().unique().all()
        ]

    async def create(self, connector: Connector) -> Connector:
        """Create a new connector."""
        model = ConnectorModel(
            id=connector.id,
            name=connector.name,
            credential_id=connector.credential_id,
            project_id=connector.project_id,
            config=connector.config,
            routing_config=connector.routing_config,
            rate_limit_config=connector.rate_limit_config,
            traffic_config=connector.traffic_config,
            traffic_reset_at=connector.traffic_reset_at,
            enabled=connector.enabled,
            pending_deletion=connector.pending_deletion,
            last_error=connector.last_error,
            last_error_at=connector.last_error_at,
            consecutive_errors=connector.consecutive_errors,
            created_at=connector.created_at,
            updated_at=connector.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return connector

    async def update(self, connector: Connector) -> Connector | None:
        """Write a stored connector as ``connector`` has it; the row as stored, ``updated_at`` stamped, or None when it is gone."""
        result = await self._session.execute(
            select(ConnectorModel).where(ConnectorModel.id == connector.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.name = connector.name
            model.credential_id = connector.credential_id
            model.config = connector.config
            model.routing_config = connector.routing_config
            model.rate_limit_config = connector.rate_limit_config
            model.traffic_config = connector.traffic_config
            model.traffic_reset_at = connector.traffic_reset_at
            model.enabled = connector.enabled
            model.pending_deletion = connector.pending_deletion
            model.last_error = connector.last_error
            model.last_error_at = connector.last_error_at
            model.consecutive_errors = connector.consecutive_errors
            model.updated_at = utc_now()
            await self._session.flush()
            # Read again with its credential, which the edit may have moved to another.
            self._session.expire(model)
        return await self.get_by_id(connector.id) if model else None

    async def delete(self, connector_id: str) -> bool:
        """Delete a connector."""
        result = await self._session.execute(
            delete(ConnectorModel).where(ConnectorModel.id == connector_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    async def record_error(self, connector_id: str, error: str | None) -> bool:
        """Record a cloud error, ``consecutive_errors`` counted in SQL, or clear the error state for None; False when nothing was written."""
        values: dict[str, Any]
        statement = update(ConnectorModel).where(ConnectorModel.id == connector_id)
        if error is None:
            statement = statement.where(
                or_(
                    ConnectorModel.last_error.is_not(None),
                    ConnectorModel.last_error_at.is_not(None),
                    ConnectorModel.consecutive_errors != 0,
                )
            )
            values = {"last_error": None, "last_error_at": None, "consecutive_errors": 0}
        else:
            values = {
                # Cut to the column, since a cloud SDK's error can run longer.
                "last_error": error[:LAST_ERROR_CHARS],
                "last_error_at": utc_now(),
                "consecutive_errors": ConnectorModel.consecutive_errors + 1,
            }
        # The error columns alone, updated_at as it was: an error is not an edit.
        result = await self._session.execute(
            statement.values(**values, updated_at=ConnectorModel.updated_at).execution_options(
                synchronize_session=False
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    def _to_domain(self, model: ConnectorModel) -> Connector:
        """Convert database model to domain model."""
        return Connector(
            id=model.id,
            name=model.name,
            credential_id=model.credential_id,
            credential_type=model.credential.type,
            project_id=model.project_id,
            config=model.config,
            routing_config=model.routing_config or {},
            rate_limit_config=model.rate_limit_config or {},
            traffic_config=model.traffic_config or {},
            traffic_reset_at=model.traffic_reset_at,
            enabled=model.enabled,
            pending_deletion=model.pending_deletion,
            last_error=model.last_error,
            last_error_at=model.last_error_at,
            consecutive_errors=model.consecutive_errors,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


# The retiring statuses, which only drain and terminate set; the health job's compare-and-set never leaves them.
_LIFECYCLE_STATUSES = tuple(status.value for status in ProxyStatus if status.retiring)

# Ids bound per statement when placing proxies: well under Postgres's limit
# of 32767 bind parameters, and a quarantine set rarely holds more than a few
# hundred.
_PLACEMENT_BATCH = 2000

# The forward-only metadata patch (``ProxyRepository.patch_metadata``). The
# column is json, so the merge goes through jsonb and back. Stamps compare as
# naive UTC timestamps, the way every timestamp here is written: cast to
# timestamptz they would be read in the server's time zone, and a naive
# bound value in the client's.
_PATCH_METADATA = text(
    "UPDATE proxies "
    "SET metadata = ((metadata::jsonb - CAST(:removed AS text[])) || CAST(:changed AS jsonb))::json, "
    "updated_at = :now "
    "WHERE id = :id "
    "AND (CAST(:checked_at AS timestamp) IS NULL "
    f"OR metadata->>'{META_LOCATION_CHECKED_AT}' IS NULL "
    f"OR (metadata->>'{META_LOCATION_CHECKED_AT}')::timestamp <= :checked_at)"
).bindparams(
    bindparam("removed", type_=ARRAY(Text)),
    bindparam("changed", type_=JSONB),
    bindparam("checked_at", type_=DateTime()),
    bindparam("now", type_=DateTime()),
)


class ProxyRepository:
    """Repository for proxy database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _select() -> Select[ProxyModel]:
        """Proxy rows as stored, refreshing any the session already holds.

        ``set_status_if``, ``set_status`` and ``patch_metadata`` write with
        statements the session does not track, so a row it loaded earlier
        would otherwise come back as it was before them.
        """
        return select(ProxyModel).execution_options(populate_existing=True)

    async def get_all(self) -> list[Proxy]:
        """Get all proxies, in creation order."""
        result = await self._session.execute(
            self._select().order_by(ProxyModel.created_at, ProxyModel.id)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_id(self, proxy_id: str) -> Proxy | None:
        """Get proxy by ID."""
        result = await self._session.execute(
            self._select().where(ProxyModel.id == proxy_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_many(self, proxy_ids: Iterable[str]) -> list[Proxy]:
        """The proxies among ``proxy_ids`` that exist, in id order; one statement however many are asked for."""
        wanted = sorted(set(proxy_ids))
        if not wanted:
            return []
        result = await self._session.execute(
            self._select().where(ProxyModel.id.in_(wanted)).order_by(ProxyModel.id)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def get_many_with_connectors(self, proxy_ids: Iterable[str]) -> list[tuple[Proxy, Connector, Credential]]:
        """Each of ``proxy_ids`` that exists with its connector and their credential, in id order, in one statement."""
        wanted = sorted(set(proxy_ids))
        if not wanted:
            return []
        result = await self._session.execute(
            self._select()
            .where(ProxyModel.id.in_(wanted))
            .options(joinedload(ProxyModel.connector).joinedload(ConnectorModel.credential))
            .order_by(ProxyModel.id)
        )
        connectors, credentials = ConnectorRepository(self._session), CredentialRepository(self._session)
        return [
            (
                self._to_domain(model),
                connectors._to_domain(model.connector),
                credentials._to_domain(model.connector.credential),
            )
            for model in result.scalars().unique().all()
        ]

    async def page(
        self, after: str | None, limit: int, *, connector_id: str | None = None, project_id: str | None = None
    ) -> list[Proxy]:
        """The next ``limit`` proxies after id ``after``, in id order: a connector's, else a project's, else any.

        Keyset paging, so a pass over a large pool costs a short read per
        page however far it has got.
        """
        statement = self._select().order_by(ProxyModel.id).limit(limit)
        if after is not None:
            statement = statement.where(ProxyModel.id > after)
        if connector_id is not None:
            statement = statement.where(ProxyModel.connector_id == connector_id)
        elif project_id is not None:
            statement = statement.join(ConnectorModel, ProxyModel.connector_id == ConnectorModel.id).where(
                ConnectorModel.project_id == project_id
            )
        result = await self._session.execute(statement)
        return [self._to_domain(m) for m in result.scalars().all()]

    async def get_by_connector(self, connector_id: str) -> list[Proxy]:
        """Get all proxies for a connector, in creation order."""
        result = await self._session.execute(
            self._select()
            .where(ProxyModel.connector_id == connector_id)
            .order_by(ProxyModel.created_at, ProxyModel.id)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_project(self, project_id: str) -> list[Proxy]:
        """Every proxy of the project's connectors, in creation order."""
        result = await self._session.execute(
            self._select()
            .join(ConnectorModel, ProxyModel.connector_id == ConnectorModel.id)
            .where(ConnectorModel.project_id == project_id)
            .order_by(ProxyModel.created_at, ProxyModel.id)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def get_in_project(self, project_id: str, proxy_id: str, *, for_update: bool = False) -> Proxy | None:
        """The proxy with this id if one of the project's connectors holds it, else None.

        ``for_update`` locks the row until the transaction ends, so an edit built from this read writes back nothing stale.
        """
        statement = (
            self._select()
            .join(ConnectorModel, ProxyModel.connector_id == ConnectorModel.id)
            .where(ProxyModel.id == proxy_id, ConnectorModel.project_id == project_id)
        )
        if for_update:
            statement = statement.with_for_update(of=ProxyModel)
        result = await self._session.execute(statement)
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def create(self, proxy: Proxy) -> Proxy:
        """Create a new proxy, stored with the status its builder gave it."""
        self._session.add(ProxyModel(**self._row(proxy)))
        await self._session.flush()
        return proxy

    async def bulk_create(self, proxies: Sequence[Proxy]) -> list[Proxy]:
        """Insert many proxies in one statement, each with its status; the ones inserted, a proxy whose ID is stored skipped.

        So a row whose ID derives from what it stands for, such as a cloud
        instance, lands once whoever saves it.
        """
        if not proxies:
            return []
        result = await self._session.execute(
            pg_insert(ProxyModel).on_conflict_do_nothing(index_elements=[ProxyModel.id]).returning(ProxyModel.id),
            [self._row(proxy) for proxy in proxies],
        )
        inserted = set(result.scalars())
        return [proxy for proxy in proxies if proxy.id in inserted]

    async def update(self, proxy: Proxy) -> Proxy:
        """Write every definition column of a stored proxy as ``proxy`` has it, metadata included, but never its status.

        An edit built from an earlier read must have locked the row with it (``get_in_project(..., for_update=True)``),
        or what another writer committed in between is written back over.
        """
        result = await self._session.execute(
            self._select().where(ProxyModel.id == proxy.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.host = proxy.host
            model.port = proxy.port
            model.protocol = proxy.protocol.value if isinstance(proxy.protocol, ProxyProtocol) else proxy.protocol
            model.username = proxy.username
            model.password = proxy.password
            model.display_host = proxy.display_host
            model.tags = proxy.tags
            model.metadata_ = proxy.metadata
            model.updated_at = utc_now()
            await self._session.flush()
        return proxy

    async def delete(self, proxy_id: str) -> bool:
        """Delete a proxy."""
        result = await self._session.execute(
            delete(ProxyModel).where(ProxyModel.id == proxy_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    async def set_status_if(self, proxy_id: str, new: ProxyStatus, expected: ProxyStatus) -> bool:
        """Move a proxy from ``expected`` to ``new``; False when it holds another status by now, or is gone.

        The health job's compare-and-set, which never leaves draining or terminating, with ``updated_at`` pinned since a
        health flip is not an edit.
        """
        result = await self._session.execute(
            update(ProxyModel)
            .where(
                ProxyModel.id == proxy_id,
                ProxyModel.status == expected.value,
                ProxyModel.status.not_in(_LIFECYCLE_STATUSES),
            )
            .values(status=new.value, status_changed_at=utc_now(), updated_at=ProxyModel.updated_at)
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def set_status(self, proxy_id: str, status: ProxyStatus) -> bool:
        """Put a proxy in ``status`` (drain and terminate); False when gone, already there, or past it.

        The lifecycle only moves forward, a row already in the status is left alone, and ``updated_at`` is pinned, as in
        ``set_status_if``.
        """
        past = _LIFECYCLE_STATUSES if status == ProxyStatus.DRAINING else (status.value,)
        result = await self._session.execute(
            update(ProxyModel)
            .where(ProxyModel.id == proxy_id, ProxyModel.status.not_in(past))
            .values(status=status.value, status_changed_at=utc_now(), updated_at=ProxyModel.updated_at)
            .execution_options(synchronize_session=False)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def patch_metadata(
        self,
        proxy_id: str,
        changed: Mapping[str, Any],
        removed: Collection[str],
        checked_at: datetime | None,
    ) -> bool:
        """Merge ``changed`` into a proxy's metadata and drop the ``removed`` keys; False when refused or gone.

        Only those keys are written, and no row lock is held.

        Args:
            checked_at: Naive UTC; applies only over an unset or older ``location_checked_at``; None writes regardless.
        """
        result = await self._session.execute(
            _PATCH_METADATA,
            {
                "id": proxy_id,
                "changed": dict(changed),
                "removed": sorted(removed),
                "checked_at": checked_at,
                "now": utc_now(),
            },
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    # Counts and placements over the pool, one grouped statement each.

    async def count_by_status(self, project_id: str | None = None) -> dict[str, int]:
        """Proxies per status (``ProxyStatus`` values), install-wide or of one project; an unused status is absent."""
        query = select(ProxyModel.status, func.count()).group_by(ProxyModel.status)
        if project_id is not None:
            query = query.join(ConnectorModel, ProxyModel.connector_id == ConnectorModel.id).where(
                ConnectorModel.project_id == project_id
            )
        result = await self._session.execute(query)
        return dict(result.all())

    async def count_by_connector(
        self, project_id: str | None = None, *, connector_id: str | None = None
    ) -> list[ConnectorStatusCount]:
        """Proxies per connector and status: install-wide, for one project, or for one connector.

        The finer cut of the pool counts: callers sum it over whichever connectors they count (the enabled ones, the
        cloud ones), so a disabled or traffic-blocked connector is left out without a second query.
        """
        query = (
            select(ConnectorModel.project_id, ProxyModel.connector_id, ProxyModel.status, func.count())
            .join(ConnectorModel, ProxyModel.connector_id == ConnectorModel.id)
            .group_by(ConnectorModel.project_id, ProxyModel.connector_id, ProxyModel.status)
        )
        if project_id is not None:
            query = query.where(ConnectorModel.project_id == project_id)
        if connector_id is not None:
            query = query.where(ProxyModel.connector_id == connector_id)
        result = await self._session.execute(query)
        return [
            ConnectorStatusCount(project_id=owner, connector_id=connector_id, status=ProxyStatus(status), count=count)
            for owner, connector_id, status, count in result.all()
        ]

    async def metadata_values(self, connector_ids: Iterable[str], key: str) -> dict[str, set[str]]:
        """The values metadata ``key`` takes on each connector's rows; a connector none of whose rows has it is absent.

        One statement that returns a row per connector and value, however many rows share it: a 10,000-slot pool with
        groups in five countries sends five.
        """
        wanted = sorted(set(connector_ids))
        if not wanted:
            return {}
        value = ProxyModel.metadata_[key].as_string()
        result = await self._session.execute(
            select(ProxyModel.connector_id, value)
            .where(ProxyModel.connector_id.in_(wanted), value.is_not(None))
            .distinct()
        )
        found: dict[str, set[str]] = {}
        for connector_id, taken in result.all():
            found.setdefault(connector_id, set()).add(taken)
        return found

    async def placements(self, proxy_ids: Iterable[str], project_id: str | None = None) -> dict[str, ProxyPlacement]:
        """The connector and status of each of ``proxy_ids`` that exists (in the project, when one is given)."""
        wanted = sorted(set(proxy_ids))
        found: dict[str, ProxyPlacement] = {}
        for start in range(0, len(wanted), _PLACEMENT_BATCH):
            query = select(ProxyModel.id, ProxyModel.connector_id, ProxyModel.status).where(
                ProxyModel.id.in_(wanted[start:start + _PLACEMENT_BATCH])
            )
            if project_id is not None:
                query = query.join(ConnectorModel, ProxyModel.connector_id == ConnectorModel.id).where(
                    ConnectorModel.project_id == project_id
                )
            result = await self._session.execute(query)
            found.update(
                (proxy_id, ProxyPlacement(connector_id=connector_id, status=ProxyStatus(status)))
                for proxy_id, connector_id, status in result.all()
            )
        return found

    @staticmethod
    def _row(proxy: Proxy) -> dict[str, Any]:
        """A new row's columns (by attribute name), the builder's status included."""
        return {
            "id": proxy.id,
            "host": proxy.host,
            "port": proxy.port,
            "protocol": ProxyProtocol(proxy.protocol).value,
            "username": proxy.username,
            "password": proxy.password,
            "display_host": proxy.display_host,
            "connector_id": proxy.connector_id,
            "tags": proxy.tags,
            "metadata_": proxy.metadata,
            "status": ProxyStatus(proxy.status).value,
            "status_changed_at": proxy.status_changed_at,
            "created_at": proxy.created_at,
            "updated_at": proxy.updated_at,
        }

    def _to_domain(self, model: ProxyModel) -> Proxy:
        """Convert database model to domain model."""
        return Proxy(
            id=model.id,
            host=model.host,
            port=model.port,
            protocol=ProxyProtocol(model.protocol),
            username=model.username,
            password=model.password,
            display_host=model.display_host,
            status=ProxyStatus(model.status),
            status_changed_at=model.status_changed_at,
            connector_id=model.connector_id,
            tags=model.tags or [],
            metadata=model.metadata_ or {},
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


def _strip_tz(dt: datetime) -> datetime:
    """Strip timezone info from a datetime for naive-datetime DB columns.

    PostgreSQL's ``to_timestamp()`` returns ``TIMESTAMP WITH TIME ZONE``,
    but our columns use ``TIMESTAMP WITHOUT TIME ZONE``.
    """
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


_MetricsModel = (
    type[ProjectMetricsModel]
    | type[ProxyMetricsModel]
    | type[ConnectorMetricsModel]
    | type[TunnelPeerMetricsModel]
    | type[HostMetricsModel]
)

# How many parent ids one existence lookup of a batch insert carries: well
# under asyncpg's 32767 bind parameters per statement.
_PARENT_LOOKUP_BATCH = 10_000

# Where each tunnel protocol keeps its devices: the table a device id is looked
# up in when its metrics are flushed, and whose ``project_id`` the history row
# copies. A new tunnel protocol registers its table here and gets history,
# compaction and retention for its devices with nothing else to write.
TUNNEL_PEER_TABLES: dict[str, type[WireGuardPeerModel] | type[OpenVpnPeerModel]] = {
    "wireguard": WireGuardPeerModel,
    "openvpn": OpenVpnPeerModel,
}


def _bucket_expressions(
    model: _MetricsModel | type[SystemMetricsModel] | type[FailureMetricsModel],
    bucket_seconds: int,
) -> tuple[Any, Any]:
    """Build bucket-epoch and bucket-timestamp expressions for time bucketing.

    Returns (bucket_epoch, bucket_ts) where bucket_epoch is a numeric
    expression suitable for GROUP BY and bucket_ts is a timestamp label.
    """
    bucket = text(str(bucket_seconds))
    ts_col = func.extract("epoch", model.timestamp)
    bucket_epoch = func.floor(ts_col / bucket) * bucket
    bucket_ts = func.to_timestamp(bucket_epoch).label("bucket_ts")
    return bucket_epoch, bucket_ts


def _success_weighted(column: Any, success_count: Any) -> Any:
    """The average of a per-row average ``column`` over the rows' successful requests; NULL where none succeeded.

    A row's NULL (a step average from before steps were kept) leaves the row out, its successes too.
    """
    weight = func.sum(success_count).filter(column.is_not(None))
    return func.sum(column * success_count) / func.nullif(weight, 0)


def _metrics_aggregate_columns(model: _MetricsModel) -> list[Any]:
    """Return the standard aggregate SELECT columns for a metrics model.

    Columns: request_count, success_count, failure_count, avg_latency_ms
    (weighted by successes), bytes_sent, bytes_received, and each step's
    average where the model keeps them (``StepLatencyColumns``).
    """
    columns = [
        func.sum(model.request_count).label("request_count"),
        func.sum(model.success_count).label("success_count"),
        func.sum(model.failure_count).label("failure_count"),
        _success_weighted(model.avg_latency_ms, model.success_count).label("avg_latency_ms"),
        func.sum(model.bytes_sent).label("bytes_sent"),
        func.sum(model.bytes_received).label("bytes_received"),
    ]
    if issubclass(model, StepLatencyColumns):
        columns += [
            _success_weighted(getattr(model, name), model.success_count).label(name) for name in STEP_COLUMNS.values()
        ]
    return columns


def _tunnel_peer_aggregate_columns() -> list[Any]:
    """The standard aggregates plus the two per-device name-resolution counters."""
    model = TunnelPeerMetricsModel
    return [
        *_metrics_aggregate_columns(model),
        func.sum(model.by_address).label("by_address"),
        func.sum(model.encrypted_dns_blocked).label("encrypted_dns_blocked"),
    ]


def _history_row(timestamp: datetime | None, row: Any) -> dict[str, Any]:
    """The six standard counters of an aggregate row, with its timestamp when it has one."""
    point: dict[str, Any] = {
        "request_count": int(row.request_count or 0),
        "success_count": int(row.success_count or 0),
        "failure_count": int(row.failure_count or 0),
        "avg_latency_ms": float(row.avg_latency_ms or 0),
        "bytes_sent": int(row.bytes_sent or 0),
        "bytes_received": int(row.bytes_received or 0),
    }
    if timestamp is not None:
        point["timestamp"] = timestamp
    return point


def _latency_history_row(timestamp: datetime, row: Any) -> dict[str, Any]:
    """A project's or connector's history point: the standard counters, the latency None where nothing succeeded, and
    each step's average under ``step_latency_ms``, None for a point from before steps were kept.
    """
    point = _history_row(timestamp, row)
    succeeded = point["success_count"] > 0
    steps = {step.value: getattr(row, name) for step, name in STEP_COLUMNS.items()}
    point["avg_latency_ms"] = point["avg_latency_ms"] if succeeded else None
    point["step_latency_ms"] = (
        {name: float(value) for name, value in steps.items()}
        if succeeded and all(value is not None for value in steps.values())
        else None
    )
    return point


def _tunnel_peer_row(timestamp: datetime, row: Any) -> dict[str, Any]:
    """One history point of a device, from a raw row or an aggregate row."""
    return {
        **_history_row(timestamp, row),
        "connections_by_address": int(row.by_address or 0),
        "encrypted_dns_blocked": int(row.encrypted_dns_blocked or 0),
    }


# Ids per totals lookup and rows per totals upsert. An upsert row binds 11
# values, so a statement stays well under asyncpg's 32767 bound parameters.
_TOTALS_BATCH = 2000

# The additive columns of metric_totals: the fields of TunnelPeerMetricDelta,
# the shape every totals row is written in.
_TOTALS_COUNTERS = tuple(TunnelPeerMetricDelta().to_dict())

# Every total recomputed from the history: each entity's rows summed, latency
# back as a sum over the successful requests so later windows add to it,
# stamped with its newest row's time. The history columns are nullable and the totals' are not, so a
# sum over nothing but NULLs counts as 0. Migration 048 filled the table with
# a frozen copy of this.
_REBUILD_TOTALS = text(
    """
    INSERT INTO metric_totals (
        kind, entity_id, request_count, success_count, failure_count, latency_sum_ms,
        bytes_sent, bytes_received, by_address, encrypted_dns_blocked, updated_at
    )
    SELECT 'proxy', proxy_id, coalesce(sum(request_count), 0), coalesce(sum(success_count), 0),
           coalesce(sum(failure_count), 0), coalesce(sum(avg_latency_ms * success_count), 0),
           coalesce(sum(bytes_sent), 0), coalesce(sum(bytes_received), 0), 0, 0,
           coalesce(max(timestamp), timezone('UTC', now()))
    FROM proxy_metrics GROUP BY proxy_id
    UNION ALL
    SELECT 'project', project_id, coalesce(sum(request_count), 0), coalesce(sum(success_count), 0),
           coalesce(sum(failure_count), 0), coalesce(sum(avg_latency_ms * success_count), 0),
           coalesce(sum(bytes_sent), 0), coalesce(sum(bytes_received), 0), 0, 0,
           coalesce(max(timestamp), timezone('UTC', now()))
    FROM project_metrics GROUP BY project_id
    UNION ALL
    SELECT 'tunnel_peer', peer_id, coalesce(sum(request_count), 0), coalesce(sum(success_count), 0),
           coalesce(sum(failure_count), 0), coalesce(sum(avg_latency_ms * success_count), 0),
           coalesce(sum(bytes_sent), 0), coalesce(sum(bytes_received), 0),
           coalesce(sum(by_address), 0), coalesce(sum(encrypted_dns_blocked), 0),
           coalesce(max(timestamp), timezone('UTC', now()))
    FROM tunnel_peer_metrics GROUP BY peer_id
    """
)


# The counters a device's history row carries, each named after the
# TunnelPeerMetricDelta field (or property, for the average) it holds.
_DEVICE_COUNTERS = (
    "request_count",
    "success_count",
    "failure_count",
    "avg_latency_ms",
    "bytes_sent",
    "bytes_received",
    "by_address",
    "encrypted_dns_blocked",
)

# Devices per history insert. A row binds nine values, so a statement stays
# well under asyncpg's 32767 bound parameters; installs have tens to hundreds
# of devices.
_DEVICE_BATCH = 2000

# The counters a request history table adds up when it compacts, besides the
# average latency over the successful requests every one of them carries.
_REQUEST_COUNTERS = ("request_count", "success_count", "failure_count", "bytes_sent", "bytes_received")


class MetricsRepository:
    """Repository for metrics database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_proxy_metrics_snapshots(
        self, batch: dict[str, MetricDelta], statuses: dict[str, str]
    ) -> list[str]:
        """Insert one row per proxy of a flush window; returns the ids dropped as gone.

        ``statuses`` holds each proxy's health status; one without is recorded
        as ``unknown``.
        """
        extra = {proxy_id: {"status": statuses.get(proxy_id, "unknown")} for proxy_id in batch}
        return await self._insert_snapshots(ProxyMetricsModel, ProxyModel, "proxy_id", batch, extra)

    async def save_project_metrics_snapshots(self, batch: Mapping[str, MetricDelta]) -> list[str]:
        """Insert one row per project of a flush window; returns the ids dropped as gone."""
        return await self._insert_snapshots(ProjectMetricsModel, ProjectModel, "project_id", batch)

    async def save_connector_metrics_snapshots(self, batch: Mapping[str, MetricDelta]) -> list[str]:
        """Insert one row per connector of a flush window; returns the ids dropped as gone."""
        return await self._insert_snapshots(ConnectorMetricsModel, ConnectorModel, "connector_id", batch)

    async def _insert_snapshots(
        self,
        model: _MetricsModel,
        parent: type[ProxyModel] | type[ProjectModel] | type[ConnectorModel],
        key: str,
        batch: Mapping[str, MetricDelta],
        extra: dict[str, dict[str, Any]] | None = None,
    ) -> list[str]:
        """Insert a row per entity of ``batch`` whose parent row still exists; returns the ids dropped.

        One lookup of the parents under ``FOR KEY SHARE``, then a single multi-row insert, so no parent goes in between.
        """
        if not batch:
            return []
        ids = list(batch)
        known: set[str] = set()
        for start in range(0, len(ids), _PARENT_LOOKUP_BATCH):
            result = await self._session.execute(
                select(parent.id)
                .where(parent.id.in_(ids[start:start + _PARENT_LOOKUP_BATCH]))
                .with_for_update(read=True, key_share=True)
            )
            known.update(result.scalars().all())
        now = utc_now()
        rows = [
            {
                key: entity_id,
                "timestamp": now,
                "request_count": delta.request_count,
                "success_count": delta.success_count,
                "failure_count": delta.failure_count,
                **delta.history_averages(),
                "bytes_sent": delta.bytes_sent,
                "bytes_received": delta.bytes_received,
                "granularity": 60,
                **(extra or {}).get(entity_id, {}),
            }
            for entity_id, delta in batch.items()
            if entity_id in known
        ]
        if rows:
            await self._session.execute(insert(model), rows)
        return [entity_id for entity_id in ids if entity_id not in known]

    async def _compact(
        self,
        model: _MetricsModel | type[FailureMetricsModel],
        scope: ColumnElement[bool],
        older_than: datetime,
        source_granularity: int,
        target_granularity: int,
        *,
        keys: Sequence[str],
        sums: Sequence[str] = _REQUEST_COUNTERS,
        latency: bool = True,
        latest: str | None = None,
    ) -> int:
        """Fold the rows ``scope`` selects at the source granularity older than ``older_than`` into a row per key and bucket; the rows folded.

        One statement that inserts the aggregate of exactly the rows it deleted, so two passes at once never fold a row twice.

        Args:
            keys: the columns a series is kept under, besides its bucket of the target granularity.
            sums: the counters added up.
            latency: whether the rows carry ``avg_latency_ms``, averaged by their successful requests, and the step
                averages where the model keeps them, alike.
            latest: a column each bucket takes from its newest row.
        """
        table = model.__table__
        averages = (
            ["avg_latency_ms", *(STEP_COLUMNS.values() if issubclass(model, StepLatencyColumns) else ())]
            if latency
            else []
        )
        returned = [*keys, "timestamp", *sums, *averages, *([latest] if latest else [])]
        moved = (
            delete(model)
            .where(scope, model.granularity == source_granularity, model.timestamp < older_than)
            .returning(*(table.c[name] for name in returned))
            .cte("moved")
        )
        width = literal_column(str(target_granularity), Integer())
        bucket = func.floor(func.extract("epoch", moved.c.timestamp) / width) * width
        columns: dict[str, Any] = {name: moved.c[name] for name in keys}
        # The bucket's start, as the naive UTC the column holds.
        columns["timestamp"] = func.timezone("UTC", func.to_timestamp(bucket))
        columns.update((name, func.sum(moved.c[name])) for name in sums)
        for name in averages:
            average = _success_weighted(moved.c[name], moved.c.success_count)
            # The latency column is not nullable; a step's NULL stays one, as in the rows folded.
            columns[name] = func.coalesce(average, 0) if name == "avg_latency_ms" else average
        if latest:
            columns[latest] = func.array_agg(aggregate_order_by(moved.c[latest], moved.c.timestamp.desc()))[1]
        columns["granularity"] = literal(target_granularity, Integer())
        buckets = select(*columns.values()).group_by(*(moved.c[name] for name in keys), bucket)
        added = insert(model).from_select(list(columns), buckets).cte("added")
        result = await self._session.execute(select(func.count()).select_from(moved).add_cte(added))
        return int(result.scalar_one())

    async def get_connector_totals_since(
        self, since_by_connector: dict[str, datetime]
    ) -> dict[str, dict[str, int]]:
        """Requests and bytes per connector since each connector's own ``since``: the sum of its rows in that window.

        A connector without rows in its window is absent.
        """
        if not since_by_connector:
            return {}
        windows = [
            (ConnectorMetricsModel.connector_id == connector_id)
            & (ConnectorMetricsModel.timestamp >= since)
            for connector_id, since in since_by_connector.items()
        ]
        query = (
            select(
                ConnectorMetricsModel.connector_id,
                func.sum(ConnectorMetricsModel.request_count).label("total_requests"),
                func.sum(ConnectorMetricsModel.bytes_sent).label("total_bytes_sent"),
                func.sum(ConnectorMetricsModel.bytes_received).label("total_bytes_received"),
            )
            .where(or_(*windows))
            .group_by(ConnectorMetricsModel.connector_id)
        )
        result = await self._session.execute(query)
        return {
            row.connector_id: {
                "request_count": int(row.total_requests or 0),
                "bytes_sent": int(row.total_bytes_sent or 0),
                "bytes_received": int(row.total_bytes_received or 0),
            }
            for row in result.all()
        }

    async def get_connector_metrics_history(
        self,
        connector_id: str,
        since: datetime | None = None,
        limit: int = 100,
        granularity: int = 60,
    ) -> list[dict[str, Any]]:
        """Historical metrics for a connector at a specific granularity, newest first."""
        query = select(ConnectorMetricsModel).where(
            ConnectorMetricsModel.connector_id == connector_id,
            ConnectorMetricsModel.granularity == granularity,
        )
        if since:
            query = query.where(ConnectorMetricsModel.timestamp >= since)
        query = query.order_by(ConnectorMetricsModel.timestamp.desc()).limit(limit)
        result = await self._session.execute(query)
        return [_latency_history_row(m.timestamp, m) for m in result.scalars().all()]

    async def get_connector_metrics_history_aggregated(
        self,
        connector_id: str,
        since: datetime,
        bucket_seconds: int,
    ) -> list[dict[str, Any]]:
        """Connector history in fixed buckets, newest first (see the project variant)."""
        bucket_epoch, bucket_ts = _bucket_expressions(ConnectorMetricsModel, bucket_seconds)
        agg_cols = _metrics_aggregate_columns(ConnectorMetricsModel)
        query = (
            select(bucket_ts, *agg_cols)
            .where(ConnectorMetricsModel.connector_id == connector_id)
            .where(ConnectorMetricsModel.timestamp >= since)
            .where(ConnectorMetricsModel.granularity <= bucket_seconds)
            .group_by(bucket_epoch)
            .order_by(bucket_epoch.desc())
        )
        result = await self._session.execute(query)
        return [_latency_history_row(row.bucket_ts, row) for row in result.all()]

    async def compact_connector_metrics(
        self,
        project_id: str,
        older_than: datetime,
        source_granularity: int,
        target_granularity: int,
    ) -> int:
        """As ``compact_project_metrics`` does, for the rows of the project's connectors, a series per connector."""
        connectors = select(ConnectorModel.id).where(ConnectorModel.project_id == project_id)
        return await self._compact(
            ConnectorMetricsModel,
            ConnectorMetricsModel.connector_id.in_(connectors),
            older_than,
            source_granularity,
            target_granularity,
            keys=["connector_id"],
        )

    async def delete_connector_metrics_for_project_older_than(
        self,
        project_id: str,
        older_than: datetime,
    ) -> int:
        """Delete connector metrics of a project's connectors older than the timestamp."""
        connector_ids_subquery = (
            select(ConnectorModel.id)
            .where(ConnectorModel.project_id == project_id)
            .scalar_subquery()
        )
        stmt = (
            delete(ConnectorMetricsModel)
            .where(ConnectorMetricsModel.connector_id.in_(connector_ids_subquery))
            .where(ConnectorMetricsModel.timestamp < older_than)
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def get_project_metrics_history(
        self,
        project_id: str,
        since: datetime | None = None,
        limit: int = 100,
        granularity: int = 60,
    ) -> list[dict[str, Any]]:
        """Get historical metrics for a project at a specific granularity."""
        query = select(ProjectMetricsModel).where(
            ProjectMetricsModel.project_id == project_id,
            ProjectMetricsModel.granularity == granularity,
        )
        if since:
            query = query.where(ProjectMetricsModel.timestamp >= since)
        query = query.order_by(ProjectMetricsModel.timestamp.desc()).limit(limit)

        result = await self._session.execute(query)
        return [_latency_history_row(m.timestamp, m) for m in result.scalars().all()]

    async def get_project_metrics_history_aggregated(
        self,
        project_id: str,
        since: datetime,
        bucket_seconds: int,
    ) -> list[dict[str, Any]]:
        """A project's metrics summed into buckets of ``bucket_seconds`` by epoch floor division, newest first.

        Latency and its steps are weighted by successful requests, and rows of any granularity up to ``bucket_seconds``
        count, so compacted rows and recent raw ones add up together.
        """
        bucket_epoch, bucket_ts = _bucket_expressions(ProjectMetricsModel, bucket_seconds)
        agg_cols = _metrics_aggregate_columns(ProjectMetricsModel)

        query = (
            select(bucket_ts, *agg_cols)
            .where(ProjectMetricsModel.project_id == project_id)
            .where(ProjectMetricsModel.timestamp >= since)
            .where(ProjectMetricsModel.granularity <= bucket_seconds)
            .group_by(bucket_epoch)
            .order_by(bucket_epoch.desc())
        )

        result = await self._session.execute(query)
        return [_latency_history_row(row.bucket_ts, row) for row in result.all()]

    async def get_projects_metrics_buckets(
        self, until: datetime, buckets: int, bucket_seconds: int
    ) -> list[tuple[str, int, MetricDelta]]:
        """Every project's counters summed into ``buckets`` buckets of ``bucket_seconds`` counted back from ``until``.

        Each row is (project id, bucket, counters, the latency left out). Bucket 0 is the newest and bucket ``n`` holds
        the rows stamped in ``(until - (n + 1) * width, until - n * width]``, so each bucket holds whole flushes; rows
        of every granularity count. Only buckets with rows come back.
        """
        model = ProjectMetricsModel
        counters = ("request_count", "success_count", "failure_count", "bytes_sent", "bytes_received")
        # Each row's bucket in a subquery, so ``until`` stays a parameter and the statement is the same every call.
        age = func.extract("epoch", literal(until, DateTime()) - model.timestamp)
        aged = (
            select(
                model.project_id,
                func.floor(age / bucket_seconds).label("bucket"),
                *(model.__table__.c[c] for c in counters),
            )
            .where(model.timestamp > until - timedelta(seconds=buckets * bucket_seconds), model.timestamp <= until)
            .subquery()
        )
        query = select(
            aged.c.project_id, aged.c.bucket, *(func.sum(aged.c[c]).label(c) for c in counters)
        ).group_by(aged.c.project_id, aged.c.bucket)
        result = await self._session.execute(query)
        return [
            (
                row.project_id,
                int(row.bucket),
                MetricDelta(
                    request_count=int(row.request_count or 0),
                    success_count=int(row.success_count or 0),
                    failure_count=int(row.failure_count or 0),
                    bytes_sent=int(row.bytes_sent or 0),
                    bytes_received=int(row.bytes_received or 0),
                ),
            )
            for row in result.all()
        ]

    async def compact_project_metrics(
        self,
        project_id: str,
        older_than: datetime,
        source_granularity: int,
        target_granularity: int,
    ) -> int:
        """Fold the project's rows of the source granularity older than ``older_than`` into a row per target bucket.

        Returns how many source rows were folded; a compaction at the same time folds each row once between them.
        """
        return await self._compact(
            ProjectMetricsModel,
            ProjectMetricsModel.project_id == project_id,
            older_than,
            source_granularity,
            target_granularity,
            keys=["project_id"],
        )

    async def compact_proxy_metrics(
        self,
        project_id: str,
        older_than: datetime,
        source_granularity: int,
        target_granularity: int,
    ) -> int:
        """As ``compact_project_metrics`` does, for the rows of the project's proxies: a series per proxy.

        Each bucket keeps the status of its newest row.
        """
        proxies = (
            select(ProxyModel.id)
            .join(ConnectorModel, ProxyModel.connector_id == ConnectorModel.id)
            .where(ConnectorModel.project_id == project_id)
        )
        return await self._compact(
            ProxyMetricsModel,
            ProxyMetricsModel.proxy_id.in_(proxies),
            older_than,
            source_granularity,
            target_granularity,
            keys=["proxy_id"],
            latest="status",
        )

    async def delete_project_metrics_older_than(
        self,
        project_id: str,
        older_than: datetime,
    ) -> int:
        """Delete all project metrics rows older than the given timestamp."""
        stmt = (
            delete(ProjectMetricsModel)
            .where(ProjectMetricsModel.project_id == project_id)
            .where(ProjectMetricsModel.timestamp < older_than)
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def delete_proxy_metrics_for_project_older_than(
        self,
        project_id: str,
        older_than: datetime,
    ) -> int:
        """Delete proxy metrics for all proxies belonging to a project.

        Uses a subquery to resolve proxy IDs via connectors in a single DELETE.
        """
        proxy_ids_subquery = (
            select(ProxyModel.id)
            .join(ConnectorModel, ProxyModel.connector_id == ConnectorModel.id)
            .where(ConnectorModel.project_id == project_id)
            .scalar_subquery()
        )
        stmt = (
            delete(ProxyMetricsModel)
            .where(ProxyMetricsModel.proxy_id.in_(proxy_ids_subquery))
            .where(ProxyMetricsModel.timestamp < older_than)
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    # Tunnel device metrics methods

    async def save_tunnel_peer_metrics_snapshots(
        self, batch: Mapping[str, TunnelPeerMetricDelta], *, granularity: int = 60
    ) -> list[str]:
        """Insert one history row per device of a flush window; returns the ids no tunnel protocol knows.

        The rows join every protocol's devices for their protocol and project, so a device deleted since is not written.
        """
        if not batch:
            return []
        table = TunnelPeerMetricsModel.__table__
        devices = union_all(
            *(
                select(
                    peers.id.label("id"),
                    peers.project_id.label("project_id"),
                    literal(protocol, String()).label("protocol"),
                )
                for protocol, peers in TUNNEL_PEER_TABLES.items()
            )
        ).subquery("devices")
        columns = ["peer_id", "timestamp", *_DEVICE_COUNTERS, "protocol", "project_id", "granularity"]
        now = utc_now()
        ids = sorted(batch)
        kept: set[str] = set()
        for start in range(0, len(ids), _DEVICE_BATCH):
            rows: list[tuple[Any, ...]] = [
                (peer_id, *(getattr(batch[peer_id], name) for name in _DEVICE_COUNTERS))
                for peer_id in ids[start:start + _DEVICE_BATCH]
            ]
            window = values(
                column("peer_id", String()),
                *(column(name, table.c[name].type) for name in _DEVICE_COUNTERS),
                name="window",
            ).data(rows)
            source = select(
                window.c.peer_id,
                literal(now, DateTime()).label("timestamp"),
                *(window.c[name] for name in _DEVICE_COUNTERS),
                devices.c.protocol,
                devices.c.project_id,
                literal(granularity, Integer()).label("granularity"),
            ).select_from(window.join(devices, devices.c.id == window.c.peer_id))
            result = await self._session.execute(
                insert(TunnelPeerMetricsModel).from_select(columns, source).returning(TunnelPeerMetricsModel.peer_id)
            )
            kept.update(result.scalars().all())
        return [peer_id for peer_id in ids if peer_id not in kept]

    async def get_tunnel_peer_metrics_history(
        self,
        project_id: str,
        peer_id: str,
        since: datetime | None = None,
        limit: int = 100,
        granularity: int = 60,
    ) -> list[dict[str, Any]]:
        """A device's history at one granularity, newest first.

        Scoped by project as well as device so a route can answer for a
        device of its project without consulting the protocol's directory.
        """
        model = TunnelPeerMetricsModel
        query = select(model).where(
            model.project_id == project_id,
            model.peer_id == peer_id,
            model.granularity == granularity,
        )
        if since:
            query = query.where(model.timestamp >= since)
        query = query.order_by(model.timestamp.desc()).limit(limit)
        result = await self._session.execute(query)
        return [_tunnel_peer_row(m.timestamp, m) for m in result.scalars().all()]

    async def get_tunnel_peer_metrics_history_aggregated(
        self,
        project_id: str,
        peer_id: str,
        since: datetime,
        bucket_seconds: int,
    ) -> list[dict[str, Any]]:
        """A device's history in fixed buckets, newest first (see the project variant)."""
        model = TunnelPeerMetricsModel
        bucket_epoch, bucket_ts = _bucket_expressions(model, bucket_seconds)
        query = (
            select(bucket_ts, *_tunnel_peer_aggregate_columns())
            .where(model.project_id == project_id)
            .where(model.peer_id == peer_id)
            .where(model.timestamp >= since)
            .where(model.granularity <= bucket_seconds)
            .group_by(bucket_epoch)
            .order_by(bucket_epoch.desc())
        )
        result = await self._session.execute(query)
        return [_tunnel_peer_row(row.bucket_ts, row) for row in result.all()]

    async def compact_tunnel_peer_metrics(
        self,
        project_id: str,
        older_than: datetime,
        source_granularity: int,
        target_granularity: int,
    ) -> int:
        """As ``compact_project_metrics`` does, for every tunnel device of the project whatever its protocol: a series per device."""
        return await self._compact(
            TunnelPeerMetricsModel,
            TunnelPeerMetricsModel.project_id == project_id,
            older_than,
            source_granularity,
            target_granularity,
            keys=["peer_id", "protocol", "project_id"],
            sums=[*_REQUEST_COUNTERS, "by_address", "encrypted_dns_blocked"],
        )

    async def delete_tunnel_peer_metrics_older_than(self, project_id: str, older_than: datetime) -> int:
        """Delete a project's device history older than the timestamp."""
        model = TunnelPeerMetricsModel
        result = await self._session.execute(
            delete(model).where(model.project_id == project_id).where(model.timestamp < older_than)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    # Host-level metrics: a project's requests by destination host and connector.

    async def save_host_metrics_snapshots(
        self, batch: dict[tuple[str, str, str], MetricDelta]
    ) -> list[tuple[str, str, str]]:
        """Insert one row per (project, connector, host) of a flush window; returns the keys dropped.

        One lookup of the connectors involved, then a single multi-row insert, and a window whose connector is gone (or
        in another project) is dropped.
        """
        if not batch:
            return []
        connector_ids = {connector_id for _project, connector_id, _host in batch}
        result = await self._session.execute(
            select(ConnectorModel.id, ConnectorModel.project_id).where(ConnectorModel.id.in_(connector_ids))
        )
        known = {row.id: row.project_id for row in result.all()}
        now = utc_now()
        rows: list[dict[str, Any]] = []
        dropped: list[tuple[str, str, str]] = []
        for key, delta in batch.items():
            project_id, connector_id, host = key
            if known.get(connector_id) != project_id:
                dropped.append(key)
                continue
            rows.append({
                "project_id": project_id,
                "connector_id": connector_id,
                "host": host,
                "timestamp": now,
                "request_count": delta.request_count,
                "success_count": delta.success_count,
                "failure_count": delta.failure_count,
                "avg_latency_ms": delta.avg_latency_ms,
                "bytes_sent": delta.bytes_sent,
                "bytes_received": delta.bytes_received,
                "granularity": 60,
            })
        if rows:
            await self._session.execute(insert(HostMetricsModel), rows)
        return dropped

    @staticmethod
    def _host_window_filter(
        project_id: str, since: datetime, connector_id: str | None, search: str | None
    ) -> list[Any]:
        model = HostMetricsModel
        conditions: list[Any] = [model.project_id == project_id, model.timestamp >= since]
        if connector_id is not None:
            conditions.append(model.connector_id == connector_id)
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(model.host.ilike(f"%{escaped}%", escape="\\"))
        return conditions

    async def get_host_totals(
        self,
        project_id: str,
        since: datetime,
        *,
        connector_id: str | None = None,
        search: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """The hosts a project requested in the window, busiest first, with their totals: at most ``limit`` of them.

        One connector or a host substring narrows the set, and ``get_host_summary`` says how many there were.
        """
        model = HostMetricsModel
        query = (
            select(
                model.host,
                *_metrics_aggregate_columns(model),
                func.count(func.distinct(model.connector_id)).label("connector_count"),
            )
            .where(*self._host_window_filter(project_id, since, connector_id, search))
            .group_by(model.host)
            .order_by(func.sum(model.request_count).desc(), func.sum(model.bytes_received).desc(), model.host)
            .limit(limit)
        )
        result = await self._session.execute(query)
        return [
            {**_history_row(None, row), "host": row.host, "connector_count": int(row.connector_count or 0)}
            for row in result.all()
        ]

    async def get_host_summary(
        self,
        project_id: str,
        since: datetime,
        *,
        connector_id: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        """How many distinct hosts matched the window and their combined totals."""
        model = HostMetricsModel
        query = select(
            func.count(func.distinct(model.host)).label("host_count"),
            *_metrics_aggregate_columns(model),
        ).where(*self._host_window_filter(project_id, since, connector_id, search))
        row = (await self._session.execute(query)).one()
        return {**_history_row(None, row), "host_count": int(row.host_count or 0)}

    async def get_host_connector_breakdown(
        self,
        project_id: str,
        since: datetime,
        hosts: list[str],
        *,
        connector_id: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Each listed host's totals split by the connector that carried the requests."""
        if not hosts:
            return {}
        model = HostMetricsModel
        query = (
            select(model.host, model.connector_id, *_metrics_aggregate_columns(model))
            .where(*self._host_window_filter(project_id, since, connector_id, None))
            .where(model.host.in_(hosts))
            .group_by(model.host, model.connector_id)
            .order_by(model.host, func.sum(model.request_count).desc())
        )
        result = await self._session.execute(query)
        breakdown: dict[str, list[dict[str, Any]]] = {}
        for row in result.all():
            breakdown.setdefault(row.host, []).append({**_history_row(None, row), "connector_id": row.connector_id})
        return breakdown

    async def get_host_history_aggregated(
        self,
        project_id: str,
        since: datetime,
        bucket_seconds: int,
        hosts: list[str],
        *,
        connector_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Per-bucket totals for each listed host, oldest first, with every other host folded into ``HOST_OVERFLOW``.

        One query, so the chart stacks the top hosts against the remainder without a second pass.
        """
        model = HostMetricsModel
        bucket_epoch, bucket_ts = _bucket_expressions(model, bucket_seconds)
        series = (
            case((model.host.in_(hosts), model.host), else_=literal(HOST_OVERFLOW)) if hosts
            else literal(HOST_OVERFLOW)
        ).label("series")
        query = (
            select(bucket_ts, series, *_metrics_aggregate_columns(model))
            .where(*self._host_window_filter(project_id, since, connector_id, None))
            .where(model.granularity <= bucket_seconds)
            .group_by(bucket_epoch, series)
            .order_by(bucket_epoch.asc())
        )
        result = await self._session.execute(query)
        return [{**_history_row(row.bucket_ts, row), "host": row.series} for row in result.all()]

    async def compact_host_metrics(
        self,
        project_id: str,
        older_than: datetime,
        source_granularity: int,
        target_granularity: int,
    ) -> int:
        """As ``compact_project_metrics`` does, for the project's host rows: a series per connector and host."""
        return await self._compact(
            HostMetricsModel,
            HostMetricsModel.project_id == project_id,
            older_than,
            source_granularity,
            target_granularity,
            keys=["project_id", "connector_id", "host"],
        )

    async def delete_host_metrics_older_than(self, project_id: str, older_than: datetime) -> int:
        """Delete a project's host history older than the timestamp."""
        model = HostMetricsModel
        result = await self._session.execute(
            delete(model).where(model.project_id == project_id).where(model.timestamp < older_than)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    # Failure counts: a project's failed requests by kind, vendor status and connector.

    async def save_failure_metrics_snapshots(self, batch: dict[FailureCountKey, int]) -> list[FailureCountKey]:
        """Insert one row per (project, connector, kind, vendor status) of a flush window; returns the keys dropped.

        A count whose project is gone, or whose connector is gone or belongs
        to another project, is dropped rather than failing the flush.
        """
        if not batch:
            return []
        project_ids = {project_id for project_id, _connector, _kind, _status in batch}
        connector_ids = {connector_id for _project, connector_id, _kind, _status in batch if connector_id}
        result = await self._session.execute(select(ProjectModel.id).where(ProjectModel.id.in_(project_ids)))
        known_projects = set(result.scalars().all())
        known_connectors: dict[str, str] = {}
        if connector_ids:
            result = await self._session.execute(
                select(ConnectorModel.id, ConnectorModel.project_id).where(ConnectorModel.id.in_(connector_ids))
            )
            known_connectors = {row.id: row.project_id for row in result.all()}
        now = utc_now()
        rows: list[dict[str, Any]] = []
        dropped: list[FailureCountKey] = []
        for key, count in batch.items():
            project_id, connector_id, kind, upstream_status = key
            if project_id not in known_projects or (
                connector_id is not None and known_connectors.get(connector_id) != project_id
            ):
                dropped.append(key)
                continue
            rows.append({
                "project_id": project_id,
                "connector_id": connector_id,
                "kind": kind,
                "upstream_status": upstream_status,
                "timestamp": now,
                "failure_count": count,
                "granularity": 60,
            })
        if rows:
            await self._session.execute(insert(FailureMetricsModel), rows)
        return dropped

    async def get_failure_breakdown(self, project_id: str, since: datetime) -> list[dict[str, Any]]:
        """A project's failures in the window by kind, vendor status and connector, most first."""
        model = FailureMetricsModel
        total = func.sum(model.failure_count).label("failure_count")
        query = (
            select(model.kind, model.upstream_status, model.connector_id, total)
            .where(model.project_id == project_id, model.timestamp >= since)
            .group_by(model.kind, model.upstream_status, model.connector_id)
            .order_by(total.desc())
        )
        result = await self._session.execute(query)
        return [
            {
                "kind": row.kind,
                "upstream_status": row.upstream_status,
                "connector_id": row.connector_id,
                "failure_count": int(row.failure_count or 0),
            }
            for row in result.all()
        ]

    async def get_failure_history(self, project_id: str, since: datetime, bucket_seconds: int) -> list[dict[str, Any]]:
        """Per-bucket failure counts of each kind in the window, chronological."""
        model = FailureMetricsModel
        bucket_epoch, bucket_ts = _bucket_expressions(model, bucket_seconds)
        query = (
            select(bucket_ts, model.kind, func.sum(model.failure_count).label("failure_count"))
            .where(model.project_id == project_id, model.timestamp >= since, model.granularity <= bucket_seconds)
            .group_by(bucket_epoch, model.kind)
            .order_by(bucket_epoch.asc())
        )
        result = await self._session.execute(query)
        return [
            {"timestamp": _strip_tz(row.bucket_ts), "kind": row.kind, "failure_count": int(row.failure_count or 0)}
            for row in result.all()
        ]

    async def compact_failure_metrics(
        self,
        project_id: str,
        older_than: datetime,
        source_granularity: int,
        target_granularity: int,
    ) -> int:
        """As ``compact_project_metrics`` does, for the project's failure counts: a series per connector, kind and vendor status."""
        return await self._compact(
            FailureMetricsModel,
            FailureMetricsModel.project_id == project_id,
            older_than,
            source_granularity,
            target_granularity,
            keys=["project_id", "connector_id", "kind", "upstream_status"],
            sums=["failure_count"],
            latency=False,
        )

    async def delete_failure_metrics_older_than(self, project_id: str, older_than: datetime) -> int:
        """Delete a project's failure counts older than the timestamp."""
        model = FailureMetricsModel
        result = await self._session.execute(
            delete(model).where(model.project_id == project_id).where(model.timestamp < older_than)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    # Running totals: each proxy's, project's and tunnel device's counters for all time.

    async def upsert_totals(self, kind: TotalledWindows, deltas: Mapping[str, MetricDelta]) -> None:
        """Add flushed windows of ``kind`` (proxies, projects or tunnel devices) to their entities' totals, creating the rows that do not exist yet, in id order.

        Called in the transaction that inserts the same windows into the history, with the ids that insert kept; an id
        whose entity went is left to ``delete_orphan_totals``.
        """
        now = utc_now()
        # Every row in a tunnel device's shape: a proxy's or a project's two device counters are 0.
        rows = [
            {
                **TunnelPeerMetricDelta.summed(deltas[entity_id]).to_dict(),
                "kind": kind.name,
                "entity_id": entity_id,
                "updated_at": now,
            }
            for entity_id in sorted(deltas)
        ]
        for start in range(0, len(rows), _TOTALS_BATCH):
            statement = pg_insert(MetricTotalsModel).values(rows[start:start + _TOTALS_BATCH])
            added = {name: getattr(MetricTotalsModel, name) + getattr(statement.excluded, name) for name in _TOTALS_COUNTERS}
            await self._session.execute(
                statement.on_conflict_do_update(
                    index_elements=[MetricTotalsModel.kind, MetricTotalsModel.entity_id],
                    set_={**added, "updated_at": statement.excluded.updated_at},
                )
            )

    async def get_totals(self, kind: TotalledWindows, ids: Iterable[str]) -> dict[str, MetricDelta]:
        """The totals of the entities of ``kind`` in ``ids`` that have any, keyed by id, in the kind's counts type.

        Plain rows rather than ORM objects, so totals upserted earlier in the
        same session are read as they are now.
        """
        wanted = sorted(set(ids))
        totals: dict[str, MetricDelta] = {}
        for start in range(0, len(wanted), _TOTALS_BATCH):
            result = await self._session.execute(
                select(MetricTotalsModel.__table__).where(
                    MetricTotalsModel.kind == kind.name,
                    MetricTotalsModel.entity_id.in_(wanted[start:start + _TOTALS_BATCH]),
                )
            )
            totals.update((row.entity_id, kind.read(dict(row._mapping))) for row in result.all())
        return totals

    async def rebuild_totals(self) -> int:
        """Recompute every total from the history, as migration 048 first filled them; the rows written.

        The last step of a backup import, in its transaction, so the totals match the history the file brought.
        """
        await self._session.execute(delete(MetricTotalsModel))
        result = await self._session.execute(_REBUILD_TOTALS)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def delete_orphan_totals(self) -> int:
        """Delete the totals of proxies, projects and tunnel devices that no longer exist; how many went.

        A device is gone when no tunnel protocol's table has it.
        """
        totals = MetricTotalsModel
        gone = or_(
            and_(totals.kind == PROXY_WINDOWS.name, ~exists().where(ProxyModel.id == totals.entity_id)),
            and_(totals.kind == PROJECT_WINDOWS.name, ~exists().where(ProjectModel.id == totals.entity_id)),
            and_(
                totals.kind == TUNNEL_PEER_WINDOWS.name,
                *[~exists().where(peers.id == totals.entity_id) for peers in TUNNEL_PEER_TABLES.values()],
            ),
        )
        result = await self._session.execute(delete(totals).where(gone))
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    # Flush records: each metrics flush Postgres committed, by its job entry's ID.

    async def record_flush(self, message_id: str) -> bool:
        """Record flush ``message_id`` in the current transaction; False when it is recorded already.

        A transaction recording the same ID meanwhile holds this one until it ends, and its commit makes this False.
        """
        statement = (
            pg_insert(MetricFlushModel)
            .values(message_id=message_id, flushed_at=utc_now())
            .on_conflict_do_nothing(index_elements=[MetricFlushModel.message_id])
            .returning(MetricFlushModel.message_id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def flush_recorded(self, message_id: str) -> bool:
        """Whether flush ``message_id`` committed."""
        result = await self._session.execute(select(exists().where(MetricFlushModel.message_id == message_id)))
        return bool(result.scalar_one())

    async def delete_flushes_older_than(self, older_than: datetime) -> int:
        """Forget the flushes committed before ``older_than``; how many went."""
        result = await self._session.execute(delete(MetricFlushModel).where(MetricFlushModel.flushed_at < older_than))
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


# Gauge columns carried through the history endpoints, in chart order. Kept in
# one place so the raw and the bucketed query can never drift apart.
_SYSTEM_GAUGES: tuple[str, ...] = (
    "database_size_bytes",
    "redis_memory_bytes",
    "redis_keys",
    "projects",
    "credentials",
    "connectors",
    "connectors_enabled",
    "users",
    "proxies_total",
    "proxies_healthy",
    "proxies_unhealthy",
)


class SystemMetricsRepository:
    """Reads and writes install-wide gauge snapshots."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_snapshot(self, **values: Any) -> None:
        """Insert one snapshot row. Caller commits."""
        self._session.add(SystemMetricsModel(timestamp=utc_now(), **values))

    async def get_latest_timestamp(self) -> datetime | None:
        """Timestamp of the newest snapshot, or None when the table is empty.

        The snapshot job uses this to decide whether a reading is due, so a
        restart or a snapshot asked for twice writes no off-cadence extra row.
        """
        query = select(SystemMetricsModel.timestamp).order_by(
            SystemMetricsModel.timestamp.desc()
        ).limit(1)
        result = await self._session.execute(query)
        return result.scalar_one_or_none()

    async def get_history(self, since: datetime, limit: int = 2000) -> list[dict[str, Any]]:
        """Raw snapshots since ``since``, oldest first."""
        query = (
            select(SystemMetricsModel)
            .where(SystemMetricsModel.timestamp >= since)
            .order_by(SystemMetricsModel.timestamp.desc())
            .limit(limit)
        )
        result = await self._session.execute(query)
        models = list(result.scalars().all())
        models.reverse()
        return [
            {"timestamp": m.timestamp, **{g: getattr(m, g) for g in _SYSTEM_GAUGES}}
            for m in models
        ]

    async def get_history_aggregated(
        self, since: datetime, bucket_seconds: int
    ) -> list[dict[str, Any]]:
        """Snapshots downsampled into fixed-width buckets, oldest first, every column averaged since these are gauges.

        Counts are rounded back to whole units.
        """
        bucket_epoch, bucket_ts = _bucket_expressions(SystemMetricsModel, bucket_seconds)
        agg_cols = [
            func.avg(getattr(SystemMetricsModel, gauge)).label(gauge) for gauge in _SYSTEM_GAUGES
        ]

        query = (
            select(bucket_ts, *agg_cols)
            .where(SystemMetricsModel.timestamp >= since)
            .group_by(bucket_epoch)
            .order_by(bucket_epoch.asc())
        )
        result = await self._session.execute(query)
        return [
            {
                "timestamp": row.bucket_ts,
                **{gauge: int(round(float(getattr(row, gauge) or 0))) for gauge in _SYSTEM_GAUGES},
            }
            for row in result.all()
        ]

    async def get_table_sizes_at_edges(
        self, since: datetime
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The per-table sizes from the oldest and newest snapshot in the window.

        Growth per table is a difference between two points, so it needs no
        bucketing - which is why these breakdowns stay in a JSON column
        instead of being normalised into rows.
        """
        base = select(SystemMetricsModel.table_sizes).where(
            SystemMetricsModel.timestamp >= since
        )
        oldest = await self._session.execute(
            base.order_by(SystemMetricsModel.timestamp.asc()).limit(1)
        )
        newest = await self._session.execute(
            base.order_by(SystemMetricsModel.timestamp.desc()).limit(1)
        )
        return oldest.scalar_one_or_none() or {}, newest.scalar_one_or_none() or {}

    async def delete_older_than(self, cutoff: datetime) -> int:
        """Drop snapshots past the retention window. Caller commits."""
        result = await self._session.execute(
            delete(SystemMetricsModel).where(SystemMetricsModel.timestamp < cutoff)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


class UserRepository:
    """Repository for user database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[User]:
        """Get all users, ordered by creation time."""
        result = await self._session.execute(
            select(UserModel).order_by(UserModel.created_at)
        )
        models = result.scalars().all()
        return [self._to_domain(m) for m in models]

    async def get_by_id(self, user_id: str) -> User | None:
        """Get user by ID."""
        result = await self._session.execute(
            select(UserModel).where(UserModel.id == user_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_by_username(self, username: str) -> User | None:
        """Get user by username."""
        result = await self._session.execute(
            select(UserModel).where(UserModel.username == username)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_by_email(self, email: str) -> User | None:
        """Get user by email (only matches non-empty emails)."""
        if not email:
            return None
        result = await self._session.execute(
            select(UserModel).where(UserModel.email == email)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def get_by_invite_token(self, token: str) -> User | None:
        """Get user by invite token."""
        result = await self._session.execute(
            select(UserModel).where(UserModel.invite_token == token)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def count(self) -> int:
        """Count total users."""
        result = await self._session.execute(select(func.count(UserModel.id)))
        return result.scalar() or 0

    async def create(self, user: User) -> User:
        """Create a new user."""
        model = UserModel(
            id=user.id,
            username=user.username,
            email=user.email,
            password_hash=user.password_hash,
            role=user.role.value if isinstance(user.role, UserRole) else user.role,
            is_active=user.is_active,
            invite_token=user.invite_token,
            invite_token_expires_at=user.invite_token_expires_at,
            theme_preference=user.theme_preference,
            last_login_at=user.last_login_at,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return user

    async def update(self, user: User) -> User:
        """Update an existing user."""
        result = await self._session.execute(
            select(UserModel).where(UserModel.id == user.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.username = user.username
            model.email = user.email
            model.password_hash = user.password_hash
            model.role = user.role.value if isinstance(user.role, UserRole) else user.role
            model.is_active = user.is_active
            model.invite_token = user.invite_token
            model.invite_token_expires_at = user.invite_token_expires_at
            model.theme_preference = user.theme_preference
            model.updated_at = utc_now()
            await self._session.flush()
        return user

    async def touch_last_login(self, user_id: str) -> datetime:
        """Stamp ``last_login_at`` for a user who just obtained a token.

        A targeted UPDATE that never rewrites the profile, ``updated_at`` pinned so its ``onupdate`` hook does not fire.
        """
        now = utc_now()
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(last_login_at=now, updated_at=UserModel.updated_at)
        )
        return now

    async def mark_release_seen(self, user_id: str, version: str) -> None:
        """Record that the user opened What's New on ``version``; ``updated_at`` pinned, as for ``touch_last_login``."""
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(last_seen_release=version, updated_at=UserModel.updated_at)
        )

    async def delete(self, user_id: str) -> bool:
        """Delete a user."""
        result = await self._session.execute(
            delete(UserModel).where(UserModel.id == user_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    def _to_domain(self, model: UserModel) -> User:
        """Convert database model to domain model."""
        return User(
            id=model.id,
            username=model.username,
            email=model.email,
            password_hash=model.password_hash,
            role=UserRole(model.role),
            is_active=model.is_active,
            invite_token=model.invite_token,
            invite_token_expires_at=model.invite_token_expires_at,
            theme_preference=model.theme_preference,
            last_login_at=model.last_login_at,
            last_seen_release=model.last_seen_release,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class ProviderDescriptorRepository:
    """Repository for admin-authored provider descriptors."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_all(self) -> list[ProviderRecord]:
        """Every stored descriptor, by id.

        One order for every reader: the feed compile and the api's and
        workers' provider stores apply the enabled documents in it, and a
        store skips a list equal to the one it applied last, which two
        orders of the same documents never are (role ``all`` runs both).
        """
        result = await self._session.execute(
            select(ProviderDescriptorModel).order_by(ProviderDescriptorModel.id)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    async def get_by_id(self, provider_id: str) -> ProviderRecord | None:
        result = await self._session.execute(
            select(ProviderDescriptorModel).where(ProviderDescriptorModel.id == provider_id)
        )
        model = result.scalar_one_or_none()
        return self._to_domain(model) if model else None

    async def create(self, record: ProviderRecord) -> ProviderRecord:
        model = ProviderDescriptorModel(
            id=record.id,
            name=record.name,
            spec=record.spec,
            enabled=record.enabled,
            version=record.version,
            created_by=record.created_by,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        self._session.add(model)
        await self._session.flush()
        return record

    async def update(self, record: ProviderRecord) -> ProviderRecord:
        result = await self._session.execute(
            select(ProviderDescriptorModel).where(ProviderDescriptorModel.id == record.id)
        )
        model = result.scalar_one_or_none()
        if model:
            model.name = record.name
            model.spec = record.spec
            model.enabled = record.enabled
            model.version = record.version
            model.updated_at = utc_now()
            record.updated_at = model.updated_at
            await self._session.flush()
        return record

    async def delete(self, provider_id: str) -> bool:
        result = await self._session.execute(
            delete(ProviderDescriptorModel).where(ProviderDescriptorModel.id == provider_id)
        )
        return bool(result.rowcount and result.rowcount > 0)  # type: ignore[attr-defined]

    def _to_domain(self, model: ProviderDescriptorModel) -> ProviderRecord:
        return ProviderRecord(
            id=model.id,
            name=model.name,
            spec=model.spec,
            enabled=model.enabled,
            version=model.version,
            created_by=model.created_by,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class ProviderAuditRepository:
    """Repository for the provider descriptor audit log."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, entry: ProviderAuditEntry) -> ProviderAuditEntry:
        self._session.add(
            ProviderAuditModel(
                id=entry.id,
                provider_id=entry.provider_id,
                action=entry.action,
                actor=entry.actor,
                egress_hosts=entry.egress_hosts,
                hosts_changed=entry.hosts_changed,
                spec=entry.spec,
                created_at=entry.created_at,
            )
        )
        await self._session.flush()
        return entry

    async def get_for_provider(self, provider_id: str, limit: int = 100) -> list[ProviderAuditEntry]:
        result = await self._session.execute(
            select(ProviderAuditModel)
            .where(ProviderAuditModel.provider_id == provider_id)
            .order_by(ProviderAuditModel.created_at.desc())
            .limit(limit)
        )
        return [self._to_domain(m) for m in result.scalars().all()]

    def _to_domain(self, model: ProviderAuditModel) -> ProviderAuditEntry:
        return ProviderAuditEntry(
            id=model.id,
            provider_id=model.provider_id,
            action=model.action,  # type: ignore[arg-type]
            actor=model.actor,
            egress_hosts=list(model.egress_hosts or []),
            hosts_changed=model.hosts_changed,
            spec=model.spec,
            created_at=model.created_at,
        )
