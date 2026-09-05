# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for provider SDK tests.

Vendor APIs are replaced by ``httpx.MockTransport`` handlers; the egress
policy is relaxed so no DNS lookups happen. Discovery requests (normally
routed *through* a proxy) get their own mock factory so tests can assert on
the proxy URL that would have been used.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest

from api.models.connector import Connector
from api.models.credential import Credential
from api.providers.sdk.descriptor import ProviderDescriptor
from api.providers.sdk.egress import EgressPolicy
from api.providers.sdk.loader import load_builtin_descriptors
from api.providers.sdk.provider import SdkRuntime

Handler = Callable[[httpx.Request], httpx.Response]

TEST_POLICY = EgressPolicy(allow_http=True, allow_private=True, pin_dns=False)


@dataclass
class MockVendor:
    """Records requests made to the vendor API and to discovery endpoints."""

    api_handler: Handler
    discovery_handler: Handler | None = None
    api_requests: list[httpx.Request] = field(default_factory=list)
    discovery_requests: list[tuple[str, httpx.Request]] = field(default_factory=list)

    def runtime(self) -> SdkRuntime:
        def api_client() -> httpx.AsyncClient:
            def handler(request: httpx.Request) -> httpx.Response:
                self.api_requests.append(request)
                return self.api_handler(request)

            return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)

        def proxied_client(proxy_url: str, timeout: float) -> httpx.AsyncClient:
            def handler(request: httpx.Request) -> httpx.Response:
                self.discovery_requests.append((proxy_url, request))
                assert self.discovery_handler is not None
                return self.discovery_handler(request)

            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

        return SdkRuntime(
            egress_policy=TEST_POLICY,
            client_factory=api_client,
            proxied_client_factory=proxied_client,
        )


@pytest.fixture
def builtins() -> dict[str, ProviderDescriptor]:
    return {d.descriptor.id: d.descriptor for d in load_builtin_descriptors()}


def make_credential(type_id: str, config: dict[str, object], credential_id: str = "cred-1") -> Credential:
    return Credential(id=credential_id, name=f"{type_id} credential", type=type_id, project_id="proj-1", config=config)


def make_connector(type_id: str, config: dict[str, object], connector_id: str = "conn-1") -> Connector:
    return Connector(
        id=connector_id,
        name=f"{type_id} connector",
        credential_id="cred-1",
        credential_type=type_id,
        project_id="proj-1",
        config=config,
    )


def json_response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)
