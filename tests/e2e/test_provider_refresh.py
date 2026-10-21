# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""A custom provider descriptor saved through one api process reaches another api's registry by its change entry.

The two share only Postgres and Redis: the second's definitions refresher reads the descriptors again as the entry
arrives, with no read in full, and from then on it accepts credentials of the new type, which it refused before.
"""

from __future__ import annotations

import pytest

from api.core.roles import Role
from api.db.redis import RedisClient
from api.db.streams import ChangeKind
from tests.e2e.harness import STARTED_ONLY, Cluster, change_entry, created, until

pytestmark = pytest.mark.e2e

# A vendor whose proxies are a session gateway. It names no vendor API (no
# validation, options or auth calls), so a credential of its type is checked
# by its fields alone and nothing leaves the test machine.
DESCRIPTOR = {
    "id": "e2e_vendor",
    "name": "E2E Vendor",
    "credential_fields": [
        {"key": "username", "label": "Username", "required": True},
        {
            "key": "password",
            "label": "Password",
            "type": "password",
            "secret": True,
            "required": True,
        },
    ],
    "proxy_types": [
        {
            "key": "residential",
            "label": "Residential",
            "mode": "session",
            "host": "gw.e2e-vendor.test",
            "port": 9000,
            "username": {
                "parts": [{"text": "{credential.username}"}, {"text": "session-{session_id}"}]
            },
            "password": "{credential.password}",
        }
    ],
}
# A credential of the new type, as the admin enters it.
CREDENTIAL = {
    "name": "Vendor account",
    "type": "e2e_vendor",
    "config": {"username": "acct", "password": "secret"},
}
# How long the second api may take to hold the descriptor: its refresher wakes
# on the change entry and reads the descriptors again at once. The rest is
# room for a busy machine; its read in full comes only once an hour.
APPLIED_WITHIN_SECONDS = 5.0


async def test_a_descriptor_saved_on_one_api_reaches_the_other_by_its_change_entry(
    cluster: Cluster, redis_client: RedisClient
) -> None:
    first, second = cluster.process(Role.API, "api-a"), cluster.process(Role.API, "api-b")
    await cluster.start(first, second)
    await cluster.ready()
    project = await created(
        first, "/api/v1/projects", {"name": "Vendors", "username": "vendors", "password": "pw"}
    )
    credentials = f"/api/v1/projects/{project['id']}/credentials"
    refused = await second.http.post(credentials, json=CREDENTIAL)
    assert refused.status_code == 422, refused.text

    stored = await created(first, "/api/v1/providers", {"spec": DESCRIPTOR})
    assert stored["id"] == "e2e_vendor"
    saved = await change_entry(redis_client, ChangeKind.DESCRIPTOR, "e2e_vendor")

    # The second api learns of it from its refresher alone: it answers no request meanwhile.
    await until(
        lambda: second.node.registry is not None
        and second.node.registry.get("e2e_vendor") is not None
        and second.has_read("changes", saved),
        within=APPLIED_WITHIN_SECONDS,
        what="the second api's refresher reading the descriptor",
    )
    # By the entry: no read in full since the one it started with.
    assert second.streams()["changes"].full_reads == STARTED_ONLY
    credential = await created(second, credentials, CREDENTIAL)
    assert (credential["type"], credential["name"]) == ("e2e_vendor", "Vendor account")
    catalog = (await second.http.get("/api/v1/providers")).json()["providers"]
    assert "e2e_vendor" in {provider["id"] for provider in catalog}
