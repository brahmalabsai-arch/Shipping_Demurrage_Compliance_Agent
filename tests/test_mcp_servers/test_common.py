"""Phase 5 — the MCP layer as a whole: coverage of every tool, naming, error handling (edge
cases), logging, no caching, and a real stdio launch of every server."""

import importlib
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg
import pytest
import structlog
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from config.settings import PROJECT_ROOT
from models.enums import ShipmentStatus
from models.schemas import SpotRateUpsert
from services import compliance_service, demurrage_service, fx_service, shipping_service
from services import db as db_module
from tests.test_mcp_servers.conftest import assert_parity, assert_tool_error, connect, structured

SERVERS = (
    "shipping_records_mcp",
    "demurrage_rates_mcp",
    "fx_rates_mcp",
    "compliance_rulebook_mcp",
    "customs_bulletin_mcp",
    "notifications_mcp",
)
# conventions §2 verbs, plus the two the architecture contract itself names (search_, diff_)
# and the two governed acts of Phase 11 (revoke_decision, clear_shipment)
VERBS = {
    "get",
    "list",
    "create",
    "update",
    "propose",
    "decide",
    "send",
    "resolve",
    "search",
    "diff",
    "revoke",
    "clear",
}


def _server(name: str) -> FastMCP:
    module = importlib.import_module(f"mcp_servers.{name}.server")
    server: FastMCP = module.mcp
    return server


async def _tool_names(name: str) -> set[str]:
    async with connect(_server(name)) as client:
        return {tool.name for tool in (await client.list_tools()).tools}


# --------------------------------------------------------------------------- coverage & naming


@pytest.mark.parametrize("name", SERVERS)
async def test_every_tool_of_every_server_has_a_parity_test(name: str) -> None:
    """Exit criterion: the reference client exercises every tool on every server."""
    tests = importlib.import_module(f"tests.test_mcp_servers.test_{name}")

    assert await _tool_names(name) == tests.TOOLS_TESTED


async def test_tool_names_are_verb_first_and_unique_across_servers() -> None:
    names = [tool for server in SERVERS for tool in await _tool_names(server)]

    assert {n.split("_")[0] for n in names} <= VERBS
    assert len(names) == len(set(names)), "agents load several servers at once: no clashes"


async def test_every_tool_returns_structured_output() -> None:
    for name in SERVERS:
        async with connect(_server(name)) as client:
            missing = [t.name for t in (await client.list_tools()).tools if not t.outputSchema]
        assert missing == [], name


# --------------------------------------------------------------------------- edge cases


def _unreachable_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    return sessionmaker(bind=engine)


async def test_unreachable_database_is_a_typed_error_and_the_server_survives(
    seeded: None, direct: Any, monkeypatch: pytest.MonkeyPatch, session_factory: Any
) -> None:
    """Edge case: the database connection fails mid-session → a typed tool error, and the same
    client keeps working once the database is back."""
    server = _server("demurrage_rates_mcp")
    async with connect(server) as client:
        monkeypatch.setattr(db_module, "get_session_factory", _unreachable_factory)
        down = await client.call_tool("list_ports", {})
        monkeypatch.setattr(db_module, "get_session_factory", lambda: session_factory)
        back = await client.call_tool("list_ports", {})

    assert_tool_error(down, "DatabaseUnavailableError")
    assert_parity(back, direct(demurrage_service.list_ports))


async def test_connection_dropped_mid_query_is_a_typed_error(
    seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def dropped(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError(
            "SELECT ...", {}, psycopg.OperationalError("server closed the connection unexpectedly")
        )

    async with connect(_server("shipping_records_mcp")) as client:
        with monkeypatch.context() as patch:
            patch.setattr(shipping_service, "list_shipments", dropped)
            down = await client.call_tool("list_shipments", {})
        back = await client.call_tool("list_shipments", {})

    text = assert_tool_error(down, "DatabaseUnavailableError")
    assert "server closed" not in text  # no driver internals leak to the caller
    assert len(structured(back)["result"]) == 4


async def test_unexpected_failure_is_reported_without_internals(
    monkeypatch: pytest.MonkeyPatch, session_factory: Any
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("secret internals")

    monkeypatch.setattr(demurrage_service, "list_ports", broken)
    async with connect(_server("demurrage_rates_mcp")) as client:
        result = await client.call_tool("list_ports", {})

    text = assert_tool_error(result, "UnexpectedError")
    assert "secret internals" not in text


MALFORMED: list[tuple[str, str, dict[str, Any], Any, str]] = [
    ("shipping_records_mcp", "get_shipment", {"shipment_id": "abc"}, shipping_service, "get_shipment"),
    ("shipping_records_mcp", "update_status", {"shipment_id": str(uuid4()), "status": "lost"},
     shipping_service, "update_status"),
    ("demurrage_rates_mcp", "get_demurrage_charge",
     {"port_code": "INNSA", "container_type": "40ft", "as_of": "2026-09-08", "days_in_port": "many"},
     demurrage_service, "charge_for_stay"),
    ("fx_rates_mcp", "resolve_rate", {"pair": "USD/INR", "as_of": "2026-13-45", "stage": "bol"},
     fx_service, "resolve_rate"),
    ("fx_rates_mcp", "resolve_rate", {"pair": "USD/INR", "as_of": "2026-08-22", "stage": "customs"},
     fx_service, "resolve_rate"),
    ("compliance_rulebook_mcp", "decide",
     {"rule_id": str(uuid4()), "decision": "maybe", "reviewer": "a.sharma"},
     compliance_service, "decide"),
    ("compliance_rulebook_mcp", "propose_change", {"proposal": {"rule_key": "bad key!"}},
     compliance_service, "propose_change"),
]  # fmt: skip


@pytest.mark.parametrize(("server", "tool", "arguments", "module", "function"), MALFORMED)
async def test_malformed_arguments_are_rejected_before_the_service(
    monkeypatch: pytest.MonkeyPatch,
    server: str,
    tool: str,
    arguments: dict[str, Any],
    module: Any,
    function: str,
) -> None:
    """Edge case: a wrong-typed argument fails schema validation at the tool boundary."""
    calls: list[Any] = []
    monkeypatch.setattr(module, function, lambda *a, **k: calls.append(a))
    async with connect(_server(server)) as client:
        result = await client.call_tool(tool, arguments)

    assert result.isError
    assert "validation error" in result.content[0].text  # type: ignore[union-attr]
    assert calls == []


async def test_reads_right_after_a_direct_write_see_it(
    seeded: None, direct: Any, session_factory: sessionmaker[Session]
) -> None:
    """Edge case: no caching layer — a service write (as the dashboards make) is visible to the
    very next tool call, with no server restart."""
    async with (
        connect(_server("fx_rates_mcp")) as fx,
        connect(_server("shipping_records_mcp")) as shipping,
    ):
        before = structured(
            await fx.call_tool("get_spot_rate", {"pair": "USD/INR", "as_of": "2026-10-06"})
        )
        with session_factory() as session, session.begin():
            fx_service.upsert_spot_rate(
                session,
                SpotRateUpsert(pair="USD/INR", rate=Decimal("88.7"),
                               as_of=datetime(2026, 10, 6, 4, tzinfo=UTC), source="desk"),
                "ops-dashboard",
            )  # fmt: skip
            clean = shipping_service.get_shipment_by_container_id(session, "MSKU7001001")
            shipping_service.update_status(session, clean.id, ShipmentStatus.UNDER_REVIEW, "ops")
        after = await fx.call_tool("get_spot_rate", {"pair": "USD/INR", "as_of": "2026-10-06"})
        shipment = await shipping.call_tool("get_shipment", {"shipment_id": str(clean.id)})

    assert before["rate"] == "88.620000"  # the 10-05 observation
    assert_parity(
        after, direct(lambda s: fx_service.get_spot_rate(s, "USD/INR", date(2026, 10, 6)))
    )
    assert structured(after)["rate"] == "88.700000"
    assert structured(shipment)["status"] == "under_review"


# --------------------------------------------------------------------------- logging


async def test_every_call_is_logged_with_caller_arguments_outcome_and_timing(
    seeded: None,
) -> None:
    meta = {"actor": "demurrage_risk_agent", "run_id": "run-7"}
    with structlog.testing.capture_logs() as logs:
        async with connect(_server("demurrage_rates_mcp")) as client:
            await client.call_tool("list_ports", {}, meta=meta)
            await client.call_tool(
                "get_rate", {"port_code": "XXABC", "container_type": "20ft", "as_of": "2026-09-08"},
                meta=meta,
            )  # fmt: skip

    ok, failed = [e for e in logs if e["event"] == "mcp.tool_call"]
    assert (
        ok["tool"] == "list_ports" and ok["outcome"] == "ok" and ok["result"] == ["INMUN", "INNSA"]
    )
    assert failed["outcome"] == "error" and failed["error"] == "PortRateNotFoundError"
    assert failed["arguments"] == {"port_code": "XXABC", "container_type": "20ft",
                                   "as_of": "2026-09-08"}  # fmt: skip
    for event in (ok, failed):
        assert (event["run_id"], event["actor"], event["server"]) == (
            "run-7", "demurrage_risk_agent", "demurrage_rates_mcp")  # fmt: skip
        assert event["duration_ms"] >= 0


# --------------------------------------------------------------------------- stdio transport


@pytest.mark.parametrize("name", SERVERS)
async def test_server_runs_over_stdio(name: str) -> None:
    """Each server starts as its own process (``python -m ...``) and speaks MCP over stdio."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", f"mcp_servers.{name}.server"],
        cwd=str(PROJECT_ROOT),
        # listing tools never touches the database; CI's unit job has no .env; and it needs
        # no retrieval models (compliance_rulebook_mcp would otherwise load them at startup)
        env={
            **os.environ,
            "POSTGRES_PASSWORD": os.environ.get("POSTGRES_PASSWORD", "unused"),
            "RAG_PRELOAD_MODELS": "false",
        },
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        init = await client.initialize()
        tools = {tool.name for tool in (await client.list_tools()).tools}

    assert init.serverInfo.name == name
    assert tools == await _tool_names(name)
