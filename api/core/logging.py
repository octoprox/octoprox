"""Logging configuration for Octoprox."""

import logging
import sys


def setup_logging(log_level: str = "INFO") -> None:
    """Configure application logging.

    Args:
        log_level: The logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

    # Prevent SQLAlchemy from propagating to root logger to avoid duplicate logs
    # SQLAlchemy's echo=True adds its own handler, so we don't want it to also
    # propagate to our root logger
    logging.getLogger('sqlalchemy.engine').propagate = False

