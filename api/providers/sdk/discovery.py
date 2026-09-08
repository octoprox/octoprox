# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Vendor-API discovery services: dynamic options, credential validation, test runs."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field

from api.models.connector import Connector
from api.models.credential import Credential
from api.models.proxy import Proxy
from api.providers.sdk.descriptor import (
    HttpCallSpec,
    OptionsSourceSpec,
    ProviderDescriptor,
    ProxyTypeSpec,
    Template,
    TemplateSpec,
    split_scoped_key,
)
from api.providers.sdk.egress import EgressDeniedError, EgressGuard
from api.providers.sdk.extract import ExtractionError, ValueExtractor
from api.providers.sdk.http import CallTrace, HttpCallError, HttpCallExecutor, _redact_text
from api.providers.sdk.provider import DescriptorProvider, SdkRuntime
from api.providers.sdk.sources import ListSource, default_proxied_client_factory
from api.providers.sdk.strategies import ProxyBuilder
from api.providers.sdk.templating import RenderContext, TemplateRenderer

logger = structlog.get_logger()

DEFAULT_PROXY_TEST_URL = "https://httpbin.org/ip"
"""Fetched through the proxy when neither the request nor the descriptor names a URL.

Same default as the health checker, so a passing test predicts a healthy proxy.
"""

PROXY_TEST_BODY_PREVIEW_BYTES = 2048


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


def missing_template_values(
    descriptor: ProviderDescriptor, templates: list[Template | None], connector_config: dict[str, Any]
) -> list[str]:
    """Required connector keys a proxy-endpoint template references that are empty."""
    texts: list[str] = []
    for template in templates:
        if template is None:
            continue
        if isinstance(template, TemplateSpec):
            texts.extend(part.text for part in template.parts)
        else:
            texts.append(template)
    missing: list[str] = []
    for text in texts:
        for path in TemplateRenderer.referenced_paths(text):
            scope, _, key = path.partition(".")
            if scope != "connector" or not key or key in missing:
                continue
            field = descriptor.find_field("connector", key)
            if field is not None and field.required and connector_config.get(key) in (None, ""):
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
        *,
        target_url: str | None = None,
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
        if action == "proxy_request":
            return await self._proxy_request(credential_config, connector_config, target_url)
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

    # --- proxy_request ------------------------------------------------------------

    async def _proxy_request(
        self, credential_config: dict[str, Any], connector_config: dict[str, Any], target_url: str | None
    ) -> DiscoveryOutcome:
        """Provision one proxy in memory exactly as a connector would, then fetch a URL through it.

        This is the end-to-end check: gateway host, username template, secret
        placeholders and (for port/list modes) the vendor calls all have to be
        right for the response to come back. Nothing is persisted.
        """
        try:
            ptype = self._descriptor.resolve_proxy_type(credential_config, connector_config)
        except ValueError as exc:
            return DiscoveryOutcome(ok=False, message=str(exc))
        url = target_url or ptype.healthcheck_url or connector_config.get("healthcheck_url") or DEFAULT_PROXY_TEST_URL
        guard = EgressGuard(self._runtime.egress_policy)
        try:
            guard.check_static(url)
        except EgressDeniedError as exc:
            return DiscoveryOutcome(ok=False, message=f"target URL rejected: {exc}")
        if ptype.mode == "list":
            assert ptype.source is not None
            missing = missing_connector_values(self._descriptor, [ptype.source.call], connector_config)
        else:
            missing = missing_template_values(
                self._descriptor, [ptype.host, ptype.username, ptype.password], connector_config
            )
        if missing:
            return DiscoveryOutcome(ok=False, message=f"Missing connector values: {', '.join(missing)}")

        # Creating a credential runs the validation call and stores what it captures
        # (e.g. Bright Data's customer id, which the username template needs), so do
        # the same here or the endpoint would be built from an incomplete credential.
        traces: list[CallTrace] = []
        validator = CredentialValidator(self._descriptor, self._runtime, executor=self._executor)
        if validator.enabled:
            validated = await validator.validate(credential_config)
            traces.extend(validated.traces)
            if not validated.ok:
                return DiscoveryOutcome(ok=False, message=validated.message, traces=traces)
            credential_config = validated.result or credential_config

        credential_config, connector_config = self._single_slot(ptype, credential_config, connector_config)
        ctx = _context(self._descriptor, credential_config, connector_config)
        credential = Credential(id="test", name="test", type=self._descriptor.id, project_id="test", config=credential_config)
        connector = Connector(
            id="test",
            name="test",
            credential_id=credential.id,
            credential_type=self._descriptor.id,
            project_id="test",
            config=connector_config,
        )
        try:
            provider = DescriptorProvider(self._descriptor, connector, credential, self._runtime)
            proxies, _removed = await provider.sync_proxies([])
        except HttpCallError as exc:
            return DiscoveryOutcome(ok=False, message=str(exc), traces=[*traces, exc.trace] if exc.trace else traces)
        except ValueError as exc:
            return DiscoveryOutcome(ok=False, message=self._redact(str(exc), ctx), traces=traces)
        if not proxies:
            return DiscoveryOutcome(ok=False, message=self._no_proxy_message(ptype), traces=traces)
        proxy = proxies[0]

        # The gateway is admin-authored data too: vet it like a vendor API host.
        try:
            await guard.resolve(f"https://{proxy.host}:{proxy.port}/")
        except EgressDeniedError as exc:
            return DiscoveryOutcome(ok=False, message=f"proxy host rejected: {exc}", traces=traces)

        proxy_url = ProxyBuilder(self._descriptor, ptype, connector.id).resolved_url(proxy, ctx)
        factory = self._runtime.proxied_client_factory or default_proxied_client_factory
        trace = CallTrace(method="GET", url=url)
        started = time.monotonic()
        try:
            async with factory(proxy_url, self._runtime.http_timeout_seconds) as client:
                response = await client.get(url)
        except httpx.TimeoutException:
            trace.elapsed_ms = (time.monotonic() - started) * 1000
            trace.error = "timed out"
            return DiscoveryOutcome(
                ok=False, message=f"Request through {proxy.host}:{proxy.port} timed out", traces=[*traces, trace]
            )
        except httpx.HTTPError as exc:
            trace.elapsed_ms = (time.monotonic() - started) * 1000
            trace.error = self._redact(str(exc) or exc.__class__.__name__, ctx)
            kind = "Proxy error" if isinstance(exc, httpx.ProxyError) else "Request failed"
            return DiscoveryOutcome(
                ok=False,
                message=f"{kind} through {proxy.host}:{proxy.port}: {trace.error}",
                traces=[*traces, trace],
                result=self._describe_proxy(proxy, url),
            )
        trace.elapsed_ms = (time.monotonic() - started) * 1000
        trace.status = response.status_code
        body = response.text[:PROXY_TEST_BODY_PREVIEW_BYTES]
        result = self._describe_proxy(proxy, url)
        result.update(
            {
                "status": response.status_code,
                "elapsed_ms": round(trace.elapsed_ms, 1),
                "exit_ip": _exit_ip_from(response),
                "body": self._redact(body, ctx),
            }
        )
        via = f"{proxy.host}:{proxy.port}"
        if response.is_success:
            exit_ip = f", exit IP {result['exit_ip']}" if result["exit_ip"] else ""
            message = f"HTTP {response.status_code} through {via} in {round(trace.elapsed_ms)} ms{exit_ip}"
            return DiscoveryOutcome(ok=True, message=message, result=result, traces=[*traces, trace])
        return DiscoveryOutcome(
            ok=False,
            message=f"HTTP {response.status_code} from {url} through {via}",
            result=result,
            traces=[*traces, trace],
        )

    @staticmethod
    def _single_slot(
        ptype: ProxyTypeSpec, credential_config: dict[str, Any], connector_config: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Copies of the configs asking for exactly one slot (one discovery, one list entry)."""
        credential_config = dict(credential_config)
        connector_config = dict(connector_config)
        try:
            scope, key = split_scoped_key(ptype.count_field)
        except ValueError:
            return credential_config, connector_config
        (credential_config if scope == "credential" else connector_config)[key] = 1
        return credential_config, connector_config

    @staticmethod
    def _no_proxy_message(ptype: ProxyTypeSpec) -> str:
        if ptype.mode == "port":
            return (
                f"No proxy could be built for '{ptype.key}': IP discovery through {ptype.host}:{ptype.port} "
                "failed. Check the credentials and the discovery URL."
            )
        if ptype.mode == "list":
            return f"The vendor returned no proxies for '{ptype.key}'"
        return f"No proxy could be built for '{ptype.key}'"

    @staticmethod
    def _describe_proxy(proxy: Proxy, url: str) -> dict[str, Any]:
        """What the proxy row would look like; secrets stay ``{placeholders}``, the password is omitted."""
        return {
            "proxy": {
                "host": proxy.host,
                "port": proxy.port,
                "protocol": proxy.protocol.value,
                "username": proxy.username,
                "metadata": dict(proxy.metadata),
            },
            "target_url": url,
        }

    @staticmethod
    def _redact(text: str, ctx: RenderContext) -> str:
        return _redact_text(text, ctx.secret_values())


def _exit_ip_from(response: httpx.Response) -> str | None:
    """Best-effort exit IP from common "what is my IP" responses."""
    text = response.text.strip()
    try:
        document = response.json()
    except ValueError:
        return text if text and len(text) <= 45 and " " not in text else None
    if isinstance(document, dict):
        for key in ("origin", "ip", "query", "address"):
            value = document.get(key)
            if isinstance(value, str) and value:
                return value
    return None
