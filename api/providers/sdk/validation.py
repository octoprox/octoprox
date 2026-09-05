# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Config validation driven by :class:`FieldSpec` lists.

Replaces the per-provider Pydantic config models: a descriptor's field list
is enough to coerce, default, normalise and check a credential or connector
config the same way for every provider.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from api.providers.sdk.descriptor import FieldScope, FieldSpec, OptionSpec
from api.providers.sdk.templating import RenderContext, TemplateRenderer

COUNTRY_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")


class ConfigValidationError(ValueError):
    """One or more fields failed validation."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class FieldSetValidator:
    """Validates a config dict against a list of fields."""

    def __init__(
        self,
        fields: list[FieldSpec],
        scope: FieldScope,
        *,
        presets: Mapping[str, list[OptionSpec]] | None = None,
        extra_allowed: Iterable[str] = (),
        renderer: TemplateRenderer | None = None,
    ) -> None:
        self._fields = fields
        self._scope = scope
        self._presets = presets or {}
        self._extra_allowed = set(extra_allowed)
        self._renderer = renderer or TemplateRenderer()

    def validate(self, config: Mapping[str, Any], other: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return a normalised config or raise :class:`ConfigValidationError`.

        ``other`` is the config of the opposite scope (credential when validating
        a connector and vice versa) so ``show_when`` conditions can be evaluated.
        """
        source = dict(config)
        credential = source if self._scope == "credential" else dict(other or {})
        connector = source if self._scope == "connector" else dict(other or {})
        ctx = RenderContext(credential=credential, connector=connector)

        errors: list[str] = []
        result: dict[str, Any] = {}
        for spec in self._fields:
            visible = self._renderer.evaluate(spec.show_when, ctx)
            raw = source.get(spec.key)
            if raw is None or (isinstance(raw, str) and raw.strip() == ""):
                if spec.default is not None and visible:
                    raw = spec.default
                else:
                    if spec.required and visible:
                        errors.append(f"{spec.label} is required")
                    continue
            if not visible:
                # Hidden fields are dropped so stale values never leak into templates.
                continue
            try:
                result[spec.key] = self._coerce(spec, raw, remote=self._uses_remote_options(spec, ctx))
            except ValueError as exc:
                errors.append(str(exc))
        for key in self._extra_allowed:
            if key in source and source[key] is not None:
                result[key] = source[key]
        if errors:
            raise ConfigValidationError(errors)
        return result

    def _uses_remote_options(self, spec: FieldSpec, ctx: RenderContext) -> bool:
        """Remote options cannot be checked offline; static fallbacks can."""
        if spec.options_from is None:
            return False
        return self._renderer.evaluate(spec.options_from_when, ctx)

    def _coerce(self, spec: FieldSpec, raw: Any, *, remote: bool = False) -> Any:
        if spec.type == "number":
            return self._coerce_number(spec, raw)
        if spec.type == "boolean":
            return self._coerce_bool(spec, raw)
        text = str(raw)
        if spec.transform == "strip" or spec.type != "textarea":
            text = text.strip()
        if spec.transform == "upper":
            text = text.upper()
        elif spec.transform == "lower":
            text = text.lower()
        if spec.type == "country":
            text = text.upper()
            if not COUNTRY_CODE_PATTERN.match(text):
                raise ValueError(f"{spec.label} must be a 2-letter country code (e.g. US, GB)")
        if spec.type == "url" and not re.match(r"^https?://\S+$", text):
            raise ValueError(f"{spec.label} must be an http(s) URL")
        if spec.pattern is not None and not re.search(spec.pattern, text):
            raise ValueError(f"{spec.label} has an invalid format")
        allowed = None if remote else self._static_option_values(spec)
        if allowed is not None and text not in allowed:
            canonical = next((v for v in allowed if v.lower() == text.lower()), None)
            if canonical is None:
                choices = ", ".join(sorted(v for v in allowed if v))
                raise ValueError(f"{spec.label} must be one of: {choices}")
            text = canonical
        if spec.min is not None and len(text) < spec.min:
            raise ValueError(f"{spec.label} must be at least {int(spec.min)} characters")
        if spec.max is not None and len(text) > spec.max:
            raise ValueError(f"{spec.label} must be at most {int(spec.max)} characters")
        return text

    @staticmethod
    def _coerce_number(spec: FieldSpec, raw: Any) -> int | float:
        if isinstance(raw, bool):
            raise ValueError(f"{spec.label} must be a number")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{spec.label} must be a number") from exc
        if spec.min is not None and number < spec.min:
            raise ValueError(f"{spec.label} must be at least {_fmt(spec.min)}")
        if spec.max is not None and number > spec.max:
            raise ValueError(f"{spec.label} must be at most {_fmt(spec.max)}")
        return int(number) if number.is_integer() else number

    @staticmethod
    def _coerce_bool(spec: FieldSpec, raw: Any) -> bool:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off", ""):
            return False
        raise ValueError(f"{spec.label} must be true or false")

    def _static_option_values(self, spec: FieldSpec) -> set[str] | None:
        if spec.options:
            return {o.value for o in spec.options}
        if spec.options_preset is not None:
            preset = self._presets.get(spec.options_preset)
            if preset:
                return {o.value for o in preset}
        return None


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)
