# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Static proxy provider for file-based or manual proxy lists."""

from api.models.connector import Connector
from api.models.credential import Credential
from api.providers.base import ProxyProvider


class StaticProvider(ProxyProvider):
    """Provider for static proxy lists defined in configuration.

    Supports credential-level default username/password that can be used
    for proxies that don't have their own credentials. Uses placeholders
    {username} and {password} that are resolved at request time by the
    proxy manager.
    """

    def __init__(self, connector: Connector, credential: Credential | None = None) -> None:
        super().__init__(connector, credential)
