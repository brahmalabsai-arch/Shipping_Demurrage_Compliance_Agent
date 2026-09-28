"""Phase 6 — the agents' MCP client wrapper: typed results and errors, role-restricted server
access, _meta propagation, and a real stdio round trip."""

import os
from typing import Any

import pytest
import structlog
from mcp.types import CallToolResult, TextContent
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from agents import mcp_clients
from agents.mcp_clients import memory_connector, open_toolbox, stdio_connector
from config.settings import get_settings
from models.db_models import AuditEvent
from scripts import seed_port_rates
from services.errors import McpToolError


@pytest.mark.parametrize(
    ("text", "error_type", "message"),
    [
        ("Error executing tool decide: RuleAlreadyDecidedError: HSN v2 is already rejected",
         "RuleAlreadyDecidedError", "HSN v2 is already rejected"),
        ("EmailDeliveryError: backend down: a: b", "EmailDeliveryError", "backend down: a: b"),
        ("Error executing tool get_rule: 1 validation error for get_ruleArguments\nrule_id ...",
         "ValidationError", None),
        ("something odd", "ToolError", "something odd"),
    ],
)  # fmt: skip
def test_error_text_is_parsed_into_a_typed_error(
    text: str, error_type: str, message: str | None
) -> None:
    result = CallToolResult(content=[TextContent(type="text", text=text)], isError=True)

    error = mcp_clients._error("tool", result)

    assert error.error_type == error_type
    assert message is None or error.message == message


async def test_results_are_unwrapped_and_calls_carry_the_agent_identity(
    session_factory: sessionmaker[Session],
) -> None:
    seed_port_rates.seed()
    with structlog.testing.capture_logs() as logs:
        async with open_toolbox(
            ["demurrage_rates_mcp"], actor="demurrage_risk_agent", run_id="run-9",
            connector=memory_connector,
        ) as tools:  # fmt: skip
            ports = await tools.call("demurrage_rates_mcp", "list_ports")
            rate = await tools.call("demurrage_rates_mcp", "update_rate", {"rate": {
                "port_code": "INNSA", "container_type": "20ft", "currency": "USD",
                "free_days": 4, "daily_rate": "60.00", "effective_from": "2026-12-01"}})  # fmt: skip

    assert ports == ["INMUN", "INNSA"]  # {"result": [...]} unwrapped
    assert rate["daily_rate"] == "60.00"  # a model result comes back as its object
    with session_factory() as session:
        actors = set(session.scalars(select(AuditEvent.actor).where(
            AuditEvent.action.like("port_rate.%"), AuditEvent.actor != "seed_script")))  # fmt: skip
    assert actors == {"demurrage_risk_agent"}
    calls = [e for e in logs if e["event"] == "agent.tool_call"]
    assert {(e["tool"], e["run_id"], e["outcome"]) for e in calls} == {
        ("list_ports", "run-9", "ok"), ("update_rate", "run-9", "ok")}  # fmt: skip


async def test_tool_errors_raise_typed_errors(session_factory: sessionmaker[Session]) -> None:
    async with open_toolbox(["demurrage_rates_mcp"], actor="a", run_id="r",
                            connector=memory_connector) as tools:  # fmt: skip
        with pytest.raises(McpToolError) as caught:
            await tools.call("demurrage_rates_mcp", "get_rate",
                             {"port_code": "XXABC", "container_type": "20ft", "as_of": "2026-09-08"})  # fmt: skip

    assert caught.value.error_type == "PortRateNotFoundError"


async def test_an_agent_cannot_call_a_server_outside_its_role() -> None:
    async with open_toolbox(["demurrage_rates_mcp"], actor="a", run_id="r",
                            connector=memory_connector) as tools:  # fmt: skip
        with pytest.raises(McpToolError) as caught:
            await tools.call("compliance_rulebook_mcp", "decide", {})

    assert caught.value.error_type == "ServerNotAllowedError"


async def test_stdio_connector_round_trip(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production transport: the server runs as its own process and answers a real query
    (pointed at the migrated test database, whose committed state is empty)."""
    monkeypatch.setitem(os.environ, "POSTGRES_DB", get_settings().postgres_test_db)
    async with open_toolbox(["demurrage_rates_mcp"], actor="a", run_id="r",
                            connector=stdio_connector) as tools:  # fmt: skip
        ports: Any = await tools.call("demurrage_rates_mcp", "list_ports")

    assert ports == []
