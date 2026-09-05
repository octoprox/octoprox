# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""JMESPath-based value extraction with optional string mapping.

JMESPath is deliberately the only query language exposed to descriptors: it
has no I/O, no attribute access and no side effects, so a descriptor authored
in the admin UI cannot escape the JSON document it is pointed at.
"""

from __future__ import annotations

from typing import Any

import jmespath
from jmespath.exceptions import JMESPathError

from api.providers.sdk.descriptor import ValueExpr, ValueSource


class ExtractionError(ValueError):
    """Raised when a JMESPath expression is invalid."""


class ValueExtractor:
    """Evaluates :data:`ValueSource` expressions against JSON documents."""

    def __init__(self) -> None:
        self._compiled: dict[str, Any] = {}

    def _expression(self, path: str) -> Any:
        compiled = self._compiled.get(path)
        if compiled is None:
            try:
                compiled = jmespath.compile(path)
            except JMESPathError as exc:
                raise ExtractionError(f"invalid JMESPath '{path}': {exc}") from exc
            self._compiled[path] = compiled
        return compiled

    def search(self, path: str, document: Any) -> Any:
        """Raw JMESPath search (``'@'`` returns the document itself)."""
        if path == "@":
            return document
        try:
            return self._expression(path).search(document)
        except JMESPathError as exc:
            raise ExtractionError(f"JMESPath '{path}' failed: {exc}") from exc

    def extract(self, source: ValueSource | None, document: Any) -> Any:
        """Evaluate a value source; returns ``None`` when nothing matches."""
        if source is None:
            return None
        if isinstance(source, str):
            return self.search(source, document)
        value = self.search(source.path, document)
        return self._apply_mapping(source, value)

    def extract_str(self, source: ValueSource | None, document: Any) -> str | None:
        value = self.extract(source, document)
        if value is None:
            return None
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, list | dict):
            return None
        return str(value)

    def extract_list(self, path: str, document: Any) -> list[Any]:
        value = self.search(path, document)
        if value is None:
            return []
        if isinstance(value, list):
            return value
        raise ExtractionError(f"JMESPath '{path}' did not select a list")

    def truthy(self, path: str | None, document: Any) -> bool:
        """Evaluate a predicate; ``None`` path means always true."""
        if path is None:
            return True
        value = self.search(path, document)
        if isinstance(value, list | dict | str):
            return len(value) > 0
        return bool(value)

    @staticmethod
    def _apply_mapping(source: ValueExpr, value: Any) -> Any:
        if not source.map:
            return value if value is not None else source.default
        text = "" if value is None else str(value)
        for rule in source.map:
            if rule.matches(text):
                return rule.to
        return source.default

    def validate_expression(self, path: str) -> None:
        """Raise :class:`ExtractionError` if ``path`` does not compile."""
        if path != "@":
            self._expression(path)
