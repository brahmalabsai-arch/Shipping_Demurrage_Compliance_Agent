"""Structured logging setup shared by every long-running process (MCP servers, scheduler).

JSON lines to **stderr** — for stdio MCP servers stdout carries the protocol and must stay clean.
"""

import logging
import sys
from typing import TextIO, cast

import structlog

from config.settings import get_settings


class _CurrentStderr:
    """Writes to whatever ``sys.stderr`` is *now*, not the stream at configure time: the logger is
    cached on first use, and a stream that is later swapped and closed (a test harness capturing
    output does exactly that) would make every later log line raise ``ValueError``."""

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


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
        logger_factory=structlog.PrintLoggerFactory(file=cast(TextIO, _CurrentStderr())),
        cache_logger_on_first_use=True,
    )
