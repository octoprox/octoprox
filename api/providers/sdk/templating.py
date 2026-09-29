# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Template rendering for descriptors.

Grammar
-------
``{credential.username}``            value from the credential config
``{connector.country_code|lower}``   with a filter (``lower``, ``upper``, ``urlencode``)
``{connector.country_code|or:any}``  fallback when the value is empty
``{geo.country}`` ``{geo.state}`` ``{geo.city}``  the place this slot or request targets
``{session_id}`` ``{index}`` ``{port}`` ``{discovered_ip}`` ``{auth.token}`` ``{item.name}``

The ``geo`` namespace is the place the proxy being rendered is targeted at,
and is the only thing a template should read the country from. It is set
per render (:meth:`RenderContext.with_place`):
for a pooled connector each slot group is rendered with one of the
connector's listed countries (or the country a client asked for on demand),
so ``geo.country`` is that group's country; for a dynamic-sessions request
it is the country rendered for that request (the ``-cc-`` code or the
allow-list pick) and ``geo.state`` / ``geo.city`` are what the request asked
for below it. ``geo.state`` is the ISO 3166-2 subdivision code (``NY``),
``geo.state_name`` that state's name as a slug (``new_york``, US states only)
and ``geo.city`` the city slug (``los_angeles``). The ``nospace`` filter
drops the underscores for vendors that want ``losangeles``.

A config value that is a list (a multi-country field) renders as its single
element, or joined with commas when it holds several. The connector's country
field is such a list and is never narrowed: a template that reads it gets
the whole list, which is why the country goes through ``geo.country``.

Rendering is plain string substitution - there is no expression language and
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

from api.models.location import LocationTarget
from api.providers.sdk.descriptor import (
    Condition,
    ProviderDescriptor,
    ProxyTypeSpec,
    Template,
    TemplateSpec,
)

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
    # The place this render targets: the slot group's country, or the whole
    # location a dynamic request asked for. Empty when nothing is targeted.
    geo: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    index: int | None = None
    port: int | None = None
    discovered_ip: str | None = None
    secret_keys: frozenset[str] = frozenset()

    @property
    def target_country(self) -> str | None:
        """The country this render targets (``geo.country``), or None when none."""
        country = self.geo.get("country")
        return country if isinstance(country, str) and country else None

    def _copy(self, **changes: Any) -> RenderContext:
        values: dict[str, Any] = {
            "credential": self.credential,
            "connector": self.connector,
            "auth": self.auth,
            "item": self.item,
            "geo": self.geo,
            "session_id": self.session_id,
            "index": self.index,
            "port": self.port,
            "discovered_ip": self.discovered_ip,
            "secret_keys": self.secret_keys,
        }
        values.update(changes)
        return RenderContext(**values)

    def lookup(self, path: str) -> Any:
        """Resolve a dotted variable path; unknown paths resolve to ``None``.

        List values collapse to a scalar: ``None`` when empty, the element
        when single, otherwise a comma-joined string.
        """
        value = self._lookup_raw(path)
        if isinstance(value, list):
            items = [str(v) for v in value if v is not None and str(v) != ""]
            if not items:
                return None
            return items[0] if len(items) == 1 else ",".join(items)
        return value

    def _lookup_raw(self, path: str) -> Any:
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
            "geo": self.geo,
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
        return self._copy(
            session_id=session_id if session_id is not None else self.session_id,
            index=index if index is not None else self.index,
            port=port if port is not None else self.port,
            discovered_ip=discovered_ip if discovered_ip is not None else self.discovered_ip,
        )

    @staticmethod
    def _geo_of(target: LocationTarget | None) -> dict[str, str]:
        if target is None:
            return {}
        return {
            "country": target.country or "",
            "state": target.state or "",
            "state_name": target.state_name or "",
            "city": target.city or "",
        }

    def with_place(self, target: LocationTarget | None) -> RenderContext:
        """Copy targeted at ``target``, exposed as the ``geo`` namespace.

        A pooled slot group is rendered with a country alone, one of the
        connector's listed countries or the one a client asked for on demand.
        A dynamic request is rendered with everything it asked for, so a
        template can build a vendor's ``us_new_york`` from ``geo.country`` and
        ``geo.state_name``. None targets nothing and empties the namespace.
        """
        return self._copy(geo=self._geo_of(target))

    def with_item(self, item: dict[str, Any]) -> RenderContext:
        return self._copy(item=item)

    def with_auth(self, auth: dict[str, Any]) -> RenderContext:
        return self._copy(auth=auth)

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
    if name == "nospace":
        # A place slug without its underscores: ``los_angeles`` becomes ``losangeles``.
        return "".join(ch for ch in value if ch.isalnum())
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

    def evaluate(self, condition: Condition | list[Condition] | None, ctx: RenderContext) -> bool:
        """Evaluate one condition, or all of a list (every one must hold)."""
        if condition is None:
            return True
        if isinstance(condition, list):
            return all(c.evaluate(ctx.lookup(c.field)) for c in condition)
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


def country_field_key(descriptor: ProviderDescriptor, ptype: ProxyTypeSpec) -> str | None:
    """Connector config key through which ``ptype`` geo-targets its upstream credentials.

    Returns the key of the descriptor's connector field of type ``country``
    (or using the ``countries`` preset) when the proxy type's username,
    password, host or port template reads ``{geo.country}``, the country a
    slot or request targets. None when none of them carries a country, or
    the descriptor has no country field to draw the list from. Templates
    that depend on list items are excluded, since list-mode credentials come
    from the vendor.

    Proxy types with such a key are provisioned as one slot group per
    country, and can take unlisted countries on demand from ``-cc-``
    requests.
    """
    paths = TemplateRenderer.referenced_paths(ptype.username) | TemplateRenderer.referenced_paths(
        ptype.password
    )
    # Regional gateways encode the country in the host (or port) rather than the credentials.
    paths |= TemplateRenderer.referenced_paths(ptype.host) | TemplateRenderer.referenced_paths(ptype.port_template)
    if any(path.startswith("item.") for path in paths):
        return None
    if "geo.country" not in paths:
        return None
    field_spec = descriptor.country_field()
    return field_spec.key if field_spec is not None else None


def _credential_paths(ptype: ProxyTypeSpec) -> set[str]:
    """Every variable path the type's credentials and endpoint templates reference."""
    return (
        TemplateRenderer.referenced_paths(ptype.username)
        | TemplateRenderer.referenced_paths(ptype.password)
        | TemplateRenderer.referenced_paths(ptype.host)
        | TemplateRenderer.referenced_paths(ptype.port_template)
    )


@dataclass(frozen=True)
class TargetingSupport:
    """Which sub-country levels a proxy type can render into a request.

    Derived from the templates: a type targets a state when its credentials
    reference ``geo.state`` or ``geo.state_name``, and a city when
    they reference ``geo.city``. ``state_by_name`` says the state reaches
    the vendor only as a name, so a subdivision with no name on record (any
    non-US state) cannot be rendered and the type does not serve it.
    """

    state: bool = False
    city: bool = False
    state_by_name: bool = False

    def serves(self, target: LocationTarget, spec: ProxyTypeSpec) -> bool:
        """Whether a request for ``target`` can be rendered faithfully by this type.

        Faithfully means every level the client named reaches the vendor:
        a request is never quietly widened to the country.
        """
        if target.state:
            if not self.state:
                return False
            if self.state_by_name and target.state_name is None:
                return False
        if target.city and not self.city:
            return False
        constraints = spec.targeting
        if constraints is not None:
            if constraints.city_requires_state and target.city and not target.state:
                return False
            if constraints.state_or_city and target.city and target.state:
                return False
        return True


def targeting_support(ptype: ProxyTypeSpec) -> TargetingSupport:
    """What ``ptype``'s templates can render of a request's state and city."""
    paths = _credential_paths(ptype)
    by_code = "geo.state" in paths
    by_name = "geo.state_name" in paths
    return TargetingSupport(
        state=by_code or by_name,
        city="geo.city" in paths,
        state_by_name=by_name and not by_code,
    )


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
