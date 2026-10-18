"""Structured logging setup for CLIs and workers: JSON lines on stderr (stdout = results)."""

from __future__ import annotations

import logging
import sys

import structlog

# HTTP client libraries log every request URL at INFO; FPL URLs carry entry (manager) and
# league ids, which must not reach logs (M1.8 review). Their warnings and errors still do.
QUIET_LOGGERS = ("httpx", "httpcore")


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    logging.basicConfig(stream=sys.stderr, level=level, format="%(message)s")
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(logging.WARNING, logging.getLevelName(level)))
    renderer = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
