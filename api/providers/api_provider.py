# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""API-based proxy provider for external proxy services."""

from api.models.connector import Connector
from api.models.credential import Credential
from api.providers.base import ProxyProvider


class APIProvider(ProxyProvider):
    """Provider for API-based proxy services."""

    def __init__(self, connector: Connector, credential: Credential | None = None) -> None:
        super().__init__(connector, credential)
        self._url = connector.config.get("url", "")
        self._api_key = connector.config.get("api_key", "")
        self._headers = connector.config.get("headers", {})
