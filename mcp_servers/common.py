"""Plumbing shared by the six MCP servers — each server stays a thin adapter over its service.

Every tool body is ``await run_tool(ctx, "<tool>", arguments, work)`` where ``work`` is a call
into the service layer. ``run_tool``:

* identifies the caller from the request's ``_meta`` — ``actor`` (recorded in the audit trail
  for writes; defaults to ``mcp:<server>``) and ``run_id`` (correlates an agent run's calls);
* runs ``work`` in its own ``session_scope()`` transaction on a worker thread (the services
  and psycopg are synchronous; the MCP event loop must not block);
* logs one structured ``mcp.tool_call`` event per call — tool, arguments, result or error,
  duration — via structlog (conventions §4);
* turns failures into typed tool errors: a domain error as ``"<ErrorType>: message"``, a lost
  or unreachable database as ``DatabaseUnavailableError`` (the server keeps serving), anything
  else as ``UnexpectedError`` without leaking internals.

Argument validation happens before ``work`` runs: FastMCP validates the call against the
tool's typed signature, so malformed input never reaches a service.
"""

import logging
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

import anyio.to_thread
import structlog
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic_core import to_jsonable_python
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.orm import Session

from config.settings import get_settings
from services.db import session_scope
from services.errors import CustomsCopilotError, DatabaseUnavailableError

T = TypeVar("T")
ToolContext = Context[Any, Any, Any]

logger = structlog.get_logger("mcp")


def create_server(name: str, instructions: str) -> FastMCP:
    """A FastMCP server with the SDK's per-request INFO chatter silenced — ``run_tool`` already
    logs every call as one structured event."""
    return FastMCP(name, instructions=instructions, log_level="WARNING")


@dataclass(frozen=True)
class Caller:
    """Who is calling a tool, taken from the request's ``_meta`` (both optional)."""

    actor: str
    run_id: str | None


def caller_of(ctx: ToolContext) -> Caller:
    """Caller identity for this request; the server name stands in for an anonymous actor."""
    meta = ctx.request_context.meta
    extra: Mapping[str, Any] = (meta.model_extra or {}) if meta is not None else {}
    actor = str(extra.get("actor") or "").strip() or f"mcp:{ctx.fastmcp.name}"
    run_id = extra.get("run_id")
    return Caller(actor=actor, run_id=None if run_id is None else str(run_id))


def _in_transaction(work: Callable[[Session, Caller], T], caller: Caller) -> T:
    with session_scope() as session:
        return work(session, caller)


def _is_connection_failure(exc: Exception) -> bool:
    return isinstance(exc, OperationalError | InterfaceError) or (
        isinstance(exc, DBAPIError) and exc.connection_invalidated
    )


async def run_tool(
    ctx: ToolContext,
    tool: str,
    arguments: Mapping[str, Any],
    work: Callable[[Session, Caller], T],
) -> T:
    """Run one tool call against the service layer; see the module docstring."""
    caller = caller_of(ctx)
    log = logger.bind(
        server=ctx.fastmcp.name,
        tool=tool,
        run_id=caller.run_id,
        actor=caller.actor,
        arguments=to_jsonable_python(dict(arguments), fallback=str),
    )
    started = time.perf_counter()

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 1)

    try:
        result = await anyio.to_thread.run_sync(_in_transaction, work, caller)
    except CustomsCopilotError as exc:
        log.warning("mcp.tool_call", outcome="error", error=type(exc).__name__, message=str(exc),
                    duration_ms=elapsed())  # fmt: skip
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc
    except Exception as exc:
        if _is_connection_failure(exc):
            error = DatabaseUnavailableError("the database is unreachable; retry the call later")
            log.error("mcp.tool_call", outcome="error", error=type(error).__name__,
                      cause=type(exc).__name__, duration_ms=elapsed())  # fmt: skip
            raise ToolError(f"{type(error).__name__}: {error}") from exc
        log.exception("mcp.tool_call", outcome="error", error="UnexpectedError",
                      duration_ms=elapsed())  # fmt: skip
        raise ToolError(f"UnexpectedError: {type(exc).__name__} in {tool}") from exc

    log.info("mcp.tool_call", outcome="ok", result=to_jsonable_python(result, fallback=str),
             duration_ms=elapsed())  # fmt: skip
    return result


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
