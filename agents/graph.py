"""The shipment pipeline as built so far (architecture §6): document verification → demurrage
& risk, over one shared ``CaseState``. Phases 9-11 extend it (compliance RAG, the checklist
fan-in, the supervisor's human gates).

Each agent keeps its own role: it gets its own MCP toolbox (only its servers, its own actor
name on every call) through ``config["configurable"][<agent name>]``. One ``run_id`` threads
through both, so the audit trail and the tool-call logs of one analysis correlate. A cancelled
shipment stops the pipeline at the first node with ``ShipmentCancelledError``.
"""

from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents import demurrage_risk_agent as risk
from agents import document_verification_agent as verification
from agents import llm
from agents.ist import today_ist
from agents.mcp_clients import Connector, open_toolbox, stdio_connector
from agents.state import CaseState


def build_graph() -> CompiledStateGraph[CaseState, Any, CaseState, CaseState]:
    """document_verification_agent → demurrage_risk_agent (the risk reads the discrepancies)."""
    graph = StateGraph(CaseState)
    graph.add_node(verification.AGENT, verification.document_verification_agent)
    graph.add_node(risk.AGENT, risk.demurrage_risk_agent)
    graph.add_edge(START, verification.AGENT)
    graph.add_edge(verification.AGENT, risk.AGENT)
    graph.add_edge(risk.AGENT, END)
    return graph.compile()


async def analyse_shipment(
    shipment_id: UUID,
    *,
    connector: Connector | None = None,
    model_factory: Callable[[], BaseChatModel | None] | None = None,
    as_of: date | None = None,
    run_id: str | None = None,
) -> CaseState:
    """Run the pipeline for one shipment. Defaults: stdio MCP servers, the configured summary
    model, today's IST date as the analysis date."""
    run_id = run_id or str(uuid4())
    connect = connector or stdio_connector
    async with AsyncExitStack() as stack:
        verification_tools = await stack.enter_async_context(
            open_toolbox(
                verification.SERVERS, actor=verification.AGENT, run_id=run_id, connector=connect
            )
        )
        risk_tools = await stack.enter_async_context(
            open_toolbox(risk.SERVERS, actor=risk.AGENT, run_id=run_id, connector=connect)
        )
        deps = {
            verification.AGENT: verification.VerificationDeps(
                toolbox=verification_tools, model=(model_factory or llm.summary_model)()
            ),
            risk.AGENT: risk.RiskDeps(risk_tools),
        }
        result = await build_graph().ainvoke(
            CaseState(run_id=run_id, shipment_id=shipment_id, as_of=as_of or today_ist()),
            config={"configurable": deps},
        )
    return CaseState.model_validate(result)
