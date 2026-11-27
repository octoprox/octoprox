# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""MITM inspection: a project's capped capture stream in Redis (``mitm:requests:{project_id}``), paged newest first and
cleared, and the CA certificate clients trust.

The intercepting gateways append to the stream and generate the CA on the volume they share with the api, which only
reads the certificate.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from api.db.repository import ProjectRepository
from api.models.mitm import MitmRequestRecord, MitmRequestsResponse
from api.services.errors import NotFoundError

if TYPE_CHECKING:
    from api.db.redis import RedisClient
    from api.db.session import SessionFactory


class MitmService:
    """Pages through and clears a project's MITM captures, and finds the CA certificate the gateways generated.

    Args:
        ca_cert_path: Where the gateways write the CA certificate (``tls_mitm_ca_cert_path``).
    """

    def __init__(
        self, session_factory: SessionFactory, redis_client: RedisClient, *, ca_cert_path: str
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis_client
        self._ca_cert_path = Path(ca_cert_path)

    async def requests(
        self, project_id: str, count: int, cursor: str | None
    ) -> MitmRequestsResponse:
        """At most ``count`` of the project's captures, newest first, older than the entry ``cursor`` names when given;
        the cursor of the next page when there are more."""
        await self._require_project(project_id)
        # Fetch count+1 to determine if there's a next page
        raw_records = await self._redis.get_mitm_requests(
            project_id,
            count=count + 1,
            before_id=cursor,
        )

        has_more = len(raw_records) > count
        records_to_return = raw_records[:count]
        records = [MitmRequestRecord.of(raw) for raw in records_to_return]
        next_cursor = records_to_return[-1]["id"] if has_more and records_to_return else None

        return MitmRequestsResponse(records=records, next_cursor=next_cursor)

    async def clear(self, project_id: str) -> None:
        """Delete every capture of the project."""
        await self._require_project(project_id)
        await self._redis.clear_mitm_requests(project_id)

    def ca_certificate(self) -> Path:
        """The CA certificate's file; not found until a gateway has generated it."""
        if not self._ca_cert_path.exists():
            raise NotFoundError("CA certificate not found")
        return self._ca_cert_path

    async def _require_project(self, project_id: str) -> None:
        """Not found when there is no such project; the session closes before Redis is read."""
        async with self._session_factory() as session:
            if await ProjectRepository(session).get_by_id(project_id) is None:
                raise NotFoundError("Project not found")
