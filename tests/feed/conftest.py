# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Shared pieces of the routing feed tests: section factories with fixed timestamps, and a publisher that leads.

Building a section twice gives the same JSON and digest, as a compile of unchanged definitions does, and the publisher
writes to the real Redis test container under a controller lease the fixture takes for it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from api.db.redis import LEASE_KEY, RedisClient
from api.feed.codec import FeedCodec
from api.feed.models import (
    GLOBAL_SECTION,
    ConnectorSection,
    GlobalSection,
    Section,
    connector_section,
)
from api.feed.publisher import FeedPublisher
from api.models.connector import Connector
from api.models.project import Project
from api.models.proxy import Proxy

SECRET = "feed-test-secret"
OWNER = "controller-1"
LEASE = LEASE_KEY.format(name="controller")
COMPILED_AT = datetime(2026, 10, 17, 12, 0, 0)
_CREATED_AT = datetime(2026, 10, 1, 9, 30, 0)


def make_project(**overrides: Any) -> Project:
    fields: dict[str, Any] = {
        "id": "project-1",
        "name": "project",
        "username": "client",
        "password": "secret",
        "created_at": _CREATED_AT,
        "updated_at": _CREATED_AT,
    }
    return Project(**{**fields, **overrides})


def make_connector_section(
    connector_id: str = "connector-1", *, hosts: tuple[str, ...] = ("10.0.0.1",)
) -> ConnectorSection:
    """A static connector of ``project-1`` with one proxy per host."""
    connector = Connector(
        id=connector_id,
        name=connector_id,
        credential_id="credential-1",
        credential_type="static_proxy_provider",
        project_id="project-1",
        created_at=_CREATED_AT,
        updated_at=_CREATED_AT,
    )
    proxies = [
        Proxy(
            id=f"{connector_id}-{index}",
            host=host,
            port=8080,
            connector_id=connector_id,
            created_at=_CREATED_AT,
            updated_at=_CREATED_AT,
        )
        for index, host in enumerate(hosts)
    ]
    return ConnectorSection(connector=connector, proxies=proxies)


def make_sections(*connectors: ConnectorSection, **global_fields: Any) -> dict[str, Section]:
    """A whole feed: the global section and the connector sections given."""
    sections: dict[str, Section] = {GLOBAL_SECTION: GlobalSection(projects=[make_project()], **global_fields)}
    for section in connectors:
        sections[connector_section(section.connector.id)] = section
    return sections


def make_feed(*connector_ids: str) -> dict[str, Section]:
    """A whole feed with a default section for each connector id."""
    return make_sections(*(make_connector_section(connector_id) for connector_id in connector_ids))


@pytest.fixture
def codec() -> FeedCodec:
    return FeedCodec(SECRET)


@pytest.fixture
async def publisher(redis_client: RedisClient, codec: FeedCodec) -> FeedPublisher:
    """A publisher whose process holds the controller lease."""
    await redis_client.client.set(LEASE, OWNER)
    return FeedPublisher(redis_client, codec, lease_key=LEASE, owner_id=OWNER)
