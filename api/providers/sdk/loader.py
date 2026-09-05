# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Loading descriptors from YAML files, directories and Python entry points."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import structlog
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from api.providers.sdk.descriptor import ProviderDescriptor

logger = structlog.get_logger()

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "builtin"
ENTRY_POINT_GROUP = "octoprox.providers"


class DescriptorLoadError(ValueError):
    """A descriptor document could not be parsed or validated."""


def descriptor_from_yaml(text: str) -> ProviderDescriptor:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DescriptorLoadError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise DescriptorLoadError("descriptor document must be a mapping")
    return descriptor_from_dict(data)


def descriptor_from_dict(data: dict[str, Any]) -> ProviderDescriptor:
    try:
        return ProviderDescriptor.model_validate(data)
    except ValidationError as exc:
        raise DescriptorLoadError(format_validation_error(exc)) from exc


def descriptor_to_yaml(descriptor: ProviderDescriptor) -> str:
    data = descriptor.model_dump(mode="json", exclude_none=True, exclude_defaults=True, by_alias=True)
    # Keep the identity fields first so exports read naturally.
    ordered = {k: data.pop(k) for k in ("id", "name", "description", "version") if k in data}
    ordered.update(data)
    return str(yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=100))


def format_validation_error(exc: ValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = error.get("msg", "invalid")
        messages.append(f"{location}: {message}" if location else message)
    return "; ".join(messages)


def load_descriptor_file(path: Path) -> ProviderDescriptor:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DescriptorLoadError(f"cannot read {path}: {exc}") from exc
    return descriptor_from_yaml(text)


@dataclass(frozen=True)
class LoadedDescriptor:
    descriptor: ProviderDescriptor
    origin: str


def load_directory(directory: Path) -> list[LoadedDescriptor]:
    """Load every ``*.yaml``/``*.yml`` in ``directory``; bad files are logged and skipped."""
    loaded: list[LoadedDescriptor] = []
    if not directory.is_dir():
        return loaded
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in (".yaml", ".yml"):
            continue
        try:
            loaded.append(LoadedDescriptor(load_descriptor_file(path), str(path)))
        except DescriptorLoadError as exc:
            logger.error("Skipping invalid provider descriptor", path=str(path), error=str(exc))
    return loaded


def load_builtin_descriptors() -> list[LoadedDescriptor]:
    """Descriptors shipped with Octoprox; a broken built-in is a programming error."""
    loaded: list[LoadedDescriptor] = []
    for path in sorted(BUILTIN_DIR.glob("*.yaml")):
        loaded.append(LoadedDescriptor(load_descriptor_file(path), f"builtin:{path.name}"))
    return loaded


@dataclass(frozen=True)
class PluginProvider:
    """A provider contributed by a Python entry point.

    The entry point may resolve to a :class:`ProviderDescriptor`, a dict, a
    path to a YAML file, or a class implementing ``SyncableProvider`` with a
    ``descriptor`` class attribute (for vendors that genuinely need code).
    """

    descriptor: ProviderDescriptor
    provider_class: type[Any] | None
    origin: str


def load_entry_points() -> list[PluginProvider]:
    plugins: list[PluginProvider] = []
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        origin = f"plugin:{entry_point.name}"
        try:
            target = entry_point.load()
            plugins.append(_plugin_from_target(target, origin))
        except Exception as exc:  # a broken plugin must not take the server down
            logger.error("Failed to load provider plugin", entry_point=entry_point.name, error=str(exc))
    return plugins


def _plugin_from_target(target: Any, origin: str) -> PluginProvider:
    if isinstance(target, ProviderDescriptor):
        return PluginProvider(target, None, origin)
    if isinstance(target, dict):
        return PluginProvider(descriptor_from_dict(target), None, origin)
    if isinstance(target, str | Path):
        return PluginProvider(load_descriptor_file(Path(target)), None, origin)
    descriptor = getattr(target, "descriptor", None)
    if isinstance(descriptor, ProviderDescriptor) and isinstance(target, type):
        return PluginProvider(descriptor, target, origin)
    if isinstance(descriptor, dict) and isinstance(target, type):
        return PluginProvider(descriptor_from_dict(descriptor), target, origin)
    raise DescriptorLoadError(
        f"{origin}: entry point must resolve to a descriptor, dict, YAML path or provider class"
    )
