# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Base class for proxy providers."""

from api.models.connector import Connector
from api.models.credential import Credential


class ProxyProvider:
    """Base class for proxy providers.

    All providers take a connector and credential.
    """

    def __init__(self, connector: Connector, credential: Credential | None = None) -> None:
        self.connector = connector
        self.credential = credential
