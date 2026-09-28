"""The agents' MCP client wrapper: typed tool calls to the servers a role is allowed to use.

``open_toolbox(servers, actor=..., run_id=...)`` connects to the named MCP servers only (an
agent imports nothing else — conventions §4) and yields an :class:`McpToolbox` whose ``call``

* sends the agent's identity and ``run_id`` as ``_meta`` (the servers audit writes under that
  actor and log the call under that run),
* logs one ``agent.tool_call`` event per call (server, tool, arguments, outcome, timing),
* returns the tool's structured result (unwrapped from ``{"result": ...}``) or raises
  :class:`~services.errors.McpToolError` carrying the server-side error type.

Transports: ``stdio_connector`` starts each server as its own process
(``python -m mcp_servers.<name>.server``, architecture §1) — the default. ``memory_connector``
runs a server in-process over memory streams (tests; same code path through the protocol).
"""

import importlib
import os
import re
import sys
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Any

import structlog
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent
from pydantic_core import to_jsonable_python

from config.settings import PROJECT_ROOT
from services.errors import McpToolError

Connector = Callable[[str], AbstractAsyncContextManager[ClientSession]]

logger = structlog.get_logger("agents")

# "Error executing tool <name>: <ErrorType>: <message>" (FastMCP prefix optional)
_ERROR = re.compile(
    r"^(?:Error executing tool \S+: )?(?P<type>[A-Za-z]+Error): (?P<message>.*)$", re.S
)


@asynccontextmanager
async def stdio_connector(server: str) -> AsyncIterator[ClientSession]:
    """Start ``mcp_servers.<server>.server`` as a subprocess and talk MCP over its stdio."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", f"mcp_servers.{server}.server"],
        cwd=str(PROJECT_ROOT),
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


@asynccontextmanager
async def memory_connector(server: str) -> AsyncIterator[ClientSession]:
    """Run the server in-process over memory streams (full MCP protocol, no subprocess)."""
    module = importlib.import_module(f"mcp_servers.{server}.server")
    async with create_connected_server_and_client_session(module.mcp) as session:
        yield session


def _error(tool: str, result: CallToolResult) -> McpToolError:
    text = next((c.text for c in result.content if isinstance(c, TextContent)), "")
    match = _ERROR.match(text.strip())
    if match:
        return McpToolError(tool, match["type"], match["message"])
    if "validation error" in text:
        return McpToolError(tool, "ValidationError", text)
    return McpToolError(tool, "ToolError", text)


class McpToolbox:
    """Open sessions to a fixed set of servers, called on behalf of one agent run."""

    def __init__(self, sessions: dict[str, ClientSession], *, actor: str, run_id: str) -> None:
        self._sessions = sessions
        self.actor = actor
        self.run_id = run_id

    async def call(self, server: str, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call ``tool`` on ``server``; return its structured result or raise McpToolError."""
        if server not in self._sessions:
            raise McpToolError(
                tool, "ServerNotAllowedError", f"{self.actor} has no {server} access"
            )
        args = to_jsonable_python(arguments or {})
        log = logger.bind(server=server, tool=tool, arguments=args, actor=self.actor,
                          run_id=self.run_id)  # fmt: skip
        started = time.perf_counter()
        result = await self._sessions[server].call_tool(
            tool, args, meta={"actor": self.actor, "run_id": self.run_id}
        )
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        if result.isError:
            error = _error(tool, result)
            log.warning("agent.tool_call", outcome="error", error=error.error_type,
                        message=error.message, duration_ms=duration_ms)  # fmt: skip
            raise error
        payload = result.structuredContent or {}
        value = payload["result"] if set(payload) == {"result"} else payload
        log.info("agent.tool_call", outcome="ok", duration_ms=duration_ms)
        return value


@asynccontextmanager
async def open_toolbox(
    servers: Sequence[str],
    *,
    actor: str,
    run_id: str,
    connector: Connector = stdio_connector,
) -> AsyncIterator[McpToolbox]:
    """Connect to ``servers`` (and only those) for one agent run."""
    async with AsyncExitStack() as stack:
        sessions = {name: await stack.enter_async_context(connector(name)) for name in servers}
        yield McpToolbox(sessions, actor=actor, run_id=run_id)
