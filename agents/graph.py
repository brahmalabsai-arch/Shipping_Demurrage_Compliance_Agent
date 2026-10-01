"""The shipment pipeline (architecture §6), over one shared ``CaseState``::

    document_verification_agent ─┬─> demurrage_risk_agent ─┬─> clearance_checklist_agent ─> END
                                  └─> compliance_rag_agent ─┘

Both middle branches read what verification found (the risk weighs the discrepancies; the
compliance agent asks the rulebook about them) and are independent of each other, so LangGraph
runs them in parallel, each writing only its own ``CaseState`` fields; the checklist waits for
both (fan-in). ``analyse_shipment`` runs exactly that (read-only, not checkpointed); the Phase 11
supervisor (``agents/supervisor.py``) wires the same pipeline (:func:`add_pipeline`) in front of
its human gates.

Each agent keeps its own role: it gets its own MCP toolbox (only its servers, its own actor
name on every call) through ``config["configurable"][<agent name>]``. One ``run_id`` threads
through all of them, so the audit trail and the tool-call logs of one analysis correlate.

The three upstream agents run behind :func:`guarded`: one that raises, or has not finished
within ``AGENT_TIMEOUT_SECONDS``, is recorded as an ``AgentFailure`` (and an ``agent.failed``
audit event) and the checklist is still built from the others (``missing_inputs``). A
cancelled shipment is the exception — ``ShipmentCancelledError`` stops the whole pipeline.
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import date
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agents import clearance_checklist_agent as checklist
from agents import compliance_rag_agent as compliance
from agents import demurrage_risk_agent as risk
from agents import document_verification_agent as verification
from agents import llm
from agents.audit import record_run_event
from agents.ist import today_ist
from agents.mcp_clients import Connector, open_toolbox, stdio_connector
from agents.state import AgentFailure, CaseState
from config.settings import get_settings
from services.errors import ShipmentCancelledError

# A LangGraph node: async (state, config) -> state update. (Its protocol names the `config`
# parameter, which a plain Callable type cannot express.)
Node = Callable[..., Awaitable[dict[str, Any]]]

logger = structlog.get_logger("agents")


def guarded(agent: ModuleType) -> Node:
    """The agent's node (looked up now, by its name), with failures turned into state."""
    name: str = agent.AGENT
    node: Node = getattr(agent, name)

    async def run(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
        timeout = get_settings().agent_timeout_seconds
        try:
            return await asyncio.wait_for(node(state, config), timeout)
        except ShipmentCancelledError:
            raise  # not a failure: the shipment is gone, the whole analysis stops
        except TimeoutError:
            failure = AgentFailure(agent=name, kind="timeout",
                                   error=f"no result within {timeout:g} s")  # fmt: skip
        except Exception as exc:
            failure = AgentFailure(agent=name, kind="error", error=f"{type(exc).__name__}: {exc}")
        logger.warning("agent.failed", agent=name, run_id=state.run_id, kind=failure.kind,
                       error=failure.error)  # fmt: skip
        record_run_event(name, state.run_id, "agent.failed", failure.model_dump())
        return {"agent_failures": [failure]}

    return run


def add_pipeline(graph: StateGraph[CaseState, Any, CaseState, CaseState]) -> None:
    """Add the four agents from START up to the checklist node (the caller wires what follows):
    verification first; demurrage & risk and compliance in parallel; then the checklist."""
    for agent in (verification, risk, compliance):
        graph.add_node(agent.AGENT, guarded(agent))
    graph.add_node(checklist.AGENT, checklist.clearance_checklist_agent)
    graph.add_edge(START, verification.AGENT)
    graph.add_edge(verification.AGENT, risk.AGENT)
    graph.add_edge(verification.AGENT, compliance.AGENT)
    graph.add_edge([risk.AGENT, compliance.AGENT], checklist.AGENT)  # fan-in: waits for both


def build_graph() -> CompiledStateGraph[CaseState, Any, CaseState, CaseState]:
    """The analysis pipeline alone, ending at the checklist."""
    graph = StateGraph(CaseState)
    add_pipeline(graph)
    graph.add_edge(checklist.AGENT, END)
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
