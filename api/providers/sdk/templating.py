# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Template rendering for descriptors.

Grammar
-------
``{credential.username}``            value from the credential config
``{connector.country_code|lower}``   with a filter (``lower``, ``upper``, ``urlencode``)
``{connector.country_code|or:any}``  fallback when the value is empty
``{session_id}`` ``{index}`` ``{port}`` ``{discovered_ip}`` ``{auth.token}`` ``{item.name}``

Rendering is plain string substitution — there is no expression language and
no attribute access, so a descriptor cannot reach anything that is not
explicitly placed in the :class:`RenderContext`.

Two modes exist. ``full`` substitutes every value and is used for vendor API
calls. ``proxy`` is used when building proxy rows that are persisted: fields
flagged ``secret`` are emitted as ``{key}`` runtime placeholders, which the
proxy manager resolves per request, so secrets never land in the proxies
table. Non-secret values are baked in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

from api.providers.sdk.descriptor import Condition, Template, TemplateSpec

RenderMode = Literal["full", "proxy"]

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?)((?:\|[a-zA-Z_]+(?::[^}|]*)?)*)\}")


class TemplateError(ValueError):
    """Raised for malformed templates or unknown filters."""


@dataclass
class RenderContext:
    """Everything a template may reference."""

    credential: dict[str, Any] = field(default_factory=dict)
    connector: dict[str, Any] = field(default_factory=dict)
    auth: dict[str, Any] = field(default_factory=dict)
    item: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    index: int | None = None
    port: int | None = None
    discovered_ip: str | None = None
    secret_keys: frozenset[str] = frozenset()

    def lookup(self, path: str) -> Any:
        """Resolve a dotted variable path; unknown paths resolve to ``None``."""
        namespace, _, key = path.partition(".")
        if not key:
            scalars = {
                "session_id": self.session_id,
                "index": self.index,
                "port": self.port,
                "discovered_ip": self.discovered_ip,
            }
            return scalars.get(namespace)
        namespaces: dict[str, dict[str, Any]] = {
            "credential": self.credential,
            "connector": self.connector,
            "auth": self.auth,
            "item": self.item,
        }
        source = namespaces.get(namespace)
        if source is None:
            return None
        return source.get(key)

    def is_secret(self, path: str) -> bool:
        namespace, _, key = path.partition(".")
        return namespace in ("credential", "connector") and key in self.secret_keys

    def with_slot(
        self,
        *,
        session_id: str | None = None,
        index: int | None = None,
        port: int | None = None,
        discovered_ip: str | None = None,
    ) -> RenderContext:
        """Copy with per-slot variables set."""
        return RenderContext(
            credential=self.credential,
            connector=self.connector,
            auth=self.auth,
            item=self.item,
            session_id=session_id if session_id is not None else self.session_id,
            index=index if index is not None else self.index,
            port=port if port is not None else self.port,
            discovered_ip=discovered_ip if discovered_ip is not None else self.discovered_ip,
            secret_keys=self.secret_keys,
        )

    def with_item(self, item: dict[str, Any]) -> RenderContext:
        return RenderContext(
            credential=self.credential,
            connector=self.connector,
            auth=self.auth,
            item=item,
            session_id=self.session_id,
            index=self.index,
            port=self.port,
            discovered_ip=self.discovered_ip,
            secret_keys=self.secret_keys,
        )

    def with_auth(self, auth: dict[str, Any]) -> RenderContext:
        return RenderContext(
            credential=self.credential,
            connector=self.connector,
            auth=auth,
            item=self.item,
            session_id=self.session_id,
            index=self.index,
            port=self.port,
            discovered_ip=self.discovered_ip,
            secret_keys=self.secret_keys,
        )

    def secret_values(self) -> list[str]:
        """Concrete secret strings, for log redaction."""
        values: list[str] = []
        for source in (self.credential, self.connector):
            for key, value in source.items():
                if key in self.secret_keys and isinstance(value, str) and value:
                    values.append(value)
        token = self.auth.get("token")
        if isinstance(token, str) and token:
            values.append(token)
        return values


def _apply_filter(value: str, name: str, arg: str | None) -> str:
    if name == "lower":
        return value.lower()
    if name == "upper":
        return value.upper()
    if name == "urlencode":
        return quote(value, safe="")
    if name == "or":
        return value if value else (arg or "")
    raise TemplateError(f"unknown template filter '{name}'")


def _parse_filters(spec: str) -> list[tuple[str, str | None]]:
    filters: list[tuple[str, str | None]] = []
    for raw in spec.split("|"):
        if not raw:
            continue
        name, _, arg = raw.partition(":")
        filters.append((name, arg if _ else None))
    return filters


class TemplateRenderer:
    """Renders descriptor templates against a :class:`RenderContext`."""

    def render(self, template: Template | None, ctx: RenderContext, mode: RenderMode = "full") -> str:
        if template is None:
            return ""
        if isinstance(template, TemplateSpec):
            parts = [
                self.render_string(part.text, ctx, mode)
                for part in template.parts
                if self.evaluate(part.when, ctx)
            ]
            return template.separator.join(p for p in parts if p != "")
        return self.render_string(template, ctx, mode)

    def render_string(self, template: str, ctx: RenderContext, mode: RenderMode = "full") -> str:
        def substitute(match: re.Match[str]) -> str:
            path = match.group(1)
            filters = _parse_filters(match.group(2))
            if mode == "proxy" and ctx.is_secret(path):
                # Secrets are resolved at request time by the proxy manager from
                # the flat credential+connector config namespace.
                _, _, key = path.partition(".")
                return "{" + key + "}"
            value = ctx.lookup(path)
            text = "" if value is None else str(value)
            for name, arg in filters:
                text = _apply_filter(text, name, arg)
            return text

        return _PLACEHOLDER.sub(substitute, template)

    def render_mapping(
        self, mapping: dict[str, str], ctx: RenderContext, mode: RenderMode = "full", *, drop_empty: bool = False
    ) -> dict[str, str]:
        rendered = {key: self.render_string(value, ctx, mode) for key, value in mapping.items()}
        if drop_empty:
            return {k: v for k, v in rendered.items() if v != ""}
        return rendered

    def render_json(self, value: Any, ctx: RenderContext) -> Any:
        """Render every string inside a JSON-like structure (used for request bodies)."""
        if isinstance(value, str):
            return self.render_string(value, ctx, "full")
        if isinstance(value, dict):
            return {k: self.render_json(v, ctx) for k, v in value.items()}
        if isinstance(value, list):
            return [self.render_json(v, ctx) for v in value]
        return value

    def evaluate(self, condition: Condition | None, ctx: RenderContext) -> bool:
        if condition is None:
            return True
        return condition.evaluate(ctx.lookup(condition.field))

    @staticmethod
    def referenced_paths(template: Template | None) -> set[str]:
        """Variable paths a template references (for static validation)."""
        if template is None:
            return set()
        texts = [template] if isinstance(template, str) else [p.text for p in template.parts]
        paths: set[str] = set()
        for text in texts:
            for match in _PLACEHOLDER.finditer(text):
                paths.add(match.group(1))
        return paths


def resolve_runtime_placeholders(text: str | None, values: dict[str, Any]) -> str | None:
    """Replace ``{key}`` runtime placeholders with concrete config values.

    Mirrors ``ProxyManager.resolve_proxy_credentials`` so the SDK can build a
    fully-resolved proxy URL for IP discovery without depending on the manager.
    """
    if text is None or "{" not in text:
        return text
    resolved = text
    for key, value in values.items():
        if isinstance(value, str) and value:
            resolved = resolved.replace("{" + key + "}", value)
    return resolved
