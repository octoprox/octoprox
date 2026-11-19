# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""The writes that change what a project's checks find ask for its alert check after their commit: a run-now entry on
the alerts job stream and the project marked requested, so its card shows a re-check under way."""

from __future__ import annotations

from datetime import datetime

from api.core import utc_now
from api.core.audit import Actor
from api.core.roles import JobKind
from api.db.redis import ALERT_CHECK_KEY, JOB_STREAM_KEY, RedisClient
from api.models.connector import Connector, ConnectorUpdate
from api.models.countries import CountryRole
from api.models.project import Project, ProjectUpdate
from api.services.connectors import ConnectorService
from api.services.projects import ProjectService
from api.services.proxies import ProxyService
from tests.conftest import queued_job_keys


async def _asked(redis_client: RedisClient, project: Project, since: datetime) -> bool:
    """Whether the project's check was asked for since ``since``: its entry queued, its mark that recent."""
    requested = await redis_client.client.hget(ALERT_CHECK_KEY.format(scope=project.id), "requested")
    return (
        await queued_job_keys(redis_client, JobKind.ALERTS) == [project.id]
        and requested is not None
        and datetime.fromisoformat(requested) >= since
    )


async def _forget(redis_client: RedisClient) -> datetime:
    """Drop what the fixtures' own writes asked for; the time from which a request is the test's."""
    await redis_client.client.delete(JOB_STREAM_KEY.format(kind=JobKind.ALERTS))
    return utc_now()


async def test_a_connector_edit_asks(
    connector_service: ConnectorService,
    project: Project,
    static_connector: Connector,
    redis_client: RedisClient,
    actor: Actor,
) -> None:
    since = await _forget(redis_client)

    await connector_service.update(project.id, static_connector.id, ConnectorUpdate(enabled=False), actor)

    assert await _asked(redis_client, project, since)


async def test_a_country_set_asks(
    connector_service: ConnectorService,
    project: Project,
    static_connector: Connector,
    redis_client: RedisClient,
    actor: Actor,
) -> None:
    since = await _forget(redis_client)

    await connector_service.set_country(project.id, "DE", {static_connector.id: CountryRole.OFF}, actor)

    assert await _asked(redis_client, project, since)


async def test_a_project_edit_asks(
    project_service: ProjectService, project: Project, redis_client: RedisClient, actor: Actor
) -> None:
    since = await _forget(redis_client)

    await project_service.update(project.id, ProjectUpdate(description="edited"), actor)

    assert await _asked(redis_client, project, since)


async def test_a_proxy_upload_asks(
    proxy_service: ProxyService,
    project: Project,
    static_connector: Connector,
    redis_client: RedisClient,
    actor: Actor,
) -> None:
    since = await _forget(redis_client)

    await proxy_service.upload(project.id, static_connector.id, "http://10.0.0.1:8080", actor)

    assert await _asked(redis_client, project, since)
