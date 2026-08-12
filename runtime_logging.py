"""Shared, concise logging for command-line refresh and maintenance jobs."""

from __future__ import annotations

import logging
import os
import sys

LOGGER_NAME = "wtarg"
LOG_LEVEL_ENV = "WTARG_LOG_LEVEL"
VERBOSE_ENV = "WTARG_VERBOSE"
_TRUTHY = {"1", "true", "yes", "on"}


def _verbose_from_environment() -> bool:
    return os.environ.get(VERBOSE_ENV, "").strip().lower() in _TRUTHY


def configure_logging(*, verbose: bool | None = None) -> logging.Logger:
    """Configure the project logger and return it.

    Normal runs emit concise INFO-or-higher status messages. ``--verbose`` or
    ``WTARG_VERBOSE=1`` enables per-item DEBUG details. All log records go to
    stderr so stdout remains available for machine-readable CLI results.
    """

    if verbose is None:
        configured_level = os.environ.get(LOG_LEVEL_ENV, "").strip().upper()
        if configured_level:
            level = getattr(logging, configured_level, logging.INFO)
        else:
            level = logging.DEBUG if _verbose_from_environment() else logging.INFO
    else:
        level = logging.DEBUG if verbose else logging.INFO

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(stream_handler)
    for configured_handler in logger.handlers:
        configured_handler.setLevel(level)
    return logger


def get_logger(component: str) -> logging.Logger:
    """Return a configured child logger with a short component name."""

    root = logging.getLogger(LOGGER_NAME)
    if not root.handlers:
        root = configure_logging()
    short_name = str(component or "runtime").rsplit(".", 1)[-1]
    return root.getChild(short_name)
