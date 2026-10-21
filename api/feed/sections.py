# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Credential placeholders in proxy rows, filled from the credential and the connector.

Every user fills them here (the feed compiler, the gateway router, the
worker's attribution store), so none can disagree, and a section's credential
keeps only the secrets a gateway reads (:func:`routing_credential`); nothing
here does I/O or changes what it is passed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from api.models.connector import Connector
from api.models.credential import Credential
from api.models.proxy import Proxy
from api.providers.sdk.descriptor import ProxyTypeSpec, TemplateSpec
from api.providers.sdk.templating import TemplateRenderer, resolve_runtime_placeholders

if TYPE_CHECKING:
    from api.providers.sdk.provider import DescriptorProvider


def resolve_credentials(proxy: Proxy, credential: Credential | None, connector: Connector) -> Proxy:
    """A copy of ``proxy`` with the placeholders in its username and password filled in.

    Each takes a non-empty string under its key from the connector's
    configuration, else the credential's, or stays as it is; the copy shares
    ``metadata`` and ``tags`` with ``proxy``, so those are replaced, never
    changed in place.
    """
    values = _placeholder_values(credential, connector)
    return proxy.model_copy(
        update={
            "username": resolve_runtime_placeholders(proxy.username, values),
            "password": resolve_runtime_placeholders(proxy.password, values),
        }
    )


def routing_credential(credential: Credential, provider: DescriptorProvider) -> Credential:
    """``credential`` as a connector section carries it: without the secrets no template reads.

    ``provider`` is the connector's descriptor provider, built from
    ``credential``; a secret field stays only when a template of its proxy type
    (username, password, host, port) reads it, in its text or a part's
    condition, and ``credential`` itself comes back when nothing is left out.
    """
    secrets = {field.key for field in provider.descriptor.credential_fields if field.secret}
    read = _keys_read(provider.proxy_type)
    kept = {key: value for key, value in credential.config.items() if key not in secrets or key in read}
    if len(kept) == len(credential.config):
        return credential
    return credential.model_copy(update={"config": kept})


def _keys_read(ptype: ProxyTypeSpec) -> set[str]:
    """The configuration keys the templates of ``ptype`` read, in their text or their parts' conditions.

    By key alone, whatever the scope: rendering puts a secret back as a bare
    ``{key}`` placeholder, which a value of that key in either scope fills
    (the connector's first).
    """
    paths: set[str] = set()
    for template in (ptype.username, ptype.password, ptype.host, ptype.port_template):
        paths |= TemplateRenderer.referenced_paths(template)
        if isinstance(template, TemplateSpec):
            for part in template.parts:
                if isinstance(part.when, list):
                    paths.update(condition.field for condition in part.when)
                elif part.when is not None:
                    paths.add(part.when.field)
    return {path.partition(".")[2] for path in paths if path.startswith(("credential.", "connector."))}


def _placeholder_values(credential: Credential | None, connector: Connector) -> dict[str, str]:
    """Every non-empty string of the credential's configuration, overlaid by the connector's."""
    values: dict[str, str] = {}
    for config in (credential.config if credential is not None else {}, connector.config):
        values.update((key, value) for key, value in config.items() if isinstance(value, str) and value)
    return values
