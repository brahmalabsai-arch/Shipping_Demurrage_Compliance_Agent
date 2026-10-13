"""Clearance Checklist agent — the fan-in: one structured checklist from everything the upstream
agents found (implementation plan Phase 10; edge cases, Phase 10).

A pure function of ``CaseState`` — no MCP tools, no LLM — so the checklist is exactly what the
upstream agents reported, nothing added:

* every **document discrepancy** (document verification) becomes one item, severity by kind
  (``blocker`` for problems customs will not assess past: missing documents, quantity / value /
  currency disagreements, invalid currency; ``action`` for dates, an expired quote, a missing FX
  rate). The compliance answer to that discrepancy's question is attached as its
  ``guidance`` — not repeated as a separate item;
* the **demurrage estimate and risk** (demurrage & risk) become one item, severity by risk
  (``action`` when high/medium, ``info`` otherwise), and each of its **data gaps** an item of
  its own (``blocker``: an unresolved gap blocks clearance — Phase 11 enforces it);
* every other **compliance finding** becomes an ``info`` item with its citations (a "not found
  in current rulebook" answer included, so the gap in the rulebook is visible).

Every item names its ``source_agent`` and has a stable ``key``; items are de-duplicated by key.
An upstream agent that failed or timed out (recorded in ``agent_failures`` by the graph's
guard) is listed in ``missing_inputs`` and the checklist is still produced from the others.
Contradictory urgency signals (documents that block clearance while the risk agent reports
low/no risk) are surfaced in ``conflicts`` rather than resolved by picking one.
``ready_for_clearance`` = no blocker, no missing input, no conflict.

State slice: reads ``run_id``, ``as_of``, ``shipment``, ``document_discrepancies``,
``document_outcome``, ``demurrage_estimate``, ``risk``, ``risk_reasons``, ``data_gaps``,
``compliance_findings``, ``agent_failures``; writes ``checklist``.
"""

from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig

from agents import compliance_rag_agent, demurrage_risk_agent, document_verification_agent
from agents.audit import record_run_event
from agents.state import (
    CaseState,
    Checklist,
    ChecklistItem,
    ComplianceFinding,
    Conflict,
    Discrepancy,
    Severity,
)

AGENT = "clearance_checklist_agent"
UPSTREAM = (document_verification_agent.AGENT, demurrage_risk_agent.AGENT,
            compliance_rag_agent.AGENT)  # fmt: skip

DOCUMENT_SEVERITY: dict[str, Severity] = {
    "missing_document": "blocker",
    "quantity_mismatch": "blocker",
    "value_mismatch": "blocker",
    "currency_mismatch": "blocker",
    "invalid_currency": "blocker",
    "date_mismatch": "action",
    "quote_expired": "action",
    "fx_rate_unavailable": "action",
}
_ORDER: dict[Severity, int] = {"blocker": 0, "action": 1, "info": 2}

logger = structlog.get_logger("agents")


def input_name(agent: str) -> str:
    """How an upstream agent is named in ``missing_inputs`` ("demurrage_risk", ...)."""
    return agent.removesuffix("_agent")


def _citations(finding: ComplianceFinding) -> list[str]:
    return [f"{c.rule_key} v{c.version}" for c in finding.citations]


def _document_item(d: Discrepancy, guidance: ComplianceFinding | None) -> ChecklistItem:
    # A document is named by its id; the shipment record by what was read from it, so two
    # findings about the record (e.g. no FX rate on two different dates) stay two items.
    sources = sorted(str(o.document_id or f"{o.source}={o.value}") for o in d.observed)
    return ChecklistItem(
        key=f"document:{d.code}:{d.field}:{','.join(sources)}",
        source_agent=d.agent,
        category="document",
        severity=DOCUMENT_SEVERITY.get(d.code, "action"),
        title=d.code.replace("_", " "),
        detail=d.message,
        guidance=None if guidance is None else guidance.answer,
        references=[] if guidance is None else _citations(guidance),
    )


def _risk_item(state: CaseState) -> ChecklistItem | None:
    if state.risk is None:
        return None
    e = state.demurrage_estimate
    detail = "; ".join(state.risk_reasons)
    if e is not None:
        detail = f"{e.basis} {detail}"
    return ChecklistItem(
        key="demurrage:risk",
        source_agent=demurrage_risk_agent.AGENT,
        category="demurrage",
        severity="action" if state.risk in ("high", "medium") else "info",
        title=f"demurrage risk {state.risk}",
        detail=detail,
        references=(
            []
            if e is None
            else [f"tariff {e.tariff_id}"] + ([] if e.fx is None else [f"fx {e.fx.source_id}"])
        ),
    )


def build_checklist(state: CaseState) -> Checklist:
    """The checklist for ``state`` (deterministic; see the module docstring)."""
    by_question = {f.question: f for f in state.compliance_findings}
    used: set[str] = set()
    items: list[ChecklistItem] = []

    for d in state.document_discrepancies:
        question = compliance_rag_agent.DISCREPANCY_QUESTIONS.get(d.code)
        guidance = by_question.get(question) if question else None
        if guidance is not None:
            used.add(guidance.question)
        items.append(_document_item(d, guidance))

    if (risk_item := _risk_item(state)) is not None:
        items.append(risk_item)
    for gap in state.data_gaps:
        items.append(ChecklistItem(
            key=f"data_gap:{gap}", source_agent=demurrage_risk_agent.AGENT, category="data_gap",
            severity="blocker", title=f"data gap: {gap.split(':', 1)[0]}", detail=gap))  # fmt: skip

    for f in state.compliance_findings:
        if f.question in used:
            continue
        items.append(ChecklistItem(
            key=f"compliance:{f.question}", source_agent=f.agent, category="compliance",
            severity="info", title="rulebook: " + ("answered" if f.found else "not found"),
            detail=f"{f.question} — {f.answer}", references=_citations(f)))  # fmt: skip

    unique: dict[str, ChecklistItem] = {}
    for item in items:
        unique.setdefault(item.key, item)
    ordered = sorted(unique.values(), key=lambda i: (_ORDER[i.severity], i.category, i.key))

    failed = {f.agent for f in state.agent_failures}
    if state.document_outcome is None:
        failed.add(document_verification_agent.AGENT)
    if state.risk is None:
        failed.add(demurrage_risk_agent.AGENT)
    missing = [input_name(a) for a in UPSTREAM if a in failed]

    conflicts = _conflicts(state, ordered)
    blockers = sum(i.severity == "blocker" for i in ordered)
    ready = not blockers and not missing and not conflicts
    shipment = state.shipment
    name = shipment.container_id if shipment else str(state.shipment_id)
    summary = (
        f"{name}: {blockers} blocker(s), {sum(i.severity == 'action' for i in ordered)} "
        f"action(s), {sum(i.severity == 'info' for i in ordered)} note(s)"
        + (f"; missing inputs: {', '.join(missing)}" if missing else "")
        + (f"; {len(conflicts)} conflict(s)" if conflicts else "")
        + (" — ready for clearance." if ready else " — not ready for clearance.")
    )
    return Checklist(
        shipment_id=state.shipment_id,
        container_id=None if shipment is None else shipment.container_id,
        as_of=state.as_of,
        items=ordered,
        missing_inputs=missing,
        conflicts=conflicts,
        ready_for_clearance=ready,
        summary=summary,
    )


def _conflicts(state: CaseState, items: list[ChecklistItem]) -> list[Conflict]:
    """Contradictory urgency signals between upstream agents, stated side by side."""
    blocking = [i for i in items if i.category == "document" and i.severity == "blocker"]
    if blocking and state.risk in ("low", "none"):
        return [
            Conflict(
                description=(
                    "Document verification found problems that block clearance, but the "
                    f"demurrage & risk agent reports {state.risk} risk."
                ),
                signals=[
                    f"{document_verification_agent.AGENT}: {len(blocking)} blocking "
                    f"discrepanc{'y' if len(blocking) == 1 else 'ies'} "
                    f"({', '.join(i.title for i in blocking)})",
                    f"{demurrage_risk_agent.AGENT}: risk {state.risk} "
                    f"({'; '.join(state.risk_reasons)})",
                ],
            )
        ]
    return []


async def clearance_checklist_agent(state: CaseState, config: RunnableConfig) -> dict[str, Any]:
    """LangGraph fan-in node: combine the upstream slices into the ``Checklist``."""
    checklist = build_checklist(state)
    record_run_event(AGENT, state.run_id, "clearance_checklist.completed", {
        "shipment_id": str(state.shipment_id),
        "container_id": checklist.container_id,
        "ready_for_clearance": checklist.ready_for_clearance,
        "missing_inputs": checklist.missing_inputs,
        "conflicts": [c.description for c in checklist.conflicts],
        "items": [{"key": i.key, "severity": i.severity, "source_agent": i.source_agent}
                  for i in checklist.items],
    })  # fmt: skip
    logger.info("agent.run_completed", agent=AGENT, run_id=state.run_id,
                container_id=checklist.container_id, ready=checklist.ready_for_clearance,
                items=len(checklist.items))  # fmt: skip
    return {"checklist": checklist}
