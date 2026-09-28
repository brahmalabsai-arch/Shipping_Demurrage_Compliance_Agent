"""MCP test harness: a real MCP client session talking to each server in memory.

Calls travel the full protocol path (JSON-RPC request → FastMCP argument validation → tool →
service → structured result → client), while the servers' ``session_scope()`` still lands in
the rolled-back test transaction (the ``session_factory`` fixture patches it).

Parity (Phase 5 exit criterion) is asserted on the wire payload: the tool's
``structuredContent`` must equal, byte for byte as canonical JSON, the direct service call's
result serialised the way the MCP SDK serialises it (a model as its JSON object; anything else
wrapped as ``{"result": ...}``).
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import pytest
from mcp import ClientSession
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel
from pydantic_core import to_jsonable_python
from sqlalchemy.orm import Session, sessionmaker

from scripts import seed_compliance, seed_fx_rates, seed_port_rates, seed_shipments

T = TypeVar("T")


@asynccontextmanager
async def connect(server: FastMCP) -> AsyncIterator[ClientSession]:
    async with create_connected_server_and_client_session(server) as client:
        yield client


def canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def wire(value: Any, *, wrapped: bool = False) -> Any:
    """How the MCP SDK puts a tool's return value into ``structuredContent``: a model as its
    JSON object — unless the declared return type is optional/union (``wrapped``) — and
    anything else inside ``{"result": ...}``."""
    if isinstance(value, BaseModel) and not wrapped:
        return value.model_dump(mode="json", by_alias=True)
    return {"result": to_jsonable_python(value)}


def structured(result: CallToolResult) -> Any:
    assert not result.isError, error_text(result)
    return result.structuredContent


def error_text(result: CallToolResult) -> str:
    assert result.content and isinstance(result.content[0], TextContent)
    return result.content[0].text


def assert_parity(result: CallToolResult, direct: Any) -> None:
    payload = structured(result)
    wrapped = isinstance(direct, BaseModel) and set(payload) == {"result"}
    assert canonical(payload) == canonical(wire(direct, wrapped=wrapped))


def assert_tool_error(result: CallToolResult, error_type: str) -> str:
    assert result.isError, result.structuredContent
    text = error_text(result)
    assert f"{error_type}: " in text, text
    return text


@pytest.fixture
def direct(session_factory: sessionmaker[Session]) -> Callable[[Callable[[Session], T]], T]:
    """Run a direct service call in a fresh session (no identity-map staleness after a tool
    call has written through its own session)."""

    def run(fn: Callable[[Session], T]) -> T:
        with session_factory() as session:
            return fn(session)

    return run


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> None:
    """All domains seeded exactly as ``python -m scripts.seed`` does."""
    seed_shipments.seed()
    seed_port_rates.seed()
    seed_fx_rates.seed()
    seed_compliance.seed()
