# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Declarative provider descriptor schema.

A descriptor is data, not code. It is authored as YAML (shipped built-ins,
operator-mounted files) or JSON (admin UI, stored in Postgres) and validated
into these Pydantic models. Everything the engine needs to provision proxies
for a vendor and to talk to that vendor's management API is expressed here:

* ``credential_fields`` / ``connector_fields`` drive the forms and validation.
* ``proxy_types`` describe how a connector's slots become proxy endpoints
  (``session``: gateway + fresh session id per slot, ``port``: one exit IP per
  slot exposed via a gateway port or a pinned-IP username, ``list``: the
  vendor API hands back concrete host:port entries).
* ``auth`` / ``options`` / ``validation`` are declarative HTTP calls with
  JMESPath extraction for two-step logins, dynamic select options (zones,
  sub-users, entry nodes) and credential checks.

Templates use ``{namespace.key}`` placeholders; see
:mod:`api.providers.sdk.templating` for the grammar.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.models.proxy import ProxyProtocol

SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")
FIELD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

FieldType = Literal["text", "password", "number", "select", "boolean", "textarea", "url", "country"]
ProxyMode = Literal["session", "port", "list"]
PortStrategy = Literal["sequential", "fixed"]
FieldScope = Literal["credential", "connector"]

# Namespaces a template may reference. ``credential``/``connector`` come from
# stored config; the rest are provided by the engine at render time.
TEMPLATE_NAMESPACES = frozenset(
    {"credential", "connector", "auth", "item", "session_id", "index", "port", "discovered_ip"}
)


def split_scoped_key(path: str) -> tuple[FieldScope, str]:
    """Split ``'credential.proxy_type'`` into ``('credential', 'proxy_type')``."""
    scope, _, key = path.partition(".")
    if scope not in ("credential", "connector") or not key:
        raise ValueError(f"'{path}' must look like 'credential.<key>' or 'connector.<key>'")
    return scope, key  # type: ignore[return-value]


class OptionSpec(BaseModel):
    """A static select option."""

    model_config = ConfigDict(extra="forbid")

    value: str
    label: str
    description: str | None = None


class Condition(BaseModel):
    """Predicate over a template variable, e.g. ``credential.proxy_type in [...]``.

    With neither ``equals`` nor ``in`` set the condition is truthiness of the
    referenced value (non-empty string).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    field: str = Field(description="Variable path such as 'connector.country_code'")
    equals: str | None = None
    in_: list[str] | None = Field(default=None, alias="in")
    negate: bool = False

    def evaluate(self, value: Any) -> bool:
        """Evaluate against an already-resolved value."""
        text = "" if value is None else str(value)
        if self.equals is not None:
            result = text == self.equals
        elif self.in_ is not None:
            result = text in self.in_
        else:
            result = bool(text)
        return not result if self.negate else result


class OptionRef(BaseModel):
    """Points at an extra value carried by another field's currently selected option."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(description="Key of a sibling field with dynamic options")
    extra: str = Field(description="Name of the option extra to read")


class FieldSpec(BaseModel):
    """One form field on a credential or connector."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    type: FieldType = "text"
    required: bool = False
    secret: bool = False
    readonly: bool = Field(
        default=False, description="Shown but not editable; typically filled from another field's option"
    )
    default: str | int | float | bool | None = None
    placeholder: str | None = None
    help: str | None = None
    group: str = "general"
    options: list[OptionSpec] = Field(default_factory=list)
    options_preset: Literal["countries"] | None = None
    options_from: str | None = Field(
        default=None, description="Name of an entry in descriptor.options"
    )
    options_from_when: Condition | None = Field(
        default=None,
        description="Use options_from only when this holds; otherwise fall back to options/options_preset",
    )
    empty_label: str | None = Field(
        default=None, description="Label of the 'no value' choice offered for optional dynamic selects"
    )
    fill: dict[str, str] = Field(
        default_factory=dict,
        description="On select: set other fields (key) from option extras (value)",
    )
    min: float | None = None
    max: float | None = None
    max_from_option: list[OptionRef] = Field(
        default_factory=list,
        description="Number fields: cap taken from the first sibling option extra that resolves",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Connector keys whose values the options source needs (computed when served)",
    )
    pattern: str | None = None
    transform: Literal["upper", "lower", "strip"] | None = None
    show_when: Condition | None = None

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        if not FIELD_KEY_PATTERN.match(value):
            raise ValueError(f"field key '{value}' must be snake_case (a-z, 0-9, _)")
        return value

    @field_validator("pattern")
    @classmethod
    def _validate_pattern(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regex pattern: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _validate_option_sources(self) -> FieldSpec:
        static_sources = [bool(self.options), self.options_preset is not None]
        if sum(static_sources) > 1:
            raise ValueError(f"field '{self.key}': options and options_preset are mutually exclusive")
        if self.options_from is not None and any(static_sources) and self.options_from_when is None:
            raise ValueError(
                f"field '{self.key}': combining options_from with static options requires options_from_when"
            )
        if self.options_from_when is not None and (self.options_from is None or not any(static_sources)):
            raise ValueError(
                f"field '{self.key}': options_from_when needs both options_from and a static fallback"
            )
        if self.type == "select" and not any(static_sources) and self.options_from is None:
            raise ValueError(f"field '{self.key}': select fields need options")
        if self.fill and self.options_from is None:
            raise ValueError(f"field '{self.key}': fill requires options_from")
        if self.max_from_option and self.type != "number":
            raise ValueError(f"field '{self.key}': max_from_option only applies to number fields")
        return self

    @property
    def has_options(self) -> bool:
        return bool(self.options) or self.options_preset is not None or self.options_from is not None


class PaginationSpec(BaseModel):
    """Follow a "next page" URL found in each response until it is empty."""

    model_config = ConfigDict(extra="forbid")

    next_url: str = Field(description="JMESPath to the absolute URL of the next page")
    max_pages: int = Field(default=1000, ge=1)


class HttpCallSpec(BaseModel):
    """A declarative HTTP request against a vendor API.

    All string values (url, headers, params, body) are templates rendered with
    full access to credential and connector config. Params whose rendered
    value is empty are dropped, which is how optional filters are expressed.
    """

    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    auth: str | None = Field(default=None, description="Auth flow name from descriptor.auth")
    paginate: PaginationSpec | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https"):
            raise ValueError(f"url '{value}' must start with http:// or https://")
        if not parts.netloc:
            raise ValueError(f"url '{value}' has no host")
        return value

    @property
    def host(self) -> str:
        """Hostname portion of the URL template (template variables are not allowed in the host)."""
        return urlsplit(self.url).hostname or ""


class AuthFlowSpec(BaseModel):
    """Two-step authentication: run ``call``, extract a token, expose ``{auth.token}``."""

    model_config = ConfigDict(extra="forbid")

    call: HttpCallSpec
    token_path: str = Field(default="token", description="JMESPath to the token in the response")
    ttl_seconds: int = Field(default=3000, ge=1)

    @model_validator(mode="after")
    def _no_nested_auth(self) -> AuthFlowSpec:
        if self.call.auth is not None:
            raise ValueError("an auth flow's call cannot itself require auth")
        return self


class MapRule(BaseModel):
    """One rule of a value mapping: match on a string, produce ``to``."""

    model_config = ConfigDict(extra="forbid")

    equals: str | None = None
    starts_with: str | None = None
    regex: str | None = None
    to: str

    @model_validator(mode="after")
    def _exactly_one_matcher(self) -> MapRule:
        matchers = [self.equals is not None, self.starts_with is not None, self.regex is not None]
        if sum(matchers) != 1:
            raise ValueError("a map rule needs exactly one of equals, starts_with, regex")
        if self.regex is not None:
            try:
                re.compile(self.regex)
            except re.error as exc:
                raise ValueError(f"invalid regex: {exc}") from exc
        return self

    def matches(self, text: str) -> bool:
        if self.equals is not None:
            return text == self.equals
        if self.starts_with is not None:
            return text.startswith(self.starts_with)
        return re.search(self.regex or "", text) is not None


class ValueExpr(BaseModel):
    """JMESPath extraction with an optional string mapping and default."""

    model_config = ConfigDict(extra="forbid")

    path: str
    map: list[MapRule] = Field(default_factory=list)
    default: str | None = None


ValueSource = str | ValueExpr


class EnrichSpec(BaseModel):
    """Per-option follow-up call whose extracted values are merged into the option.

    Templates inside ``call`` may reference ``{item.<key>}`` for the option
    being enriched (its value, label and extras).
    """

    model_config = ConfigDict(extra="forbid")

    call: HttpCallSpec
    when: str | None = Field(default=None, description="JMESPath predicate over the option")
    merge: dict[str, ValueSource]


class OptionsSourceSpec(BaseModel):
    """Dynamic select options fetched from the vendor API.

    ``label`` and ``description`` are evaluated over the option document
    (``value``, ``label``, the raw item's fields and every ``extra``, after
    enrichment), so a description can mention enriched values. With
    ``group_by_value`` items sharing a value collapse into one option whose
    ``count`` extra (``count_key``) holds how many there were, e.g. exit IPs
    per country.
    """

    model_config = ConfigDict(extra="forbid")

    call: HttpCallSpec
    items: str = Field(default="@", description="JMESPath selecting the list of items")
    value: ValueSource
    label: ValueSource | None = None
    description: ValueSource | None = None
    extra: dict[str, ValueSource] = Field(default_factory=dict)
    enrich: list[EnrichSpec] = Field(default_factory=list)
    filter: str | None = Field(default=None, description="JMESPath predicate over the option")
    group_by_value: bool = Field(default=False, description="Collapse items with the same value")
    count_key: str = Field(default="count", description="Extra holding the group size when grouping")
    cache_seconds: int = Field(default=300, ge=0)


class ValidationSpec(BaseModel):
    """Credential check run on create/update; captured values are stored in the config."""

    model_config = ConfigDict(extra="forbid")

    call: HttpCallSpec
    success: str | None = Field(
        default=None, description="JMESPath predicate over the response body; 2xx alone if unset"
    )
    capture: dict[str, ValueSource] = Field(default_factory=dict)
    error_message: str = "Credential validation failed"
    when: Condition | None = Field(
        default=None, description="Only validate when this condition holds (e.g. optional API key set)"
    )


class TemplatePart(BaseModel):
    """A segment of a composed template, included only when ``when`` holds."""

    model_config = ConfigDict(extra="forbid")

    text: str
    when: Condition | None = None


class TemplateSpec(BaseModel):
    """A template composed of conditional parts joined by ``separator``."""

    model_config = ConfigDict(extra="forbid")

    separator: str = "-"
    parts: list[TemplatePart] = Field(min_length=1)


Template = str | TemplateSpec


class SessionIdSpec(BaseModel):
    """How fresh session identifiers are generated for ``session`` mode."""

    model_config = ConfigDict(extra="forbid")

    length: int = Field(default=12, ge=1, le=64)
    alphabet: Literal["lower_digits", "digits", "alnum", "lower"] = "lower_digits"
    prefix: str = ""


class IpDiscoverySpec(BaseModel):
    """Learn a port-mode slot's exit IP by requesting ``url`` through the proxy."""

    model_config = ConfigDict(extra="forbid")

    url: str = "https://httpbin.org/ip"
    ip_path: str = Field(default="origin", description="JMESPath into the JSON body; '@text' for raw text")
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries_per_slot: int = Field(
        default=3, ge=1, description="Fixed-port strategy: attempts per slot when the IP duplicates one we have"
    )
    max_consecutive_failures: int = Field(
        default=3, ge=1, description="Stop provisioning after this many slots in a row fail to discover an IP"
    )
    max_consecutive_duplicates: int = Field(
        default=3, ge=1, description="Sequential-port strategy: stop after this many ports in a row return known IPs"
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"discovery url '{value}' must be an absolute http(s) URL")
        return value


class KnownIpsSpec(BaseModel):
    """Port mode: the vendor API lists the exit IPs the account owns."""

    model_config = ConfigDict(extra="forbid")

    call: HttpCallSpec
    items: str = "@"
    ip: ValueSource = "ip"
    country: ValueSource | None = None


class ListSourceSpec(BaseModel):
    """List mode: the vendor API returns concrete proxy endpoints."""

    model_config = ConfigDict(extra="forbid")

    call: HttpCallSpec
    items: str = "@"
    host: ValueSource
    port: ValueSource
    username: ValueSource | None = None
    password: ValueSource | None = None
    protocol: ValueSource | None = None
    country: ValueSource | None = None
    identity: ValueSource | None = Field(
        default=None, description="Stable id per entry; defaults to host:port"
    )
    filter: str | None = None


class ProxyTypeSpec(BaseModel):
    """How one kind of proxy (residential, ISP, ...) is provisioned."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    key: str
    label: str
    mode: ProxyMode
    host: Template | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    protocol: ProxyProtocol = ProxyProtocol.HTTP
    username: Template | None = None
    password: Template | None = None
    port_strategy: PortStrategy = "sequential"
    discovery: IpDiscoverySpec | None = None
    known_ips: KnownIpsSpec | None = None
    source: ListSourceSpec | None = Field(
        default=None, alias="list", description="List-mode source (YAML key: list)"
    )
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    count_field: str = Field(
        default="connector.num_proxies",
        description="Variable holding the desired slot count (list mode: optional cap)",
    )
    healthcheck_url: str | None = None

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        if not FIELD_KEY_PATTERN.match(value):
            raise ValueError(f"proxy type key '{value}' must be snake_case")
        return value

    @model_validator(mode="after")
    def _validate_mode(self) -> ProxyTypeSpec:
        if self.mode in ("session", "port"):
            if self.host is None or self.port is None:
                raise ValueError(f"proxy type '{self.key}': {self.mode} mode needs host and port")
            if self.username is None:
                raise ValueError(f"proxy type '{self.key}': {self.mode} mode needs a username template")
            if self.source is not None:
                raise ValueError(f"proxy type '{self.key}': list source only applies to list mode")
        if self.mode == "port" and self.discovery is None:
            self.discovery = IpDiscoverySpec()
        if self.mode == "list":
            if self.source is None:
                raise ValueError(f"proxy type '{self.key}': list mode needs a list source")
            if self.known_ips is not None or self.discovery is not None:
                raise ValueError(f"proxy type '{self.key}': list mode does not use discovery")
        if self.mode == "session" and (self.known_ips is not None or self.discovery is not None):
            raise ValueError(f"proxy type '{self.key}': session mode does not use discovery")
        return self


class ProviderDescriptor(BaseModel):
    """Complete declarative description of a proxy provider."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str = ""
    version: int = Field(default=1, ge=1)
    logo: str | None = Field(default=None, description="data: URI of an SVG/PNG logo")
    docs_url: str | None = None
    beta: bool = False
    credential_fields: list[FieldSpec] = Field(default_factory=list)
    connector_fields: list[FieldSpec] = Field(default_factory=list)
    proxy_type_field: str | None = Field(
        default=None,
        description="Variable selecting the proxy type, e.g. 'credential.proxy_type'",
    )
    proxy_types: list[ProxyTypeSpec] = Field(min_length=1)
    session_id: SessionIdSpec = Field(default_factory=SessionIdSpec)
    auth: dict[str, AuthFlowSpec] = Field(default_factory=dict)
    options: dict[str, OptionsSourceSpec] = Field(default_factory=dict)
    validation: ValidationSpec | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not SLUG_PATTERN.match(value):
            raise ValueError(
                "id must be 2-41 chars, lowercase letters, digits, '-' or '_', starting with a letter"
            )
        return value

    @field_validator("logo")
    @classmethod
    def _validate_logo(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("data:image/"):
            raise ValueError("logo must be a data:image/... URI")
        if value is not None and len(value) > 200_000:
            raise ValueError("logo must be under 200KB")
        return value

    @model_validator(mode="after")
    def _validate_cross_references(self) -> ProviderDescriptor:
        self._check_unique_keys()
        self._check_proxy_type_selector()
        self._check_named_references()
        return self

    def _check_unique_keys(self) -> None:
        for scope, fields in (("credential", self.credential_fields), ("connector", self.connector_fields)):
            keys = [f.key for f in fields]
            duplicates = {k for k in keys if keys.count(k) > 1}
            if duplicates:
                raise ValueError(f"duplicate {scope} field keys: {sorted(duplicates)}")
        type_keys = [t.key for t in self.proxy_types]
        duplicates = {k for k in type_keys if type_keys.count(k) > 1}
        if duplicates:
            raise ValueError(f"duplicate proxy type keys: {sorted(duplicates)}")

    def _check_proxy_type_selector(self) -> None:
        if len(self.proxy_types) > 1 and self.proxy_type_field is None:
            raise ValueError("proxy_type_field is required when there is more than one proxy type")
        if self.proxy_type_field is not None:
            scope, key = split_scoped_key(self.proxy_type_field)
            if self.find_field(scope, key) is None:
                raise ValueError(f"proxy_type_field refers to unknown field '{self.proxy_type_field}'")

    def _check_named_references(self) -> None:
        for scope, fields in (("credential", self.credential_fields), ("connector", self.connector_fields)):
            keys = {f.key for f in fields}
            for field in fields:
                if field.options_from is not None and field.options_from not in self.options:
                    raise ValueError(
                        f"field '{field.key}' references unknown options source '{field.options_from}'"
                    )
                for ref in field.max_from_option:
                    sibling = self.find_field(scope, ref.field)  # type: ignore[arg-type]
                    if sibling is None or sibling.options_from is None:
                        raise ValueError(
                            f"field '{field.key}': max_from_option must reference a sibling with options_from"
                        )
                for target in field.fill:
                    if target not in keys:
                        raise ValueError(f"field '{field.key}': fill targets unknown field '{target}'")
        for call in self.iter_calls():
            if call.auth is not None and call.auth not in self.auth:
                raise ValueError(f"call to {call.url} references unknown auth flow '{call.auth}'")

    # --- helpers -----------------------------------------------------------

    def find_field(self, scope: FieldScope, key: str) -> FieldSpec | None:
        fields = self.credential_fields if scope == "credential" else self.connector_fields
        return next((f for f in fields if f.key == key), None)

    def secret_keys(self) -> set[str]:
        """Config keys whose values must never be baked into stored proxies."""
        return {f.key for f in (*self.credential_fields, *self.connector_fields) if f.secret}

    def get_proxy_type(self, key: str) -> ProxyTypeSpec | None:
        return next((t for t in self.proxy_types if t.key == key), None)

    def resolve_proxy_type(
        self, credential_config: dict[str, Any], connector_config: dict[str, Any]
    ) -> ProxyTypeSpec:
        """Pick the proxy type for a credential/connector pair.

        Raises ``ValueError`` when the selector field holds an unknown value.
        """
        if self.proxy_type_field is None:
            return self.proxy_types[0]
        scope, key = split_scoped_key(self.proxy_type_field)
        source = credential_config if scope == "credential" else connector_config
        value = source.get(key)
        if value is None:
            field = self.find_field(scope, key)
            value = field.default if field is not None else None
        spec = self.get_proxy_type(str(value)) if value is not None else None
        if spec is None:
            raise ValueError(f"unknown proxy type '{value}' for provider '{self.id}'")
        return spec

    def iter_calls(self) -> list[HttpCallSpec]:
        """Every declarative HTTP call in the descriptor (for host extraction)."""
        calls: list[HttpCallSpec] = []
        for flow in self.auth.values():
            calls.append(flow.call)
        for source in self.options.values():
            calls.append(source.call)
            calls.extend(e.call for e in source.enrich)
        if self.validation is not None:
            calls.append(self.validation.call)
        for ptype in self.proxy_types:
            if ptype.known_ips is not None:
                calls.append(ptype.known_ips.call)
            if ptype.source is not None:
                calls.append(ptype.source.call)
        return calls

    def egress_hosts(self) -> list[str]:
        """Hosts that will receive requests carrying credential material."""
        hosts = {c.host for c in self.iter_calls() if c.host}
        return sorted(hosts)

    def discovery_hosts(self) -> list[str]:
        """Hosts contacted *through* the proxies for IP discovery (no credentials sent)."""
        hosts: set[str] = set()
        for ptype in self.proxy_types:
            if ptype.discovery is not None:
                host = urlsplit(ptype.discovery.url).hostname
                if host:
                    hosts.add(host)
        return sorted(hosts)

    def gateway_hosts(self) -> list[str]:
        """Static gateway hosts proxies connect to (templates are reported verbatim)."""
        hosts: set[str] = set()
        for ptype in self.proxy_types:
            if isinstance(ptype.host, str):
                hosts.add(ptype.host)
            elif isinstance(ptype.host, TemplateSpec):
                hosts.add(ptype.host.separator.join(p.text for p in ptype.host.parts))
        return sorted(hosts)

    def is_session_only(self) -> bool:
        return all(t.mode == "session" for t in self.proxy_types)


DescriptorInput = Annotated[dict[str, Any], Field(description="Raw descriptor document")]


def parse_descriptor(data: dict[str, Any]) -> ProviderDescriptor:
    """Validate a raw document into a descriptor (raises ``pydantic.ValidationError``)."""
    return ProviderDescriptor.model_validate(data)
