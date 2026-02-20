"""Logging configuration for Octoprox."""

import logging
import sys

import structlog


def setup_logging(log_level: str = "INFO") -> None:
    """Configure application logging.

    Args:
        log_level: The logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    level = log_level.upper()

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

    # Prevent SQLAlchemy from propagating to root logger to avoid duplicate logs
    # SQLAlchemy's echo=True adds its own handler, so we don't want it to also
    # propagate to our root logger
    logging.getLogger('sqlalchemy.engine').propagate = False

    # Configure structlog to respect the log level
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.filter_by_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Set the minimum log level for structlog's stdlib integration
    logging.getLogger().setLevel(level)

