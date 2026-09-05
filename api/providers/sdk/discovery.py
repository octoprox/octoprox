# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Vendor-API discovery services: dynamic options, credential validation, test runs."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel, Field

from api.providers.sdk.descriptor import HttpCallSpec, OptionsSourceSpec, ProviderDescriptor
from api.providers.sdk.extract import ExtractionError, ValueExtractor
from api.providers.sdk.http import CallTrace, HttpCallError, HttpCallExecutor
from api.providers.sdk.provider import SdkRuntime
from api.providers.sdk.sources import ListSource
from api.providers.sdk.templating import RenderContext, TemplateRenderer

logger = structlog.get_logger()


class ResolvedOption(BaseModel):
    """A select option produced by an options source."""

    value: str
    label: str
    description: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


@dataclass
class DiscoveryOutcome:
    """Common result envelope carrying redacted traces for the admin test panel."""

    ok: bool
    message: str = ""
    result: Any = None
    traces: list[CallTrace] = field(default_factory=list)

    def trace_dicts(self) -> list[dict[str, Any]]:
        return [t.as_dict() for t in self.traces]


class OptionsCache:
    """TTL cache for resolved options, keyed by descriptor, source and rendered inputs."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[list[ResolvedOption], float]] = {}

    def get(self, key: str) -> list[ResolvedOption] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        options, expires_at = entry
        if time.monotonic() >= expires_at:
            self._entries.pop(key, None)
            return None
        return options

    def put(self, key: str, options: list[ResolvedOption], ttl: int) -> None:
        if ttl > 0:
            self._entries[key] = (options, time.monotonic() + ttl)

    def clear(self) -> None:
        self._entries.clear()


_options_cache = OptionsCache()


def missing_connector_values(
    descriptor: ProviderDescriptor, calls: list[HttpCallSpec], connector_config: dict[str, Any]
) -> list[str]:
    """Required connector keys a call references that are empty in ``connector_config``.

    Optional fields (e.g. filter parameters) are allowed to be empty because the
    executor drops empty params. Used by the tester to explain a would-be vendor
    error ("zone is required") before the request is made.
    """
    missing: list[str] = []
    for call in calls:
        texts = [call.url, *call.headers.values(), *call.params.values()]
        if call.body is not None:
            texts.append(json.dumps(call.body))
        for text in texts:
            for path in TemplateRenderer.referenced_paths(text):
                scope, _, key = path.partition(".")
                if scope != "connector" or not key or key in missing:
                    continue
                field = descriptor.find_field("connector", key)
                if (field is None or field.required) and connector_config.get(key) in (None, ""):
                    missing.append(key)
    return missing


def _option_document(option: ResolvedOption) -> dict[str, Any]:
    """Flat view of an option used by ``filter``/``when`` predicates and ``{item.*}`` templates."""
    return {"value": option.value, "label": option.label, "description": option.description, **option.extra}


def _context(descriptor: ProviderDescriptor, credential: dict[str, Any], connector: dict[str, Any]) -> RenderContext:
    return RenderContext(
        credential=dict(credential),
        connector=dict(connector),
        secret_keys=frozenset(descriptor.secret_keys()),
    )


class OptionsResolver:
    """Resolves a descriptor's named options source against a credential."""

    def __init__(
        self,
        descriptor: ProviderDescriptor,
        runtime: SdkRuntime,
        *,
        executor: HttpCallExecutor | None = None,
        cache: OptionsCache | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._executor = executor or runtime.executor(descriptor)
        self._extractor = ValueExtractor()
        self._renderer = TemplateRenderer()
        self._cache = cache or _options_cache

    async def resolve(
        self,
        name: str,
        credential_config: dict[str, Any],
        connector_config: dict[str, Any] | None = None,
        *,
        use_cache: bool = True,
    ) -> DiscoveryOutcome:
        spec = self._descriptor.options.get(name)
        if spec is None:
            return DiscoveryOutcome(ok=False, message=f"unknown options source '{name}'")
        ctx = _context(self._descriptor, credential_config, connector_config or {})
        cache_key = self._cache_key(name, ctx)
        if use_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return DiscoveryOutcome(ok=True, result=cached)
        traces: list[CallTrace] = []
        try:
            result = await self._executor.execute(spec.call, ctx)
            traces.extend(result.traces)
            options = self._collect(spec, result.items(self._extractor, spec.items))
            finished: list[ResolvedOption] = []
            for option, item in options:
                await self._enrich(spec, option, ctx, traces)
                self._finalize(spec, option, item)
                if spec.filter is not None and not self._extractor.truthy(spec.filter, _option_document(option)):
                    continue
                finished.append(option)
            options_out = finished
        except HttpCallError as exc:
            if exc.trace is not None:
                traces.append(exc.trace)
            return DiscoveryOutcome(ok=False, message=str(exc), traces=traces)
        except ExtractionError as exc:
            return DiscoveryOutcome(ok=False, message=str(exc), traces=traces)
        self._cache.put(cache_key, options_out, spec.cache_seconds)
        return DiscoveryOutcome(ok=True, result=options_out, traces=traces)

    def _collect(self, spec: OptionsSourceSpec, items: list[Any]) -> list[tuple[ResolvedOption, Any]]:
        """Build one option per item, or per distinct value when grouping."""
        options: list[tuple[ResolvedOption, Any]] = []
        by_value: dict[str, ResolvedOption] = {}
        for item in items:
            value = self._extractor.extract_str(spec.value, item)
            if value is None or value == "":
                continue
            if spec.group_by_value and value in by_value:
                by_value[value].extra[spec.count_key] = int(by_value[value].extra[spec.count_key]) + 1
                continue
            extra = {key: self._extractor.extract(source, item) for key, source in spec.extra.items()}
            if spec.group_by_value:
                extra[spec.count_key] = 1
            option = ResolvedOption(value=value, label=value, extra=extra)
            by_value[value] = option
            options.append((option, item))
        return options

    def _finalize(self, spec: OptionsSourceSpec, option: ResolvedOption, item: Any) -> None:
        """Evaluate label/description over the enriched option document."""
        document: dict[str, Any] = {**(item if isinstance(item, dict) else {}), **_option_document(option)}
        if spec.label is not None:
            option.label = self._extractor.extract_str(spec.label, document) or option.value
        if spec.description is not None:
            option.description = self._extractor.extract_str(spec.description, document)

    async def _enrich(
        self, spec: OptionsSourceSpec, option: ResolvedOption, ctx: RenderContext, traces: list[CallTrace]
    ) -> None:
        for enrich in spec.enrich:
            document = _option_document(option)
            if enrich.when is not None and not self._extractor.truthy(enrich.when, document):
                continue
            item_ctx = ctx.with_item(document)
            try:
                result = await self._executor.execute(enrich.call, item_ctx)
            except HttpCallError as exc:
                if exc.trace is not None:
                    traces.append(exc.trace)
                logger.warning("Option enrichment failed", option=option.value, error=str(exc))
                continue
            traces.extend(result.traces)
            for key, source in enrich.merge.items():
                option.extra[key] = self._extractor.extract(source, result.data)

    def _cache_key(self, name: str, ctx: RenderContext) -> str:
        payload = json.dumps({"c": ctx.credential, "k": ctx.connector}, sort_keys=True, default=str)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        return f"{self._descriptor.id}:{self._descriptor.version}:{name}:{digest}"


class CredentialValidator:
    """Runs a descriptor's ``validation`` call and captures values into the config."""

    def __init__(
        self,
        descriptor: ProviderDescriptor,
        runtime: SdkRuntime,
        *,
        executor: HttpCallExecutor | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._executor = executor or runtime.executor(descriptor)
        self._extractor = ValueExtractor()
        self._renderer = TemplateRenderer()

    @property
    def enabled(self) -> bool:
        return self._descriptor.validation is not None

    def applies(self, credential_config: dict[str, Any]) -> bool:
        spec = self._descriptor.validation
        if spec is None:
            return False
        ctx = _context(self._descriptor, credential_config, {})
        return self._renderer.evaluate(spec.when, ctx)

    async def validate(self, credential_config: dict[str, Any]) -> DiscoveryOutcome:
        """Validate; on success ``result`` holds the config with captured values merged."""
        spec = self._descriptor.validation
        if spec is None or not self.applies(credential_config):
            return DiscoveryOutcome(ok=True, result=dict(credential_config))
        ctx = _context(self._descriptor, credential_config, {})
        try:
            result = await self._executor.execute(spec.call, ctx, raise_for_status=False)
        except HttpCallError as exc:
            traces = [exc.trace] if exc.trace is not None else []
            return DiscoveryOutcome(ok=False, message=f"{spec.error_message} ({exc})", traces=traces)
        if not result.ok:
            return DiscoveryOutcome(
                ok=False, message=f"{spec.error_message} (HTTP {result.status})", traces=result.traces
            )
        try:
            if spec.success is not None and not self._extractor.truthy(spec.success, result.data):
                return DiscoveryOutcome(ok=False, message=spec.error_message, traces=result.traces)
            merged = dict(credential_config)
            for key, source in spec.capture.items():
                captured = self._extractor.extract_str(source, result.data)
                if captured is not None:
                    merged[key] = captured
        except ExtractionError as exc:
            return DiscoveryOutcome(ok=False, message=str(exc), traces=result.traces)
        return DiscoveryOutcome(ok=True, message="Credential validated", result=merged, traces=result.traces)


class DescriptorTester:
    """Admin "test" panel backend: exercise a descriptor with a throwaway credential."""

    def __init__(self, descriptor: ProviderDescriptor, runtime: SdkRuntime) -> None:
        self._descriptor = descriptor
        self._runtime = runtime
        self._executor = runtime.executor(descriptor)

    async def run(
        self,
        action: str,
        credential_config: dict[str, Any],
        connector_config: dict[str, Any],
        option_name: str | None = None,
    ) -> DiscoveryOutcome:
        if action == "validate":
            validator = CredentialValidator(self._descriptor, self._runtime, executor=self._executor)
            if not validator.enabled:
                return DiscoveryOutcome(ok=False, message="This provider has no credential validation call")
            outcome = await validator.validate(credential_config)
            if outcome.ok:
                captured = {
                    k: v for k, v in (outcome.result or {}).items() if k not in credential_config
                }
                outcome.result = {"captured": captured}
            return outcome
        if action == "options":
            if not option_name:
                return DiscoveryOutcome(ok=False, message="option_name is required")
            source = self._descriptor.options.get(option_name)
            if source is None:
                return DiscoveryOutcome(ok=False, message=f"unknown options source '{option_name}'")
            missing = missing_connector_values(
                self._descriptor, [source.call, *(e.call for e in source.enrich)], connector_config
            )
            if missing:
                return DiscoveryOutcome(ok=False, message=f"Missing connector values: {', '.join(missing)}")
            resolver = OptionsResolver(self._descriptor, self._runtime, executor=self._executor)
            outcome = await resolver.resolve(option_name, credential_config, connector_config, use_cache=False)
            if outcome.ok:
                options: list[ResolvedOption] = outcome.result
                outcome.message = f"{len(options)} option(s)"
                outcome.result = [o.model_dump() for o in options]
            return outcome
        if action == "list_proxies":
            return await self._list_proxies(credential_config, connector_config)
        return DiscoveryOutcome(ok=False, message=f"unknown test action '{action}'")

    async def _list_proxies(
        self, credential_config: dict[str, Any], connector_config: dict[str, Any]
    ) -> DiscoveryOutcome:
        try:
            ptype = self._descriptor.resolve_proxy_type(credential_config, connector_config)
        except ValueError as exc:
            return DiscoveryOutcome(ok=False, message=str(exc))
        if ptype.mode != "list" or ptype.source is None:
            return DiscoveryOutcome(ok=False, message=f"proxy type '{ptype.key}' is not list mode")
        missing = missing_connector_values(self._descriptor, [ptype.source.call], connector_config)
        if missing:
            return DiscoveryOutcome(ok=False, message=f"Missing connector values: {', '.join(missing)}")
        ctx = _context(self._descriptor, credential_config, connector_config)
        try:
            listed = await ListSource(ptype.source, self._executor, ValueExtractor()).fetch(ctx)
        except HttpCallError as exc:
            return DiscoveryOutcome(ok=False, message=str(exc), traces=[exc.trace] if exc.trace else [])
        preview = [
            {"host": p.host, "port": p.port, "username": p.username, "country": p.country, "identity": p.identity}
            for p in listed[:50]
        ]
        return DiscoveryOutcome(ok=True, message=f"{len(listed)} proxies", result=preview)
