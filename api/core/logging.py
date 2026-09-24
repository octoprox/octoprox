# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Logging configuration for Octoprox.

Both structlog loggers (application code) and plain stdlib loggers (uvicorn,
SQLAlchemy, third-party libraries) render through one structlog formatter,
so every line has the same shape and carries the same request context.

Two formats are supported:

- ``console``: human-readable, coloured only when stdout is a terminal so
  container logs and files stay free of escape codes. The default, and
  what development should use.
- ``json``: one JSON object per line with ISO-8601 timestamps, for log
  shippers and structured search in production.

Whatever the format, fields bound with ``structlog.contextvars`` (the
request ID from ``RequestContextMiddleware`` and the user from
``get_current_user``) appear on every line logged during a request, and
``instance`` names the process that wrote the line so replicas in a cluster
can be told apart once their output is collected in one place.
"""

import logging
import sys
from typing import Any

import structlog

LOG_FORMATS = ("console", "json")

# Loggers uvicorn configures itself when launched from its CLI.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _add_instance(instance_id: str) -> Any:
    """Build a processor that stamps ``instance`` on every event.

    A processor rather than a bound context variable because the request
    middleware clears context variables at each request boundary, and the
    instance should be on every line, inside a request or not.
    """

    def processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event_dict.setdefault("instance", instance_id)
        return event_dict

    return processor


def setup_logging(
    log_level: str = "INFO", log_format: str = "console", instance_id: str | None = None
) -> None:
    """Configure application logging.

    Args:
        log_level: The logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_format: ``console`` for human-readable output, ``json`` for one
            JSON object per line.
        instance_id: Identifier of this process, added to every line as
            ``instance``. Omitted when ``None``.
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
    if instance_id:
        shared_processors.append(_add_instance(instance_id))

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
