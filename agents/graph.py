"""The shipment pipeline as built so far (architecture §6), over one shared ``CaseState``::

    document_verification_agent ─┬─> demurrage_risk_agent ──> END
                                  └─> compliance_rag_agent ──> END

Both branches read what verification found (the risk weighs the discrepancies; the compliance
agent asks the rulebook about them) and are independent of each other, so LangGraph runs them
in parallel, each writing only its own ``CaseState`` fields. Phases 10-11 add the checklist
fan-in and the supervisor's human gates.

Each agent keeps its own role: it gets its own MCP toolbox (only its servers, its own actor
name on every call) through ``config["configurable"][<agent name>]``. One ``run_id`` threads
through all of them, so the audit trail and the tool-call logs of one analysis correlate. A
cancelled shipment stops the pipeline at the first node with ``ShipmentCancelledError``.
"""

from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents import compliance_rag_agent as compliance
from agents import demurrage_risk_agent as risk
from agents import document_verification_agent as verification
from agents import llm
from agents.ist import today_ist
from agents.mcp_clients import Connector, open_toolbox, stdio_connector
from agents.state import CaseState


def build_graph() -> CompiledStateGraph[CaseState, Any, CaseState, CaseState]:
    """Verification first; then demurrage & risk and compliance in parallel."""
    graph = StateGraph(CaseState)
    graph.add_node(verification.AGENT, verification.document_verification_agent)
    graph.add_node(risk.AGENT, risk.demurrage_risk_agent)
    graph.add_node(compliance.AGENT, compliance.compliance_rag_agent)
    graph.add_edge(START, verification.AGENT)
    graph.add_edge(verification.AGENT, risk.AGENT)
    graph.add_edge(verification.AGENT, compliance.AGENT)
    graph.add_edge(risk.AGENT, END)
    graph.add_edge(compliance.AGENT, END)
    return graph.compile()


async def analyse_shipment(
    shipment_id: UUID,
    *,
    connector: Connector | None = None,
    model_factory: Callable[[], BaseChatModel | None] | None = None,
    as_of: date | None = None,
    run_id: str | None = None,
) -> CaseState:
    """Run the pipeline for one shipment. Defaults: stdio MCP servers; the configured summary
    model for verification and reasoning model for compliance (``model_factory`` replaces
    both); today's IST date as the analysis date."""
    run_id = run_id or str(uuid4())
    connect = connector or stdio_connector

    async def toolbox(agent: Any, stack: AsyncExitStack) -> Any:
        return await stack.enter_async_context(
            open_toolbox(agent.SERVERS, actor=agent.AGENT, run_id=run_id, connector=connect)
        )

    async with AsyncExitStack() as stack:
        deps = {
            verification.AGENT: verification.VerificationDeps(
                toolbox=await toolbox(verification, stack),
                model=(model_factory or llm.summary_model)(),
            ),
            risk.AGENT: risk.RiskDeps(await toolbox(risk, stack)),
            compliance.AGENT: compliance.ComplianceDeps(
                toolbox=await toolbox(compliance, stack),
                model=(model_factory or llm.reasoning_model)(),
            ),
        }
        result = await build_graph().ainvoke(
            CaseState(run_id=run_id, shipment_id=shipment_id, as_of=as_of or today_ist()),
            config={"configurable": deps},
        )
    return CaseState.model_validate(result)
