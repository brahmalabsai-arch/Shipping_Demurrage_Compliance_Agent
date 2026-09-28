"""Structured logging setup shared by every long-running process (MCP servers, scheduler).

JSON lines to **stderr** — for stdio MCP servers stdout carries the protocol and must stay clean.
"""

import logging
import sys

import structlog

from config.settings import get_settings


def configure_logging() -> None:
    """JSON logs to **stderr** — stdout carries the MCP stdio protocol and must stay clean."""
    level = logging.getLevelNamesMapping().get(get_settings().log_level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
