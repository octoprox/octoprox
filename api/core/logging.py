# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Logging for Octoprox: one formatter for the lines of structlog and of the standard library, as console text or JSON.

Every line carries the request context bound with ``structlog.contextvars`` and the ``instance`` and ``role`` of the
process that wrote it; ``error_text`` is how a line names an error.
"""

import logging
import sys
from typing import Any

import structlog

LOG_FORMATS = ("console", "json")

# Loggers uvicorn configures itself when launched from its CLI.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Paths the kubelet, the load balancers and Prometheus poll every few seconds.
PROBE_PATHS = frozenset({"/health", "/live", "/ready", "/metrics"})


class _DropProbeAccess(logging.Filter):
    """Drop uvicorn's access lines for the probe paths."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access logs (client, method, path, http version, status).
        args = record.args
        return not (isinstance(args, tuple) and len(args) >= 3 and args[2] in PROBE_PATHS)


_PROBE_FILTER = _DropProbeAccess()


def _add_identity(identity: dict[str, str]) -> Any:
    """Build a processor that stamps the process's identity (``instance``, ``role``) on every event.

    A processor rather than bound context variables because the request
    middleware clears context variables at each request boundary, and the
    identity should be on every line, inside a request or not.
    """

    def processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        for key, value in identity.items():
            event_dict.setdefault(key, value)
        return event_dict

    return processor


def setup_logging(
    log_level: str = "INFO",
    log_format: str = "console",
    instance_id: str | None = None,
    role: str | None = None,
) -> None:
    """Configure application logging.

    Args:
        log_level: The logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_format: ``console`` for human-readable lines, ``json`` for one JSON object per line.
        instance_id: This process's identifier, on every line as ``instance``; None leaves it out.
        role: What this process runs (``Settings.role``), on every line as ``role``; None leaves it out.
    """
    level = log_level.upper()
    fmt = log_format.lower()
    if fmt not in LOG_FORMATS:
        raise ValueError(f"Unknown log format {log_format!r}; expected one of {', '.join(LOG_FORMATS)}")
    as_json = fmt == "json"

    # Processors applied to every event, whether it came from structlog or
    # from a stdlib logger (via ProcessorFormatter's foreign_pre_chain).
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso" if as_json else "%Y-%m-%d %H:%M:%S"),
        structlog.processors.StackInfoRenderer(),
    ]
    identity: dict[str, str] = {}
    if instance_id:
        identity["instance"] = instance_id
    if role:
        identity["role"] = role
    if identity:
        shared_processors.append(_add_identity(identity))

    renderers: list[Any]
    if as_json:
        renderers = [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # ConsoleRenderer pretty-prints exc_info itself. Its colour default
        # is "always" on non-Windows, so gate it on a real terminal.
        renderers = [structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())]

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *renderers],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # When uvicorn is started from its CLI (as the Docker image does) it
    # installs its own handlers on these loggers, with propagation off,
    # before importing the app. Left alone, its access and error lines would
    # bypass the formatter above and come out in uvicorn's plain format.
    # Strip those handlers and let the records reach the root handler.
    for name in UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        for existing in list(uvicorn_logger.handlers):
            uvicorn_logger.removeHandler(existing)
        uvicorn_logger.propagate = True

    # Below DEBUG, quiet the request lines nobody reads: httpx logs every
    # outbound request at INFO (each preflight echo is one), and probes hit
    # the health paths every few seconds. At a busy proxy's rate they made up
    # half of all output and pushed everything else out of the container log
    # within minutes.
    quiet = level != "DEBUG"
    logging.getLogger("httpx").setLevel(logging.WARNING if quiet else logging.NOTSET)
    access_logger = logging.getLogger("uvicorn.access")
    if quiet:
        access_logger.addFilter(_PROBE_FILTER)
    else:
        access_logger.removeFilter(_PROBE_FILTER)

    # Prevent SQLAlchemy from propagating to root logger to avoid duplicate logs
    # SQLAlchemy's echo=True adds its own handler, so we don't want it to also
    # propagate to our root logger
    logging.getLogger('sqlalchemy.engine').propagate = False

    structlog.configure(
        processors=[
            # First, so events below the threshold are dropped before any
            # timestamping or context merging is spent on them. It is not in
            # shared_processors because stdlib records reaching the formatter
            # have already been level-filtered by logging itself.
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def error_text(error: BaseException) -> str:
    """How a log line names ``error``: its text, or its type's name when it has none (a deadline's TimeoutError)."""
    return str(error) or type(error).__name__


def error_label(error: BaseException) -> str:
    """How the job stats and a failed task's state name ``error``: its type's name, then its text."""
    return f"{type(error).__name__}: {error}"
