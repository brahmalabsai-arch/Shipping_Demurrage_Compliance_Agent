"""The supervisor (architecture §6, Phase 11): the shipment pipeline behind two human gates,
checkpointed in Postgres so a run can wait for people — across processes — and resume where it
stopped::

    verification ─┬─> risk ───────┬─> checklist ─> compliance_gate ──(no pending rule)──> filing_gate
                  └─> compliance ─┘                  │     ^                                │
                               one per pending rule  v     │ (re-ask the rulebook +     approved
                         await_compliance_decision ─> reassess_after_rule_change      ─> file_clearance

**Compliance gate.** After the checklist, every *pending* rule proposal covering the shipment's
HS code blocks the run: one ``interrupt()`` per rule (parallel ``Send`` branches — so two
reviewers can decide two of them independently). Only ``compliance_service.decide`` decides a
rule (the Compliance page, in any process); :func:`resume_after_compliance_decision` then
resumes the run, and the gate re-reads the rule from the rulebook — the resume carries no
decision. An approval (or revocation) changes the rulebook, so the compliance agent and the
checklist run again; a rejection is final for that proposal (logged, never re-queued) and the
run carries on under the rulebook in force.

Each gate calls ``interrupt()`` once per execution; a refused answer ends that execution
without effect and the graph routes back to the gate, which waits anew.

**Filing gate.** Only a checklist that is ready for clearance (no blocker, data gap, missing
input or conflict) and a shipment that is docked or under review reach it — otherwise the run
ends ``blocked`` / ``not_fileable``. :func:`resume_after_filing_approval` — a registered reviewer
approving or rejecting — is the only way past it: it records the ``governance.filing_decisions``
row first and resumes with that row's id; anything else sent to the gate is refused and the run
keeps waiting. A rejection ends the run (``filing_rejected``, final: a new run is needed). An
approval files the shipment: ``docked -> under_review`` and ``clear_shipment`` over
``shipping_records_mcp`` — the only path to ``cleared``, which also re-checks the checklist and
raises ``IncompleteCaseError`` before any MCP call if it is not ready.

**Bookkeeping.** ``governance.case_runs`` mirrors where each run stands (dashboards read it);
the supervisor writes it (and reads filing decisions) through ``governance_service`` directly —
orchestration records, like the agents' ``agent_run`` audit events. Domain data is reached only
through MCP (actor ``supervisor``). Resumptions of one run are serialised by a Postgres advisory
lock on the ``run_id`` (``agents.checkpoint.run_lock``). Every interrupt has exactly one named
resumption function (conventions §5).
"""

import functools
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import date
from typing import Any, cast
from uuid import UUID, uuid4

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Send, interrupt
from pydantic import BaseModel, TypeAdapter

from agents import checkpoint, graph, llm, mcp_clients
from agents import clearance_checklist_agent as checklist
from agents import compliance_rag_agent as compliance
from agents import demurrage_risk_agent as risk
from agents import document_verification_agent as verification
from agents.audit import record_run_event
from agents.ist import today_ist
from agents.mcp_clients import Connector, McpToolbox, open_toolbox, stdio_connector
from agents.state import CaseState, GateDecision, RunOutcome
from models.enums import RuleDecision, RuleStatus, RunState, ShipmentStatus
from models.schemas import (
    CaseRunRecord,
    ComplianceRuleRecord,
    DocumentRecord,
    FilingDecisionRecord,
    ShipmentRecord,
)
from services import compliance_service, governance_service
from services.db import session_scope
from services.errors import (
    ClearanceRequiresApprovalError,
    DecisionPendingError,
    FilingAlreadyDecidedError,
    FilingDecisionNotFoundError,
    IncompleteCaseError,
    InvalidFilingDecisionError,
    McpToolError,
    RunAlreadyFinishedError,
    RunNotAtGateError,
    ShipmentCancelledError,
)

SUPERVISOR = "supervisor"
SHIPPING, RULEBOOK = "shipping_records_mcp", "compliance_rulebook_mcp"
SERVERS = (SHIPPING, RULEBOOK)

COMPLIANCE_GATE = "compliance_gate"
AWAIT_COMPLIANCE = "await_compliance_decision"
REASSESS = "reassess_after_rule_change"
FILING_GATE = "filing_gate"
FILE = "file_clearance"

# Filing moves `docked -> under_review -> cleared`; a held box needs customs' release first.
FILEABLE = frozenset({ShipmentStatus.DOCKED, ShipmentStatus.UNDER_REVIEW})
ENDED_AS: dict[RunOutcome, RunState] = {
    "filed": RunState.FILED,
    "filing_rejected": RunState.FILING_REJECTED,
    "blocked": RunState.BLOCKED,
    "not_fileable": RunState.NOT_FILEABLE,
}
PIPELINE = (verification.AGENT, risk.AGENT, compliance.AGENT)

logger = structlog.get_logger("agents")


@dataclass(frozen=True)
class SupervisorDeps:
    """The supervisor's own MCP access (shipping records + the rulebook, actor ``supervisor``)."""

    toolbox: McpToolbox


class ClearanceRun(BaseModel):
    """Where a supervised run stands: its index row, its checkpointed ``CaseState`` and the
    requests of the gates it waits at (``{"gate": "compliance" | "filing", ...}``)."""

    run: CaseRunRecord
    case: CaseState
    waiting_on: list[dict[str, Any]]


def _deps(config: RunnableConfig) -> SupervisorDeps:
    deps: SupervisorDeps = config["configurable"][SUPERVISOR]
    return deps


async def _active_shipment(toolbox: McpToolbox, shipment_id: UUID) -> ShipmentRecord:
    try:
        raw = await toolbox.call(SHIPPING, "get_active_shipment", {"shipment_id": shipment_id})
    except McpToolError as exc:
        if exc.error_type == "ShipmentCancelledError":
            raise ShipmentCancelledError(exc.message) from exc
        raise
    return ShipmentRecord.model_validate(raw)


def _is_request_for(answer: Any, gate: str, **expected: str) -> bool:
    """A resume value is only accepted from this gate's own resumption function."""
    return (
        isinstance(answer, dict)
        and answer.get("gate") == gate
        and all(answer.get(key) == value for key, value in expected.items())
    )


# --------------------------------------------------------------------------- compliance gate


async def compliance_gate(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    """Find the pending rule proposals that cover the shipment's goods (the run waits on each)."""
    toolbox = _deps(config).toolbox
    shipment = await _active_shipment(toolbox, state.shipment_id)
    pending = TypeAdapter(list[ComplianceRuleRecord]).validate_python(
        await toolbox.call(RULEBOOK, "list_pending")
    )
    covering = [rule for rule in pending if rule.applies_to(shipment.hs_code)]
    if covering:
        record_run_event(SUPERVISOR, state.run_id, "supervisor.compliance_gate_waiting", {
            "shipment_id": str(state.shipment_id),
            "hs_code": shipment.hs_code,
            "rules": [{"rule_id": str(r.id), "rule_key": r.rule_key, "version": r.version}
                      for r in covering],
        })  # fmt: skip
    return {"pending_rules": covering}


def _route_compliance(state: CaseState) -> list[Send] | str:
    if not state.pending_rules:
        return FILING_GATE
    return [
        Send(AWAIT_COMPLIANCE, {"gate": "compliance", "run_id": state.run_id,
                                "rule_id": str(rule.id), "rule_key": rule.rule_key,
                                "version": rule.version, "hs_codes": rule.hs_codes})
        for rule in state.pending_rules
    ]  # fmt: skip


async def await_compliance_decision(
    request: dict[str, Any], config: RunnableConfig
) -> dict[str, Any]:
    """Wait (``interrupt``) until the rule is decided, then read how from the rulebook.

    Resumed only by :func:`resume_after_compliance_decision`. Any other resume value, or one
    while the rule is still pending, records nothing: the rule is still pending, so the gate
    finds it again and waits on it anew (one ``interrupt`` per execution — a node never loops
    on ``interrupt``).
    """
    rule_id = request["rule_id"]
    answer = interrupt(request)
    rule: ComplianceRuleRecord | None = None
    if _is_request_for(answer, "compliance", rule_id=rule_id):
        rule = ComplianceRuleRecord.model_validate(
            await _deps(config).toolbox.call(RULEBOOK, "get_rule", {"rule_id": rule_id})
        )
    if rule is None or rule.status is RuleStatus.PENDING:
        reason = "the rule is still pending" if rule else "not a compliance decision on this rule"
        logger.warning("supervisor.resume_refused", gate="compliance", run_id=request["run_id"],
                       rule_id=rule_id, reason=reason)  # fmt: skip
        return {}

    decision = GateDecision(
        rule_id=rule.id,
        rule_key=rule.rule_key,
        version=rule.version,
        outcome=rule.status,
        reviewer=rule.decided_by,
        note=rule.decision_note,
        decided_at=rule.decided_at,
    )
    record_run_event(
        SUPERVISOR,
        request["run_id"],
        f"supervisor.compliance_{rule.status.value}",
        decision.model_dump(mode="json"),
    )
    return {"compliance_decisions": [decision]}


async def reassess_after_rule_change(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    """After an approved (or revoked) rule, ask the rulebook again and rebuild the checklist;
    after rejections only, nothing changed — the rulebook in force still stands."""
    round_ids = {rule.id for rule in state.pending_rules}
    changed = [
        d
        for d in state.compliance_decisions
        if d.rule_id in round_ids and d.outcome is not RuleStatus.REJECTED
    ]
    if not changed:
        return {"pending_rules": []}
    update = await graph.guarded(compliance)(state, config)
    failures = update.get("agent_failures", [])
    reassessed = state.model_copy(
        update={**update, "agent_failures": [*state.agent_failures, *failures]}
    )
    update |= await checklist.clearance_checklist_agent(reassessed, config)
    return {**update, "pending_rules": []}


# --------------------------------------------------------------------------- filing gate


def unresolved(state: CaseState) -> list[str]:
    """Everything that stops the shipment from being filed (empty = ready)."""
    case = state.checklist
    if case is None:
        return ["there is no clearance checklist"]
    problems = [f"blocker: {item.title}" for item in case.items if item.severity == "blocker"]
    problems += [f"data gap: {gap}" for gap in state.data_gaps]
    problems += [f"missing input: {name}" for name in case.missing_inputs]
    problems += [f"conflict: {conflict.description}" for conflict in case.conflicts]
    if not case.ready_for_clearance and not problems:
        problems.append("the checklist is not ready for clearance")
    return list(dict.fromkeys(problems))


def _filing_decision(answer: Any, state: CaseState) -> FilingDecisionRecord | None:
    """The filing decision a resume names — only if it is a recorded decision of this run."""
    if not _is_request_for(answer, "filing", run_id=state.run_id):
        return None
    try:
        decision_id = UUID(str(answer.get("filing_decision_id")))
        with session_scope() as session:
            decision = governance_service.get_filing_decision(session, decision_id)
    except (ValueError, FilingDecisionNotFoundError):
        return None
    if decision.run_id != state.run_id or decision.shipment_id != state.shipment_id:
        return None
    return decision


async def filing_gate(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    """Wait for a reviewer's filing decision — only for a ready checklist of a fileable
    shipment; otherwise end the run as ``blocked`` / ``not_fileable``."""
    if problems := unresolved(state):
        summary = state.checklist.summary if state.checklist else "no checklist"
        return {"outcome": "blocked", "outcome_detail": f"{summary} ({'; '.join(problems)})"}
    shipment = await _active_shipment(_deps(config).toolbox, state.shipment_id)
    if shipment.status not in FILEABLE:
        return {
            "outcome": "not_fileable",
            "outcome_detail": f"{shipment.container_id} is {shipment.status.value}; only docked "
            "or under-review shipments are filed",
        }
    request = {
        "gate": "filing",
        "run_id": state.run_id,
        "container_id": shipment.container_id,
        "summary": state.checklist.summary if state.checklist else "",
    }
    decision = _filing_decision(interrupt(request), state)
    if decision is None:  # refused: routed back here, to wait again (see _route_filing)
        logger.warning("supervisor.resume_refused", gate="filing", run_id=state.run_id,
                       reason="not a recorded filing decision of this run")  # fmt: skip
        return {}
    if decision.decision is RuleDecision.REJECT:
        return {
            "filing_decision": decision,
            "outcome": "filing_rejected",
            "outcome_detail": f"filing rejected by {decision.reviewer}: {decision.note}",
        }
    return {"filing_decision": decision}


def _route_filing(state: CaseState) -> str:
    """Ended (blocked, not fileable, rejected) -> END; approved -> file; else wait again."""
    if state.outcome is not None:
        return END
    if state.filing_decision is not None:
        return FILE  # an approval (a rejection sets the outcome)
    return FILING_GATE


async def file_shipment(state: CaseState, toolbox: McpToolbox) -> dict[str, Any]:
    """File an approved, complete case: ``docked -> under_review`` if needed, then
    ``clear_shipment``. Raises ``IncompleteCaseError`` (before any MCP call) if anything is
    unresolved, ``ClearanceRequiresApprovalError`` without an approved filing decision. If the
    documents changed since the analysis, nothing is filed and the run ends ``blocked``."""
    if problems := unresolved(state):
        raise IncompleteCaseError(f"shipment {state.shipment_id} cannot be filed: "
                                  + "; ".join(problems))  # fmt: skip
    decision = state.filing_decision
    if decision is None or decision.decision is not RuleDecision.APPROVE:
        raise ClearanceRequiresApprovalError(f"run {state.run_id} has no approved filing decision")

    shipment = await _active_shipment(toolbox, state.shipment_id)
    documents = TypeAdapter(list[DocumentRecord]).validate_python(
        await toolbox.call(SHIPPING, "get_documents", {"shipment_id": state.shipment_id})
    )
    if {(d.id, d.version) for d in documents} != {(d.id, d.version) for d in state.documents}:
        return {
            "outcome": "blocked",
            "outcome_detail": f"{shipment.container_id}: the documents changed after the "
            "analysis the filing was approved on — not filed; analyse the shipment again",
        }
    if shipment.status is ShipmentStatus.DOCKED:
        await toolbox.call(
            SHIPPING,
            "update_status",
            {"shipment_id": shipment.id, "status": ShipmentStatus.UNDER_REVIEW},
        )
    cleared = ShipmentRecord.model_validate(
        await toolbox.call(
            SHIPPING,
            "clear_shipment",
            {"shipment_id": shipment.id, "filing_decision_id": decision.id},
        )
    )
    return {
        "shipment": cleared,
        "outcome": "filed",
        "outcome_detail": f"{cleared.container_id} cleared; filing approved by "
        f"{decision.reviewer}",
    }


async def file_clearance(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    """Graph node around :func:`file_shipment`."""
    return await file_shipment(state, _deps(config).toolbox)


# --------------------------------------------------------------------------- graph


def build_supervisor(checkpointer: Any) -> CompiledStateGraph[CaseState, Any, CaseState, CaseState]:
    """The pipeline (``graph.add_pipeline``) followed by the two gates, checkpointed."""
    builder = StateGraph(CaseState)
    graph.add_pipeline(builder)
    builder.add_node(COMPLIANCE_GATE, compliance_gate)
    # A Send branch: its input is the gate's request dict, not the CaseState.
    builder.add_node(AWAIT_COMPLIANCE, cast(Any, await_compliance_decision))
    builder.add_node(REASSESS, reassess_after_rule_change)
    builder.add_node(FILING_GATE, filing_gate)
    builder.add_node(FILE, file_clearance)
    builder.add_edge(checklist.AGENT, COMPLIANCE_GATE)
    builder.add_conditional_edges(
        COMPLIANCE_GATE, _route_compliance, [AWAIT_COMPLIANCE, FILING_GATE]
    )
    builder.add_edge(AWAIT_COMPLIANCE, REASSESS)
    builder.add_edge(REASSESS, COMPLIANCE_GATE)
    builder.add_conditional_edges(FILING_GATE, _route_filing, [FILE, FILING_GATE, END])
    builder.add_edge(FILE, END)
    return builder.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------- driving a run

ModelFactory = Callable[[], BaseChatModel | None]


def _supervisor_connector(connect: Connector) -> Connector:
    """The supervisor only lists and reads rules — its rulebook server skips the RAG models."""
    if connect is mcp_clients.stdio_connector:
        return functools.partial(mcp_clients.stdio_connector, env={"RAG_PRELOAD_MODELS": "false"})
    return connect


async def _open_deps(
    stack: AsyncExitStack,
    run_id: str,
    agents: tuple[str, ...],
    connector: Connector | None,
    model_factory: ModelFactory | None,
) -> dict[str, Any]:
    """Toolboxes for the supervisor and for the named agents (only those the run still needs).
    The default transport is looked up at call time, so the dashboard's tests can swap it."""
    connect = connector or stdio_connector

    async def toolbox(servers: tuple[str, ...], actor: str, via: Connector) -> McpToolbox:
        return await stack.enter_async_context(
            open_toolbox(servers, actor=actor, run_id=run_id, connector=via)
        )

    deps: dict[str, Any] = {
        SUPERVISOR: SupervisorDeps(
            await toolbox(SERVERS, SUPERVISOR, _supervisor_connector(connect))
        )
    }
    if verification.AGENT in agents:
        deps[verification.AGENT] = verification.VerificationDeps(
            toolbox=await toolbox(verification.SERVERS, verification.AGENT, connect),
            model=(model_factory or llm.summary_model)(),
        )
    if risk.AGENT in agents:
        deps[risk.AGENT] = risk.RiskDeps(await toolbox(risk.SERVERS, risk.AGENT, connect))
    if compliance.AGENT in agents:
        deps[compliance.AGENT] = compliance.ComplianceDeps(
            toolbox=await toolbox(compliance.SERVERS, compliance.AGENT, connect),
            model=(model_factory or llm.reasoning_model)(),
        )
    return deps


def _update_run(run_id: str, state: RunState, **fields: Any) -> CaseRunRecord:
    with session_scope() as session:
        return governance_service.update_run(session, run_id, state, actor=SUPERVISOR, **fields)


def _open_requests(snapshot: Any) -> list[dict[str, Any]]:
    """The requests of the interrupts still waiting (a resumed branch has a result)."""
    return [
        dict(pending.value)
        for task in snapshot.tasks
        if task.result is None
        for pending in task.interrupts
    ]


async def _drive(
    run_id: str,
    payload: CaseState | Command[Any],
    agents: tuple[str, ...],
    connector: Connector | None,
    model_factory: ModelFactory | None,
) -> ClearanceRun:
    """Run the graph from ``payload`` until it waits at a gate or ends; mirror that in
    ``governance.case_runs``. A cancelled shipment ends the run ``stopped``; any other error
    ends it ``failed`` and is re-raised."""
    config: RunnableConfig = {"configurable": {"thread_id": run_id}}
    try:
        async with AsyncExitStack() as stack:
            deps = await _open_deps(stack, run_id, agents, connector, model_factory)
            with checkpoint.open_checkpointer() as saver:
                supervisor = build_supervisor(saver)
                config["configurable"].update(deps)
                await supervisor.ainvoke(payload, config)
                snapshot = await supervisor.aget_state(config)
    except ShipmentCancelledError as exc:
        record = _update_run(run_id, RunState.STOPPED, detail=str(exc))
        return await inspect_run(run_id, record=record)
    except Exception as exc:
        _update_run(run_id, RunState.FAILED, detail=f"{type(exc).__name__}: {exc}")
        raise

    case = CaseState.model_validate(snapshot.values)
    waiting = _open_requests(snapshot)
    compliance_waits = [r for r in waiting if r.get("gate") == "compliance"]
    if compliance_waits:
        record = _update_run(
            run_id,
            RunState.AWAITING_COMPLIANCE,
            awaiting_rule_ids=[UUID(r["rule_id"]) for r in compliance_waits],
            detail="waiting for decisions on: "
            + ", ".join(f"{r['rule_key']} v{r['version']}" for r in compliance_waits),
        )
    elif waiting:
        record = _update_run(
            run_id,
            RunState.AWAITING_FILING,
            detail=case.checklist.summary if case.checklist else None,
        )
    elif case.outcome is not None:
        record = _update_run(run_id, ENDED_AS[case.outcome], detail=case.outcome_detail)
    else:
        record = _update_run(run_id, RunState.FAILED, detail="the run ended without an outcome")
    return ClearanceRun(run=record, case=case, waiting_on=waiting)


async def inspect_run(run_id: str, *, record: CaseRunRecord | None = None) -> ClearanceRun:
    """A run's current state from its checkpoint (read-only; for dashboards and the CLI)."""
    if record is None:
        with session_scope() as session:
            record = governance_service.get_run(session, run_id)
    with checkpoint.open_checkpointer() as saver:
        snapshot = await build_supervisor(saver).aget_state({"configurable": {"thread_id": run_id}})
    values = snapshot.values or {
        "run_id": run_id,
        "shipment_id": record.shipment_id,
        "as_of": record.as_of,
    }
    waiting = _open_requests(snapshot) if record.state.is_open else []
    return ClearanceRun(run=record, case=CaseState.model_validate(values), waiting_on=waiting)


async def start_clearance_run(
    shipment_id: UUID,
    *,
    started_by: str = SUPERVISOR,
    connector: Connector | None = None,
    model_factory: ModelFactory | None = None,
    as_of: date | None = None,
    run_id: str | None = None,
) -> ClearanceRun:
    """Start a supervised clearance run: the whole pipeline, then the gates. Returns where it
    stopped — normally waiting at a gate. Defaults as ``graph.analyse_shipment`` (stdio MCP
    servers, the configured models, today's IST date). ``RunAlreadyOpenError`` if the shipment
    already has an open run."""
    run_id = run_id or str(uuid4())
    as_of = as_of or today_ist()
    with session_scope() as session:
        governance_service.start_run(session, run_id, shipment_id, as_of, started_by)
    initial = CaseState(run_id=run_id, shipment_id=shipment_id, as_of=as_of)
    return await _drive(run_id, initial, PIPELINE, connector, model_factory)


async def _open_request(thread_id: str, gate: str, **expected: str) -> str:
    """The interrupt id of the run's open request at ``gate`` (matching ``expected``)."""
    with checkpoint.open_checkpointer() as saver:
        snapshot = await build_supervisor(saver).aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
    for task in snapshot.tasks:
        if task.result is not None:
            continue
        for pending in task.interrupts:
            if _is_request_for(pending.value, gate, **expected):
                return str(pending.id)
    raise RunNotAtGateError(f"clearance run {thread_id} has no open {gate} request")


def _check_waiting(run: CaseRunRecord, state: RunState) -> None:
    if not run.state.is_open:
        raise RunAlreadyFinishedError(f"clearance run {run.run_id} already ended as {run.state}")
    if run.state is not state:
        raise RunNotAtGateError(f"clearance run {run.run_id} is {run.state}, not {state}")


async def resume_after_compliance_decision(
    run_id: str,
    rule_id: UUID,
    *,
    connector: Connector | None = None,
    model_factory: ModelFactory | None = None,
) -> ClearanceRun:
    """Resume a run waiting at the compliance gate on ``rule_id``, once a human has decided that
    rule (``compliance_service.decide`` — e.g. on the Compliance page, in another process). The
    gate reads the decision from the rulebook; this call carries none.

    Raises ``RunAlreadyFinishedError`` / ``RunNotAtGateError`` if the run is not waiting on
    that rule and ``DecisionPendingError`` if the rule is still pending.
    """
    async with checkpoint.run_lock(run_id):
        with session_scope() as session:
            run = governance_service.get_run(session, run_id)
            _check_waiting(run, RunState.AWAITING_COMPLIANCE)
            if rule_id not in run.awaiting_rule_ids:
                raise RunNotAtGateError(f"clearance run {run_id} is not waiting on rule {rule_id}")
            rule = compliance_service.get_rule(session, rule_id)
        if rule.status is RuleStatus.PENDING:
            raise DecisionPendingError(
                f"{rule.rule_key} v{rule.version} is still pending — decide it first"
            )
        interrupt_id = await _open_request(run_id, "compliance", rule_id=str(rule_id))
        record_run_event(SUPERVISOR, run_id, "supervisor.resumed",
                         {"gate": "compliance", "rule_id": str(rule_id)})  # fmt: skip
        answer = {"gate": "compliance", "rule_id": str(rule_id)}
        return await _drive(
            run_id,
            Command(resume={interrupt_id: answer}),
            (compliance.AGENT,),
            connector,
            model_factory,
        )


async def resume_after_filing_approval(
    run_id: str,
    decision: RuleDecision | str,
    reviewer: str,
    note: str | None = None,
    *,
    connector: Connector | None = None,
    model_factory: ModelFactory | None = None,
) -> ClearanceRun:
    """The explicit human approval (or rejection) at a run's filing gate — the only way past it.

    ``reviewer`` must be registered (``UnknownReviewerError``). The decision is recorded
    (``governance.filing_decisions``, with the checklist the reviewer saw) before the run
    resumes; approving an incomplete case raises ``IncompleteCaseError`` and records nothing. A
    retry after a crash between the two steps reuses the recorded decision if it is the same
    verdict (``FilingAlreadyDecidedError`` otherwise).
    """
    try:
        verdict = RuleDecision(decision)
    except ValueError as exc:
        raise InvalidFilingDecisionError(
            f"decision must be 'approve' or 'reject', not {decision!r}"
        ) from exc
    person = governance_service.require_reviewer(reviewer)
    async with checkpoint.run_lock(run_id):
        with session_scope() as session:
            run = governance_service.get_run(session, run_id)
            _check_waiting(run, RunState.AWAITING_FILING)
            existing = governance_service.filing_decision_for_run(session, run_id)
        interrupt_id = await _open_request(run_id, "filing", run_id=run_id)
        case = (await inspect_run(run_id, record=run)).case
        if existing is not None and existing.decision is not verdict:
            raise FilingAlreadyDecidedError(
                f"clearance run {run_id}: {existing.reviewer} already decided "
                f"'{existing.decision}' at the filing gate"
            )
        if existing is None:
            if verdict is RuleDecision.APPROVE and (problems := unresolved(case)):
                raise IncompleteCaseError(
                    f"clearance run {run_id} cannot be approved: " + "; ".join(problems)
                )
            with session_scope() as session:
                existing = governance_service.record_filing_decision(
                    session,
                    run_id,
                    verdict,
                    person.name,
                    note,
                    checklist=case.checklist.model_dump(mode="json") if case.checklist else {},
                    ready_for_clearance=bool(case.checklist and case.checklist.ready_for_clearance),
                )
        record_run_event(SUPERVISOR, run_id, "supervisor.resumed",
                         {"gate": "filing", "filing_decision_id": str(existing.id)})  # fmt: skip
        answer = {"gate": "filing", "run_id": run_id, "filing_decision_id": str(existing.id)}
        return await _drive(run_id, Command(resume={interrupt_id: answer}), (), connector,
                            model_factory)  # fmt: skip


def abandon_clearance_run(run_id: str, reviewer: str, reason: str) -> CaseRunRecord:
    """End an open run by hand (``failed``) — e.g. one left ``running`` by a crashed process.
    Its checkpoint stays for the record; the shipment can then start a new run."""
    person = governance_service.require_reviewer(reviewer)
    with session_scope() as session:
        return governance_service.abandon_run(session, run_id, person.name, reason)
